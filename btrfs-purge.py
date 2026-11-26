#!/usr/bin/env python3
"""
btrfs-purge: delete a file or directory from a btrfs subvolume AND from every
snapshot of that subvolume (e.g. btrbk snapshots in /var/.snapshots).

PATH may point into the live filesystem (the file may already be gone there,
as long as its parent directory exists) or into a snapshot, e.g.
    btrfs-purge.py /var/home/me/big.iso
    btrfs-purge.py /var/.snapshots/home.20261101T0000/me/old-vm-images

Matching is by path relative to the subvolume root. "Same subvolume" is
determined by UUID: a snapshot belongs to a source if its parent_uuid equals
the source's uuid. Snapshot directories are read from btrbk.conf, plus -s DIR,
plus <subvolume root>/.snapshots.

Read-only snapshots are temporarily made writable and always set read-only
again afterwards. Everything is done through file descriptors and btrfs
ioctls: the subvolume root is opened once and verified by UUID, the ro flag is
toggled on that fd, and deletion walks from it with O_NOFOLLOW, never leaving
the subvolume. A path swapped for a symlink, or a snapshot replaced, after
planning makes the deletion fail instead of escaping.

If btrbk creates snapshots during the run, live copies are deleted first and a
rescan at the end catches new snapshots that still contain the item.

Needs Python >= 3.11, stdlib only; no btrfs-progs needed. Run as root.
Default is to show the plan and ask. Use -n for dry run, -y to skip the prompt.
"""

import argparse
import fcntl
import os
import re
import signal
import stat
import struct
import sys
import uuid as uuidlib

if sys.version_info < (3, 11):
    sys.exit("error: needs Python >= 3.11")

BTRBK_CONF = "/etc/btrbk/btrbk.conf"
BLOCKING_PROCS = {"borg", "borgmatic", "btrbk"}
MAX_RESCANS = 3

# --- btrfs ioctls (no root needed for reading, no output parsing) -----------

_INFO_SIZE = 504  # sizeof(struct btrfs_ioctl_get_subvol_info_args)
BTRFS_IOC_GET_SUBVOL_INFO = (2 << 30) | (_INFO_SIZE << 16) | (0x94 << 8) | 60
BTRFS_IOC_SUBVOL_GETFLAGS = (2 << 30) | (8 << 16) | (0x94 << 8) | 25
BTRFS_IOC_SUBVOL_SETFLAGS = (1 << 30) | (8 << 16) | (0x94 << 8) | 26
BTRFS_ROOT_SUBVOL_RDONLY = 1 << 0  # in subvol info flags
BTRFS_SUBVOL_RDONLY = 1 << 1       # in GET/SETFLAGS flags
BTRFS_SUBVOL_ROOT_INO = 256
BTRFS_EMPTY_SUBVOL_DIR_INO = 2     # placeholder for a nested subvol inside a snapshot
NULL_UUID = uuidlib.UUID(int=0)

_NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class PlanError(Exception):
    pass


class Gone(Exception):
    """The snapshot disappeared during the run (e.g. btrbk rotation)."""


class Subvol:
    def __init__(self, path, fd=None):
        """Read subvolume info of path, or of the open directory fd if given."""
        self.path = path
        own_fd = fd is None
        if own_fd:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            buf = bytearray(_INFO_SIZE)
            fcntl.ioctl(fd, BTRFS_IOC_GET_SUBVOL_INFO, buf)
        finally:
            if own_fd:
                os.close(fd)
        (self.treeid,) = struct.unpack_from("Q", buf, 0)
        (flags,) = struct.unpack_from("Q", buf, 288)
        self.readonly = bool(flags & BTRFS_ROOT_SUBVOL_RDONLY)
        u = lambda off: uuidlib.UUID(bytes=bytes(buf[off:off + 16]))
        self.uuid, self.parent_uuid, self.received_uuid = u(296), u(312), u(328)

    def __repr__(self):
        return self.path


