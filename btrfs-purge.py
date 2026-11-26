#!/usr/bin/env python3
"""
btrfs-purge: delete a file or directory from a btrfs subvolume AND from every
snapshot of that subvolume (e.g. btrbk snapshots in /var/.snapshot).

PATH may point into the live filesystem (the file may already be gone there,
as long as its parent directory exists) or into a snapshot, e.g.
    btrfs-purge.py /home/me/big.iso
    btrfs-purge.py /var/.snapshot/home.20261101T0000/me/old-vm-images

Matching is by path relative to the subvolume root. "Same subvolume" is
determined by UUID: a snapshot belongs to a source if its parent_uuid equals
the source's uuid. Read-only snapshots are temporarily made writable and are
always set read-only again afterwards. Needs Python >= 3.11 (stdlib only) plus
the btrfs command (for toggling read-only). Run as root.

Deletion is done through directory file descriptors opened with O_NOFOLLOW
from the subvolume root, so a path component swapped for a symlink after
planning makes the deletion fail instead of escaping the subvolume.

Default is to show the plan and ask. Use -n for dry run, -y to skip the prompt.
"""

import argparse
import fcntl
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import uuid as uuidlib

if sys.version_info < (3, 11):
    sys.exit("error: needs Python >= 3.11")

DEFAULT_SNAPSHOT_DIR = "/var/.snapshot"
BLOCKING_PROCS = {"borg", "borgmatic", "btrbk"}

# --- btrfs subvolume info via ioctl (no root needed, no output parsing) -----

_INFO_SIZE = 504  # sizeof(struct btrfs_ioctl_get_subvol_info_args)
BTRFS_IOC_GET_SUBVOL_INFO = (2 << 30) | (_INFO_SIZE << 16) | (0x94 << 8) | 60
BTRFS_ROOT_SUBVOL_RDONLY = 1 << 0
BTRFS_SUBVOL_ROOT_INO = 256
BTRFS_EMPTY_SUBVOL_DIR_INO = 2  # placeholder for a nested subvol inside a snapshot
NULL_UUID = uuidlib.UUID(int=0)


class Subvol:
    def __init__(self, path):
        self.path = path
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            buf = bytearray(_INFO_SIZE)
            fcntl.ioctl(fd, BTRFS_IOC_GET_SUBVOL_INFO, buf)
        finally:
            os.close(fd)
        (self.treeid,) = struct.unpack_from("Q", buf, 0)
        (flags,) = struct.unpack_from("Q", buf, 288)
        self.readonly = bool(flags & BTRFS_ROOT_SUBVOL_RDONLY)
        u = lambda off: uuidlib.UUID(bytes=bytes(buf[off:off + 16]))
        self.uuid, self.parent_uuid, self.received_uuid = u(296), u(312), u(328)

    def __repr__(self):
        return self.path


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
            sys.exit(f"error: {path} is not on btrfs (no subvolume root found)")
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
    for m in mountpoints() if mounts is None else mounts:
        if m == root or not m.startswith(root + "/"):
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

def plan(arg, snapdirs, include_live):
    # Resolve symlinks in the parent only: a symlink argument deletes the link.
    p = os.path.abspath(arg)
    parent = os.path.realpath(os.path.dirname(p))
    name = os.path.basename(p)
    if name in ("", ".", ".."):
        sys.exit(f"error: refusing ambiguous path {arg!r}")
    try:
        if not stat.S_ISDIR(os.stat(parent).st_mode):
            raise NotADirectoryError
    except PermissionError:
        sys.exit(f"error: cannot access {parent}: permission denied (run as root)")
    except (FileNotFoundError, NotADirectoryError):
        sys.exit(f"error: parent directory {parent} does not exist")
    p = os.path.join(parent, name)
    if is_subvol_root(p):
        sys.exit(f"error: {p} is a subvolume itself; use 'btrfs subvolume delete'")

    root = find_subvol_root(parent)
    rel = os.path.relpath(p, root)
    info = Subvol(root)
    # A read-only subvolume with a parent is a snapshot (btrbk makes them ro).
    in_snapshot = info.readonly and info.parent_uuid != NULL_UUID
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
            s = Subvol(c)
            if s.parent_uuid == source_uuid and s.uuid not in seen_uuids:
                seen_uuids.add(s.uuid)
                subvols.append(s)

    targets = []  # (subvol, target, access denied?)
    for s in subvols:
        t = os.path.join(s.path, rel)
        try:
            os.lstat(t)
        except PermissionError:
            targets.append((s, t, True))
        except (FileNotFoundError, NotADirectoryError):
            pass
        else:
            targets.append((s, t, False))
    return p, rel, live, in_snapshot, sorted(seen_dirs), len(subvols), targets


def check_target(s, rel, t, denied):
    """Return (reason to skip or None, allocated size, size incomplete?)."""
    if denied:
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


# --- deletion ---------------------------------------------------------------

def set_ro(path, value):
    # Own process group: a Ctrl-C at the terminal must not kill btrfs midway.
    subprocess.run(["btrfs", "property", "set", "-ts", path, "ro",
                    "true" if value else "false"], check=True, process_group=0)


_NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def remove_at(subvol_root, rel):
    """Delete subvol_root/rel without following symlinks or leaving the
    subvolume: walk to the parent with O_NOFOLLOW dir fds, check each stays
    on the subvolume's device, then unlink/rmtree relative to that fd."""
    parts = rel.split("/")
    if not parts or any(c in ("", ".", "..") for c in parts):
        raise ValueError(f"bad relative path {rel!r}")
    fd = os.open(subvol_root, _NOFOLLOW_DIR)
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
        if stat.S_ISDIR(st.st_mode):
            if st.st_dev != dev and st.st_ino != BTRFS_EMPTY_SUBVOL_DIR_INO:
                raise OSError(f"{name}: is another subvolume/mount")
            shutil.rmtree(name, dir_fd=fd)  # fd-based, never follows symlinks
        else:
            os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)


def delete_in(s, rel):
    """Delete rel inside subvolume s, making it writable temporarily."""
    t = os.path.join(s.path, rel)
    m = mount_conflict(s.path, t)  # re-check now, not only at planning time
    if m:
        raise OSError(f"mount point {m} on or below the path")
    readonly = Subvol(s.path).readonly
    try:
        if readonly:
            set_ro(s.path, False)
        remove_at(s.path, rel)
    finally:
        if readonly:
            try:
                set_ro(s.path, True)
            except Exception as e:
                print(f"!!! FAILED to make {s.path} read-only again: {e}\n"
                      f"!!! fix manually: btrfs property set -ts {s.path} ro true",
                      file=sys.stderr)
                raise


_stop_requested = False


def _request_stop(signum, frame):
    global _stop_requested
    _stop_requested = True
    print(f"\n{signal.Signals(signum).name}: stopping after the current item "
          "(its snapshot is restored to read-only first)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="Delete a file/dir from a btrfs subvolume and all its snapshots.")
    ap.add_argument("paths", nargs="+", metavar="PATH")
    ap.add_argument("-s", "--snapshot-dir", action="append", dest="snapshot_dirs",
                    metavar="DIR",
                    help=f"directory holding snapshots; repeatable (default {DEFAULT_SNAPSHOT_DIR}). "
                         "Also searched automatically: <subvolume root>/.snapshots and the "
                         "directory of the snapshot PATH points into")
    ap.add_argument("-n", "--dry-run", action="store_true", help="only show what would be deleted")
    ap.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--snapshots-only", action="store_true",
                    help="leave the live filesystem alone, only purge snapshots")
    args = ap.parse_args()

    if args.snapshot_dirs:
        for d in args.snapshot_dirs:
            if not os.path.isdir(d):
                sys.exit(f"error: snapshot dir {d} does not exist")
        snapdirs = args.snapshot_dirs
    else:
        snapdirs = [DEFAULT_SNAPSHOT_DIR]  # silently skipped if absent

    procs = blocking_processes()
    if procs:
        sys.exit("error: refusing to run while these are active: " + ", ".join(procs))
    if os.geteuid() != 0:
        print("note: not root; paths you can't read show as 'permission denied' "
              "and sizes may be incomplete", file=sys.stderr)

    work, total = [], 0
    for arg in args.paths:
        p, rel, live, in_snapshot, searched, n_subvols, targets = plan(
            arg, snapdirs, not args.snapshots_only)
        print(f"\n{p}\n  relative path: {rel}")
        print(f"  snapshot dirs: {', '.join(searched) or '(none found)'}")
        if in_snapshot and not args.snapshots_only:
            print(f"  live subvolume: {live or 'not found (not an ancestor of the snapshot, not mounted); skipped'}")
        print(f"  {len(targets)} of {n_subvols} subvolumes contain it (or might):")
        for s, t, denied in targets:
            skip, size, incomplete = check_target(s, rel, t, denied)
            flags = ["ro"] if s.readonly else []
            mark = (f"SKIP: {skip}" if skip else
                    f">= {human(size)}, some dirs unreadable" if incomplete else human(size))
            print(f"    {'[' + ','.join(flags) + ']':5} {t}  ({mark})")
            if not skip:
                work.append((s, rel, t))
                total += size
        if not targets:
            print("    (nothing found)")

    if not work:
        print("\nNothing to delete.")
        return
    print(f"\n{len(work)} item(s), {human(total)} allocated in total "
          "(shared extents counted once per snapshot; actual space freed may be less).")
    if args.dry_run:
        return
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("error: not a terminal; use -y to confirm")
        try:
            answer = input("Delete? [y/N] ")
        except (KeyboardInterrupt, EOFError):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("\naborted")

    # Re-check right before acting: borg may have started while we waited.
    procs = blocking_processes()
    if procs:
        sys.exit("error: refusing to run while these are active: " + ", ".join(procs))

    # From here on, signals only request a stop between items, so a snapshot
    # is never left writable by an interruption.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _request_stop)
    failed = 0
    for s, rel, t in work:
        if _stop_requested:
            sys.exit("stopped; remaining items were not touched")
        try:
            delete_in(s, rel)
            print(f"deleted {t}")
        except Exception as e:
            failed += 1
            print(f"FAILED  {t}: {e}", file=sys.stderr)
    if _stop_requested:
        sys.exit("stopped after the last item")
    if failed:
        sys.exit(f"{failed} deletion(s) failed")
    print("done. Space is reclaimed by btrfs in the background (btrfs-cleaner).")


if __name__ == "__main__":
    main()
