# btrfs-purge

Delete a file or directory from a btrfs subvolume **and from every snapshot of
it**, e.g. to get rid of a large file that btrbk snapshots would otherwise keep
around for weeks.

Single Python file, stdlib only (Python >= 3.11). It talks to btrfs through
ioctls, so it doesn't even need btrfs-progs (the tests do).

> [!WARNING]
> **Do not use this if your snapshots are used for `btrfs send`/`receive`.**
>
> Incremental send (`btrfs send -p <parent>`) only transfers the difference
> between a snapshot and its parent, and assumes the parent is identical on
> both sides. This script modifies snapshots in place, so a modified parent no
> longer matches the copy on the receiving side. Later incremental sends then
> fail, or worse, apply cleanly and leave the backup silently different from
> the source. Received snapshots (with a `received_uuid`) are always skipped,
> but the *source* snapshots that serve as send parents look like any
> other snapshot and cannot be detected.
>
> This affects, among others:
> - **btrbk** with `target` configured (backups). btrbk with only
>   `snapshot_dir` and no `target`, i.e. used just for local snapshots, is fine.
> - **snapper** with `snbk` / snapper backup configs, or **snap-sync**
> - **btrfs-backup**, **buttersink**, **btrfs-sxbackup**, and any custom
>   `btrfs send | btrfs receive` script or cron job
> - **Synology/NAS replication** or other tools replicating btrfs snapshots
>
> Not affected: file-level backup tools that only *read* a snapshot, such as
> borg, restic, rsync or kopia.
>
> Also think twice if you rely on a snapshot being an exact image of a moment
> in time: rollback snapshots (snapper/Timeshift), forensic or audit copies, or
> snapshots compared with `snapper diff` / `btrfs subvolume find-new`. They
> will no longer contain the deleted files.

> [!WARNING]
> **Vibe coded with Claude Opus 5.5**


## How it works

1. Finds the subvolume containing `PATH` and the path relative to its root.
2. Finds snapshots of that subvolume: subvolumes whose `parent_uuid` equals the
   source subvolume's UUID. Snapshots are looked for in
   - the snapshot directories in `/etc/btrbk/btrbk.conf` (each `volume`'s
     `snapshot_dir`, relative ones relative to the volume),
   - every `-s DIR`,
   - `<subvolume root>/.snapshots` (e.g. `~/.snapshots`),
   - the directory of the snapshot `PATH` points into, if it does.

   Snapshots may sit directly in those directories (btrbk: `DIR/name.DATE`)
   or one level down (snapper: `DIR/N/snapshot`).
3. Shows what it would delete (with sizes) and asks.
4. Deletes the live copies first, then the snapshot copies. For each snapshot:
   makes it writable, deletes, and **always** makes it read-only again (also
   on errors). Ctrl-C, SIGTERM and SIGHUP stop the run *between* snapshots,
   never in the middle of one.