def set_ro_fd(fd, value):
    """Set or clear the read-only flag of the subvolume open as fd."""
    buf = bytearray(8)
    fcntl.ioctl(fd, BTRFS_IOC_SUBVOL_GETFLAGS, buf)
    (flags,) = struct.unpack("Q", buf)
    flags = flags | BTRFS_SUBVOL_RDONLY if value else flags & ~BTRFS_SUBVOL_RDONLY
    fcntl.ioctl(fd, BTRFS_IOC_SUBVOL_SETFLAGS, struct.pack("Q", flags))


def set_ro(path, value):
    fd = os.open(path, _NOFOLLOW_DIR)
    try:
        set_ro_fd(fd, value)
    finally:
        os.close(fd)


def is_subvol_root(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_ino == BTRFS_SUBVOL_ROOT_INO


def find_subvol_root(path):
    """Closest ancestor directory (or path itself) that is a subvolume root."""
    p = path
    while True:
        if is_subvol_root(p):
            return p
        if p == "/":
            raise PlanError(f"{path} is not on btrfs (no subvolume root found)")
        p = os.path.dirname(p)


def mountpoints(fstype=None):
    """Mount points from /proc/self/mountinfo, optionally of one fs type."""
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, _, right = line.partition(" - ")
            if fstype is None or right.split()[0] == fstype:
                yield re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)),
                             left.split()[4])


def mount_conflict(subvol_root, target, mounts=None):
    """A mount point that is the target, inside it, or on the path between the
    subvolume root and the target (bind mounts included), else None."""
    root = os.path.realpath(subvol_root)
    prefix = root.rstrip("/") + "/"  # "/" -> "/", not "//"
    for m in mountpoints() if mounts is None else mounts:
        if m == root or not m.startswith(prefix):
            continue
        if m == target or m.startswith(target + "/") or target.startswith(m + "/"):
            return m
    return None


def find_live_subvol(source_uuid, snapshot_root):
    """Locate the source subvolume: first among the ancestors of the snapshot
    (e.g. ~/.snapshots/x -> ~), then among mounted btrfs subvolumes."""
    p = os.path.dirname(snapshot_root)
    while p != "/":
        if is_subvol_root(p):
            try:
                if Subvol(p).uuid == source_uuid:
                    return p
            except OSError:
                pass
        p = os.path.dirname(p)
    for mp in mountpoints("btrfs"):
        if is_subvol_root(mp):
            try:
                if Subvol(mp).uuid == source_uuid:
                    return mp
            except OSError:
                pass
    return None


def btrbk_snapshot_dirs(conf):
    """Snapshot directories configured in btrbk.conf: each volume's
    snapshot_dir (relative ones are relative to the volume), or the volume
    directory itself if none is set (btrbk's default)."""
    try:
        with open(conf) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    dirs, global_sd, volume, vol_has_sd = [], None, None, False

    def close_volume():
        if volume is not None and not vol_has_sd and global_sd is None:
            dirs.append(volume)

    for line in lines:
        words = line.split("#", 1)[0].split(None, 1)
        if len(words) != 2:
            continue
        key, val = words[0], words[1].strip()
        if key == "volume":
            close_volume()
            volume, vol_has_sd = val, False
            if global_sd is not None:
                dirs.append(os.path.join(volume, global_sd))
        elif key == "snapshot_dir":
            if volume is None:
                global_sd = val
            else:
                vol_has_sd = True
                dirs.append(os.path.join(volume, val))  # absolute val wins
    close_volume()
    return list(dict.fromkeys(os.path.normpath(d) for d in dirs))


# --- safety checks ----------------------------------------------------------

def blocking_processes():
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        # argv[0] for native/entry-point scripts, argv[1] for "python3 /usr/bin/borg"
        names = {os.path.basename(a).decode(errors="replace") for a in argv[:2] if a}
        hit = names & BLOCKING_PROCS
        if hit:
            found.append(f"{pid} ({', '.join(sorted(hit))})")
    return found


def path_is_contained(subvol_root, rel, target):
    """True if target's parent resolves inside subvol_root without symlinks."""
    expected = os.path.normpath(os.path.join(os.path.realpath(subvol_root),
                                             os.path.dirname(rel)))
    return os.path.realpath(os.path.dirname(target)) == expected


