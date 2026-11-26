# btrfs-purge

Delete a file or directory from a btrfs subvolume **and from every snapshot of
it**, e.g. to get rid of a large file that btrbk snapshots would otherwise keep
around for weeks.

Single Python file, stdlib only, plus the `btrfs` command (btrfs-progs).

## How it works

1. Finds the subvolume containing `PATH` and the path relative to its root.
2. Finds snapshots of that subvolume: subvolumes whose `parent_uuid` equals the
   source subvolume's UUID. Snapshots are looked for in
   - every `-s DIR` (default `/var/.snapshot`),
   - `<subvolume root>/.snapshots` (e.g. `~/.snapshots`),
   - the directory of the snapshot `PATH` points into, if it does.
3. Shows what it would delete (with sizes) and asks.
4. For each match: makes a read-only snapshot writable, deletes, and **always**
   makes it read-only again (also on errors, Ctrl-C, SIGTERM).

Other subvolumes that happen to contain the same relative path are never
touched.

## Usage

```
btrfs-purge.py [-n] [-y] [-s DIR]... [--snapshots-only] [--force-received] PATH...
```

| Option | |
|---|---|
| `-n`, `--dry-run` | only show what would be deleted |
| `-y`, `--yes` | don't ask for confirmation |
| `-s DIR` | snapshot directory; repeatable (default `/var/.snapshot`) |
| `--snapshots-only` | leave the live filesystem alone |
| `--force-received` | also modify snapshots with a `received_uuid` (breaks send/receive) |

Deleting needs root (btrbk snapshots are owned by root). A dry run works as a
normal user; anything you can't read is reported as `permission denied`
instead of being silently missed.

## Examples

See what would go, without deleting anything:

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
sudo btrfs-purge.py /var/.snapshot/var.20261101T0000/tmp/huge.tar
```

A user's home with its snapshots in `~/.snapshots` is found automatically if
`~` is its own subvolume. Otherwise name the directory, or several:

```sh
sudo btrfs-purge.py -s /var/.snapshot -s /home/me/.snapshots /home/me/big.iso
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
  snapshot dirs: /var/.snapshot
  3 of 30 subvolumes contain it (or might):
    []    /var/lib/libvirt/images/old-vm.qcow2  (20.0 GiB)
    [ro]  /var/.snapshot/var.20261101T0000/lib/libvirt/images/old-vm.qcow2  (20.0 GiB)
    [ro]  /var/.snapshot/var.20261108T0000/lib/libvirt/images/old-vm.qcow2  (20.0 GiB)

3 item(s), 60.0 GiB allocated in total (shared extents counted once per snapshot; actual space freed may be less).
Delete? [y/N]
```

## Safety

- Refuses to run while `borg`, `borgmatic` or `btrbk` is running (borg
  snapshots before backing up; btrbk creates/rotates snapshots). Checked at
  start and again right after you confirm, but it can't prevent one from
  *starting* during the deletion.
- Skips (and says why):
  - targets containing a nested subvolume or a mount point. Empty
    nested-subvolume placeholders inside snapshots are fine.
  - paths that go through a symlink inside a snapshot.
  - received snapshots, unless `--force-received` is given.
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