5. Rescans, and offers to delete copies in snapshots that appeared during the
   run (see [Running alongside btrbk](#running-alongside-btrbk)).

The same item given twice, or a path inside another given directory, is
deleted once.

Other subvolumes that happen to contain the same relative path are never
touched.

## Usage

```
btrfs-purge.py [-n] [-y] [-s DIR]... [--btrbk-conf FILE] [--snapshots-only] PATH...
```

| Option | |
|---|---|
| `-n`, `--dry-run` | only show what would be deleted |
| `-y`, `--yes` | don't ask for confirmation |
| `-s DIR` | additional snapshot directory; repeatable |
| `--btrbk-conf FILE` | btrbk config to read snapshot dirs from (default `/etc/btrbk/btrbk.conf`, ignored if missing) |
| `--snapshots-only` | leave the live filesystem alone |

Deleting needs root (btrbk snapshots are owned by root). A dry run works as a
normal user; anything you can't read is reported as `permission denied`
instead of being silently missed.

## Examples

See what would go, without deleting anything (run without `sudo` for extra safety):

```sh
btrfs-purge.py -n /var/lib/libvirt/images/old-vm.qcow2
```

Delete a file from the live filesystem and all snapshots (asks first):

```sh
sudo btrfs-purge.py /var/lib/libvirt/images/old-vm.qcow2
```

Delete a whole directory:

```sh
sudo btrfs-purge.py ~/Downloads/iso-collection
```

Already deleted it, or renamed it since? Point at its copy inside any snapshot;
it is removed from all sibling snapshots (and from the live subvolume, if still
there):

```sh
sudo btrfs-purge.py /var/.snapshots/var.20261101T0000/tmp/huge.tar
```

Snapshot directories from btrbk.conf, and `~/.snapshots` if `~` is its own
subvolume, are found automatically. Others can be added, as many as needed:

```sh
sudo btrfs-purge.py -s /mnt/old-snapshots -s /srv/.snaps /home/me/big.iso
```

Only clean the snapshots, keep the live copy:

```sh
sudo btrfs-purge.py --snapshots-only /var/cache/big-but-needed.img
```

Several paths at once, no prompt (e.g. from a script):

```sh
sudo btrfs-purge.py -y /srv/a.iso /srv/b.iso
```

Example output:

```
/var/lib/libvirt/images/old-vm.qcow2
  relative path: lib/libvirt/images/old-vm.qcow2
  snapshot dirs: /var/.snapshots
  3 of 30 subvolumes contain it (or might):
    []    /var/lib/libvirt/images/old-vm.qcow2  (20.0 GiB)
    [ro]  /var/.snapshots/var.20261101T0000/lib/libvirt/images/old-vm.qcow2  (20.0 GiB)
    [ro]  /var/.snapshots/var.20261108T0000/lib/libvirt/images/old-vm.qcow2  (20.0 GiB)

3 item(s), 60.0 GiB allocated in total (shared extents counted once per snapshot; actual space freed may be less).
Delete? [y/N]
```

## Safety

- Refuses to run while `borg`, `borgmatic` or `btrbk` is running (borg
  snapshots before backing up; btrbk creates/rotates snapshots). Checked at
  start and again right after you confirm.
- Resolves `PATH` like the kernel does: symlinks before `..`, so
  `link/../x` means the `x` next to the link's *target*.
- Works on file descriptors, not paths: each subvolume root is opened once
  and verified by UUID to still be the planned subvolume, the read-only flag
  is toggled on that descriptor (ioctl), and deletion walks from it with
  `O_NOFOLLOW`, checking at each step that it stays on the same subvolume. If
  a directory on the path is swapped for a symlink, or the snapshot is
  replaced or renamed, between planning and deletion, the deletion fails
  instead of escaping, and the read-only flag still reaches the right
  snapshot. Recursive deletion never descends into another subvolume, even
  one created after the preview.
- Deletes only what the preview showed: each target's inode number and type
  are recorded when planning and must still match when deleting. A file
  replaced by another file or a directory in the meantime is left alone and
  reported as a failure. (A previewed directory that is still the same
  directory is deleted with whatever it contains by then, like `rm -r`.
  Snapshot contents can't change before deletion anyway: they are read-only.)
- Skips (and says why):
  - targets that are, contain, or lie below a mount point, or that contain a
    nested subvolume. Empty nested-subvolume placeholders inside snapshots
    are fine. Mounts are found via `/proc/self/mountinfo` (bind mounts on the
    same filesystem included), checked when planning and again right before
    each deletion. A bind mount created by root *during* a deletion would not
    be noticed. Mounts made inside other mount namespaces (bwrap, flatpak,
    containers) are invisible to the script and can't affect it.
  - paths that go through a symlink inside a snapshot.
  - received snapshots (those with a `received_uuid`).
- Refuses to delete a subvolume itself; use `btrfs subvolume delete` for that.
- A symlink given as `PATH` deletes the link, not its target.

## Limitations

- Matching is by path. A file renamed between snapshots is only found under the
  name you give; run it again with the other name (e.g. via a snapshot path).
- Space is freed only when no snapshot (or reflink copy) references the data
  any more. It is reclaimed in the background by `btrfs-cleaner`.
- Deleting is not secure erasure: on a copy-on-write filesystem, old blocks
  remain on disk until overwritten. And it doesn't touch backups, e.g. borg
  repositories.
- The live subvolume is found from a snapshot path only if it is an ancestor of
  the snapshot directory or is mounted somewhere.
- A writable subvolume with a parent UUID counts as a snapshot only if it sits
  in a known snapshot directory (btrbk.conf, `-s`, or one named `.snapshots`).
  Elsewhere it is taken as a live subvolume, as one restored from a snapshot
  would be.

## Running alongside btrbk

btrbk may run while this does, e.g. from a timer (`systemctl cat btrbk.timer`
shows when). That is safe:

- A snapshot btrbk takes during the run can't get a copy that was already
  deleted from the live subvolume, and live copies are deleted first. If one
  still slipped in, the rescan at the end finds it and offers to delete it
  (or just does, with `-y`). The rescan only looks at *new snapshots of the
  subvolume planned at the start*, in the directories searched then; it never
  resolves the command-line paths again, so a subvolume moved into place
  during the run is not touched.
- A snapshot btrbk rotates away during the run is reported as `gone`, not as
  a failure.
- A snapshot is writable only for the moment its copy is deleted.

## Tests

`test_btrfs_purge.py` runs the script against real, throwaway btrfs
subvolumes. It needs no root, only a directory on btrfs that you own (default:
the repo directory):

```sh
./test_btrfs_purge.py
BTRFS_TEST_DIR=/some/btrfs/dir ./test_btrfs_purge.py
```

## License

MIT, see [LICENSE](LICENSE).