def is_placeholder(path, st):
    """A nested subvolume appears inside a snapshot as an empty placeholder
    dir (ino 2); rmdir removes it."""
    return (stat.S_ISDIR(st.st_mode) and st.st_ino == BTRFS_EMPTY_SUBVOL_DIR_INO
            and not os.listdir(path))


def scan_tree(target, dev):
    """Return (allocated bytes, nested-subvolume/mount path or None,
    True if some directories could not be read). dev is the subvolume's st_dev;
    anything on another device (other subvolume, mount) is reported."""
    st = os.lstat(target)
    if st.st_dev != dev and not is_placeholder(target, st):
        return 0, target, False
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512, None, False
    total, stack, incomplete = st.st_blocks * 512, [target], False
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except PermissionError:
            incomplete = True
            continue
        for e in entries:
            est = e.stat(follow_symlinks=False)
            if est.st_dev != dev:
                if is_placeholder(e.path, est):
                    continue
                return total, e.path, incomplete
            total += est.st_blocks * 512
            if stat.S_ISDIR(est.st_mode):
                stack.append(e.path)
    return total, None, incomplete


def human(n):
    for unit in "B KiB MiB GiB TiB".split():
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


# --- planning ---------------------------------------------------------------

def resolve_arg(arg):
    """Absolute path of arg with symlinks in the parent resolved *before* any
    '..' is applied (as the kernel does), but the last component kept as is,
    so a symlink argument means the link itself. Trailing slashes are fine."""
    a = arg.rstrip("/")
    if not a:
        raise PlanError("refusing to work on /")
    if not os.path.isabs(a):
        a = os.path.join(os.getcwd(), a)  # no normpath: it would apply '..' early
    name = os.path.basename(a)
    if name in (".", ".."):
        raise PlanError(f"refusing ambiguous path {arg!r}")
    parent = os.path.realpath(os.path.dirname(a))
    return parent, name


def is_snapshot(root, info, snapdirs):
    """Whether the subvolume at root is a snapshot rather than a live source."""
    if info.parent_uuid == NULL_UUID:
        return False
    if info.readonly:
        return True
    # Writable, but has a parent: a snapshot if it lives in a snapshot dir.
    # (A live subvolume restored from a snapshot also has a parent uuid.)
    in_snapdir = lambda d: d in snapdirs or os.path.basename(d) == ".snapshots"
    d = os.path.dirname(root)
    if in_snapdir(d):
        return True
    # snapper layout: <snapdir>/N/snapshot
    return os.path.basename(root) == "snapshot" and in_snapdir(os.path.dirname(d))


def plan(arg, snapdirs, include_live):
    parent, name = resolve_arg(arg)
    try:
        if not stat.S_ISDIR(os.stat(parent).st_mode):
            raise NotADirectoryError
    except PermissionError:
        raise PlanError(f"cannot access {parent}: permission denied (run as root)")
    except (FileNotFoundError, NotADirectoryError):
        raise PlanError(f"parent directory {parent} does not exist")
    p = os.path.join(parent, name)
    if is_subvol_root(p):
        raise PlanError(f"{p} is a subvolume itself; use 'btrfs subvolume delete'")

    known = {os.path.realpath(d) for d in snapdirs}
    root = find_subvol_root(parent)
    rel = os.path.relpath(p, root)
    info = Subvol(root)
    in_snapshot = is_snapshot(root, info, known)
    dirs = list(snapdirs)

    if in_snapshot:
        source_uuid = info.parent_uuid
        live_root = find_live_subvol(source_uuid, root)
        dirs.append(os.path.dirname(root))
        if os.path.basename(root) == "snapshot":  # snapper: .snapshots/N/snapshot
            dirs.append(os.path.dirname(os.path.dirname(root)))
    else:
        source_uuid = info.uuid
        live_root = root
    if live_root:
        dirs.append(os.path.join(live_root, ".snapshots"))
    live = live_root if include_live else None

    seen_dirs, seen_uuids = set(), set()
    subvols = [Subvol(live)] if live else []
    if live:
        seen_uuids.add(subvols[0].uuid)
    for d in dirs:
        d = os.path.realpath(d)
        if d in seen_dirs or not os.path.isdir(d):
            continue
        seen_dirs.add(d)
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except PermissionError:
            print(f"warning: cannot list {d}: permission denied", file=sys.stderr)
            continue
        # Snapshots directly in d, or one level down (snapper: d/N/snapshot).
        candidates = []
        for e in entries:
            if is_subvol_root(e.path):
                candidates.append(e.path)
            elif e.is_dir(follow_symlinks=False):
                try:
                    candidates += sorted(c.path for c in os.scandir(e.path)
                                         if is_subvol_root(c.path))
                except OSError:
                    pass
        for c in candidates:
            try:
                s = Subvol(c)
            except OSError:
                continue  # vanished meanwhile, or not readable
            if s.parent_uuid == source_uuid and s.uuid not in seen_uuids:
                seen_uuids.add(s.uuid)
                subvols.append(s)

    targets = []  # (subvol, target, identity or None if access denied)
    for s in subvols:
        t = os.path.join(s.path, rel)
        try:
            st = os.lstat(t)
        except PermissionError:
            targets.append((s, t, None))
        except (FileNotFoundError, NotADirectoryError):
            pass
        else:
            targets.append((s, t, identity(st)))
    return p, rel, live, in_snapshot, sorted(seen_dirs), len(subvols), targets


def identity(st):
    """What must still match at deletion time: inode number and file type.
    (btrfs allocates inode numbers increasingly, so a replacement differs.)"""
    return st.st_ino, stat.S_IFMT(st.st_mode)


def check_target(s, rel, t, ident):
    """Return (reason to skip or None, allocated size, size incomplete?)."""
    if ident is None:
        return "permission denied, can't tell if it exists (run as root)", 0, False
    if s.received_uuid != NULL_UUID:
        return "received snapshot (send/receive target), not touched", 0, False
    if not path_is_contained(s.path, rel, t):
        return "path goes through a symlink", 0, False
    m = mount_conflict(s.path, t)
    if m:
        return f"mount point {m} on or below the path", 0, False
    size, nested, incomplete = scan_tree(t, os.lstat(s.path).st_dev)
    if nested:
        return f"contains nested subvolume/mount {nested}", 0, False
    return None, size, incomplete


def build_work(paths, snapdirs, include_live, exclude=frozenset(), quiet=False):
    """Plan all paths. Returns (work items, total size). Work items are
    (subvol, rel, target, is_live, identity), live copies first: if btrbk snapshots the
    live subvolume during the run, the new snapshot won't contain the item."""
    out = (lambda *a, **k: None) if quiet else print
    work, total = [], 0
    for arg in paths:
        try:
            p, rel, live, in_snapshot, searched, n_subvols, targets = plan(
                arg, snapdirs, include_live)
        except PlanError as e:
            if quiet:
                continue
            raise
        targets = [x for x in targets if (x[0].uuid, rel) not in exclude]
        if quiet and not targets:
            continue
        out(f"\n{p}\n  relative path: {rel}")
        out(f"  snapshot dirs: {', '.join(searched) or '(none found)'}")
        if in_snapshot and include_live:
            out(f"  live subvolume: {live or 'not found (not an ancestor of the snapshot, not mounted); skipped'}")
        out(f"  {len(targets)} of {n_subvols} subvolumes contain it (or might):")
        for s, t, ident in targets:
            skip, size, incomplete = check_target(s, rel, t, ident)
            flags = ["ro"] if s.readonly else []
            mark = (f"SKIP: {skip}" if skip else
                    f">= {human(size)}, some dirs unreadable" if incomplete else human(size))
            out(f"    {'[' + ','.join(flags) + ']':5} {t}  ({mark})")
            if not skip:
                work.append((s, rel, t, s.path == live, ident))
                total += size
        if not targets:
            out("    (nothing found)")
    work.sort(key=lambda w: not w[3])  # stable: live copies first
    return work, total


# --- deletion ---------------------------------------------------------------

def _rmtree_at(parent_fd, name, dev):
    """Recursively delete parent_fd/name like rm -r, but fd-based (never
    follows symlinks) and never descending into another subvolume (different
    st_dev). Empty nested-subvolume placeholders are removed."""
    fd = os.open(name, _NOFOLLOW_DIR, dir_fd=parent_fd)  # ELOOP if now a symlink
    try:
        st = os.fstat(fd)
        if st.st_dev != dev and st.st_ino != BTRFS_EMPTY_SUBVOL_DIR_INO:
            raise OSError(f"{name}: is another subvolume/mount, not descending")
        with os.scandir(fd) as it:
            entries = list(it)
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                _rmtree_at(fd, e.name, dev)
            else:
                os.unlink(e.name, dir_fd=fd)  # EISDIR if swapped for a dir
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def remove_at(root_fd, rel, expect=None):
    """Delete rel below the subvolume root open as root_fd, without following
    symlinks or leaving the subvolume: walk to the parent with O_NOFOLLOW dir
    fds, check each stays on the subvolume's device, then delete relative to
    that fd. If expect (an identity()) is given, the target must still be
    that same file, else nothing is deleted."""
    parts = rel.split("/")
    if not parts or any(c in ("", ".", "..") for c in parts):
        raise ValueError(f"bad relative path {rel!r}")
    fd = os.dup(root_fd)
    try:
        dev = os.fstat(fd).st_dev
        for comp in parts[:-1]:
            nfd = os.open(comp, _NOFOLLOW_DIR, dir_fd=fd)  # ELOOP if symlink
            os.close(fd)
            fd = nfd
            if os.fstat(fd).st_dev != dev:
                raise OSError(f"{comp}: crosses into another subvolume/mount")
        name = parts[-1]
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if expect is not None and identity(st) != expect:
            raise OSError(f"{name}: changed since the preview (another file now), "
                          "not deleted")
        if stat.S_ISDIR(st.st_mode):
            _rmtree_at(fd, name, dev)
        else:
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def _restore_ro(s, fd):
    """Make the subvolume open as fd read-only again and verify it."""
    try:
        set_ro_fd(fd, True)
        if not Subvol(s.path, fd).readonly:
            raise OSError("still writable after setting ro")
    except Exception as e:
        if not os.path.lexists(s.path):
            print(f"note: {s.path} disappeared while writable (btrbk rotation?)",
                  file=sys.stderr)
            return
        print(f"!!! FAILED to make {s.path} (uuid {s.uuid}) read-only again: {e}\n"
              f"!!! fix manually: btrfs property set -ts {s.path} ro true",
              file=sys.stderr)
        raise


def delete_in(s, rel, expect=None):
    """Delete rel inside subvolume s, making it writable temporarily.

    Works on an fd of the subvolume root, verified to be the planned
    subvolume (uuid); the ro flag is toggled on that same fd. So a path
    replaced after planning is never written to."""
    t = os.path.join(s.path, rel)
    m = mount_conflict(s.path, t)  # re-check now, not only at planning time
    if m:
        raise OSError(f"mount point {m} on or below the path")
    try:
        fd = os.open(s.path, _NOFOLLOW_DIR)
    except FileNotFoundError:
        raise Gone(s.path)
    try:
        cur = Subvol(s.path, fd)
        if cur.uuid != s.uuid:
            raise OSError(f"{s.path} is no longer the planned subvolume (uuid changed)")
        if cur.received_uuid != NULL_UUID:
            raise OSError(f"{s.path} is a received snapshot")
        try:
            if cur.readonly:
                set_ro_fd(fd, False)
            remove_at(fd, rel, expect)
        finally:
            if cur.readonly and not Subvol(s.path, fd).readonly:
                _restore_ro(s, fd)
    except Exception as e:
        if not isinstance(e, Gone) and not os.path.lexists(s.path):
            raise Gone(s.path) from e
        raise
    finally:
        os.close(fd)


_stop_requested = False


def _request_stop(signum, frame):
    global _stop_requested
    _stop_requested = True
    print(f"\n{signal.Signals(signum).name}: stopping after the current item "
          "(its snapshot is restored to read-only first)", file=sys.stderr)


def run_work(work):
    """Delete all work items. Returns (done, failed) sets of (uuid, rel)."""
    done, failed = set(), set()
    for s, rel, t, _, ident in work:
        if _stop_requested:
            sys.exit("stopped; remaining items were not touched")
        try:
            delete_in(s, rel, ident)
            print(f"deleted {t}")
            done.add((s.uuid, rel))
        except Gone:
            print(f"gone    {t} (snapshot disappeared, rotated away by btrbk?)")
            done.add((s.uuid, rel))
        except Exception as e:
            failed.add((s.uuid, rel))
            print(f"FAILED  {t}: {e}", file=sys.stderr)
    if _stop_requested:
        sys.exit("stopped after the last item")
    return done, failed


def confirm(prompt):
    if not sys.stdin.isatty():
        sys.exit("error: not a terminal; use -y to confirm")
    try:
        answer = input(prompt)
    except (KeyboardInterrupt, EOFError):
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def check_blocking():
    procs = blocking_processes()
    if procs:
        sys.exit("error: refusing to run while these are active: " + ", ".join(procs))


def main():
    ap = argparse.ArgumentParser(
        description="Delete a file/dir from a btrfs subvolume and all its snapshots.")
    ap.add_argument("paths", nargs="+", metavar="PATH")
    ap.add_argument("-s", "--snapshot-dir", action="append", default=[],
                    dest="snapshot_dirs", metavar="DIR",
                    help="additional directory holding snapshots; repeatable. Always "
                         "searched: snapshot dirs from btrbk.conf, <subvolume root>/"
                         ".snapshots, and the directory of the snapshot PATH points into")
    ap.add_argument("--btrbk-conf", default=BTRBK_CONF, metavar="FILE",
                    help=f"btrbk config to read snapshot dirs from (default {BTRBK_CONF}; "
                         "ignored if missing)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="only show what would be deleted")
    ap.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--snapshots-only", action="store_true",
                    help="leave the live filesystem alone, only purge snapshots")
    args = ap.parse_args()

    for d in args.snapshot_dirs:
        if not os.path.isdir(d):
            sys.exit(f"error: snapshot dir {d} does not exist")
    snapdirs = btrbk_snapshot_dirs(args.btrbk_conf) + args.snapshot_dirs
    include_live = not args.snapshots_only

    check_blocking()
    if os.geteuid() != 0:
        print("note: not root; paths you can't read show as 'permission denied' "
              "and sizes may be incomplete", file=sys.stderr)

    try:
        work, total = build_work(args.paths, snapdirs, include_live)
    except PlanError as e:
        sys.exit(f"error: {e}")
    if not work:
        print("\nNothing to delete.")
        return
    print(f"\n{len(work)} item(s), {human(total)} allocated in total "
          "(shared extents counted once per snapshot; actual space freed may be less).")
    if args.dry_run:
        return
    if not args.yes and not confirm("Delete? [y/N] "):
        sys.exit("aborted")
    check_blocking()  # borg may have started while we waited

    # From here on, signals only request a stop between items, so a snapshot
    # is never left writable by an interruption.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _request_stop)
    done, failed = run_work(work)

    # btrbk may have taken a snapshot while we were deleting: look again.
    for _ in range(MAX_RESCANS):
        work, total = build_work(args.paths, snapdirs, include_live,
                                 exclude=done | failed, quiet=True)
        if not work:
            break
        print(f"\n{len(work)} item(s) appeared during the run (new snapshots?):")
        for _, _, t, _, _ in work:
            print(f"    {t}")
        if not args.yes and not confirm("Delete these too? [y/N] "):
            break
        d, f = run_work(work)
        done |= d
        failed |= f
    else:
        print("warning: new copies kept appearing; run again later", file=sys.stderr)

    if failed:
        sys.exit(f"{len(failed)} deletion(s) failed")
    print("done. Space is reclaimed by btrfs in the background (btrfs-cleaner).")


if __name__ == "__main__":
    main()
