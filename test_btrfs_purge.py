#!/usr/bin/env python3
"""
Tests for btrfs-purge.py, run against real btrfs subvolumes.

Needs a directory on btrfs that you own; no root required. Test subvolumes are
created in a temporary directory below BTRFS_TEST_DIR (default: this repo's
directory) and removed afterwards.

    ./test_btrfs_purge.py            # or: python3 -m unittest -v test_btrfs_purge
    BTRFS_TEST_DIR=/some/btrfs/dir ./test_btrfs_purge.py
"""

import contextlib
import importlib.util
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "btrfs-purge.py")
BASE = os.environ.get("BTRFS_TEST_DIR", HERE)

_spec = importlib.util.spec_from_file_location("btrfs_purge", SCRIPT)
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)


def btrfs(*args):
    subprocess.run(["btrfs", *args], check=True, stdout=subprocess.DEVNULL)


def on_btrfs(path):
    r = subprocess.run(["stat", "-f", "-c", "%T", path], capture_output=True, text=True)
    return r.stdout.strip() == "btrfs"


def remove_subvol(path):
    """Delete a subvolume (and nested ones) without needing root."""
    if bp.is_subvol_root(path):
        bp.set_ro(path, False)
    for e in os.scandir(path):
        if e.is_dir(follow_symlinks=False):
            if bp.is_subvol_root(e.path):
                remove_subvol(e.path)
            else:
                st = os.lstat(e.path)
                if st.st_ino == bp.BTRFS_EMPTY_SUBVOL_DIR_INO:
                    os.rmdir(e.path)
                else:
                    os.chmod(e.path, 0o755)
                    remove_subvol(e.path)
        else:
            os.unlink(e.path)
    os.rmdir(path)  # works unprivileged for an empty subvolume, too


@unittest.skipUnless(shutil.which("btrfs"), "btrfs command not found")
@unittest.skipUnless(on_btrfs(BASE), f"{BASE} is not on btrfs (set BTRFS_TEST_DIR)")
class PurgeTest(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".btrfs-purge-test-", dir=BASE)
        self.live = self.p("live")
        self.snaps = self.p("snaps")
        btrfs("subvolume", "create", self.live)
        os.mkdir(self.snaps)
        os.makedirs(self.p("live/me/vm"))
        self.write("live/me/vm/big.img", 1 << 20)
        self.write("live/me/keep.txt", 10)

    def tearDown(self):
        remove_subvol(self.d)

    # --- helpers --------------------------------------------------------

    def p(self, rel):
        return os.path.join(self.d, rel)

    def write(self, rel, size):
        with open(self.p(rel), "wb") as f:
            f.write(os.urandom(size))

    def snapshot(self, name, src="live", dest="snaps"):
        path = self.p(f"{dest}/{name}")
        btrfs("subvolume", "snapshot", "-r", self.p(src), path)
        return path

    def run_purge(self, *args, ok=True):
        # Ignore the real /etc/btrbk/btrbk.conf unless a test passes its own.
        if "--btrbk-conf" not in args:
            args = ("--btrbk-conf", os.devnull, *args)
        r = subprocess.run([sys.executable, SCRIPT, *args], cwd=self.d,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def run_main(self, *args, hook=None):
        """Run main() in-process; hook(n, root_fd, rel) is called before the
        n-th deletion (n from 0). Returns (exit code, stdout, stderr)."""
        orig_remove, orig_argv = bp.remove_at, sys.argv
        handlers = {sig: signal.getsignal(sig)
                    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        calls = []

        def wrapped(root_fd, rel):
            if hook:
                hook(len(calls), root_fd, rel)
            calls.append(rel)
            orig_remove(root_fd, rel)

        bp.remove_at = wrapped
        sys.argv = ["btrfs-purge", "--btrbk-conf", os.devnull, *args]
        out, err, code = io.StringIO(), io.StringIO(), 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                bp.main()
        except SystemExit as e:
            code = e.code
        finally:
            bp.remove_at, sys.argv = orig_remove, orig_argv
            for sig, h in handlers.items():
                signal.signal(sig, h)
            bp._stop_requested = False
        return code, out.getvalue(), err.getvalue()

    def exists(self, rel):
        return os.path.lexists(self.p(rel))

    def assert_all_ro(self):
        for e in os.scandir(self.snaps):
            self.assertTrue(bp.Subvol(e.path).readonly, f"{e.path} left writable")

    # --- tests ----------------------------------------------------------

    def test_ioctl_reads_uuids(self):
        snap = bp.Subvol(self.snapshot("s1"))
        live = bp.Subvol(self.live)
        self.assertEqual(snap.parent_uuid, live.uuid)
        self.assertTrue(snap.readonly)
        self.assertFalse(live.readonly)

    def test_dry_run_deletes_nothing(self):
        self.snapshot("s1")
        r = self.run_purge("-n", "-s", "snaps", "live/me/vm/big.img")
        self.assertIn("2 of 2 subvolumes", r.stdout)
        self.assertTrue(self.exists("live/me/vm/big.img"))
        self.assertTrue(self.exists("snaps/s1/me/vm/big.img"))

    def test_no_tty_without_yes_refuses(self):
        self.snapshot("s1")
        r = self.run_purge("-s", "snaps", "live/me/vm/big.img", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.exists("snaps/s1/me/vm/big.img"))

    def test_delete_file_everywhere(self):
        self.snapshot("s1")
        self.snapshot("s2")
        self.run_purge("-y", "-s", "snaps", "live/me/vm/big.img")
        for rel in ("live", "snaps/s1", "snaps/s2"):
            self.assertFalse(self.exists(f"{rel}/me/vm/big.img"), rel)
            self.assertTrue(self.exists(f"{rel}/me/keep.txt"), rel)
        self.assert_all_ro()

    def test_delete_directory(self):
        self.snapshot("s1")
        self.run_purge("-y", "-s", "snaps", "live/me/vm")
        self.assertFalse(self.exists("live/me/vm"))
        self.assertFalse(self.exists("snaps/s1/me/vm"))
        self.assertTrue(self.exists("snaps/s1/me/keep.txt"))
        self.assert_all_ro()

    def test_unrelated_subvolume_untouched(self):
        self.snapshot("s1")
        other = self.p("other")
        btrfs("subvolume", "create", other)
        os.makedirs(self.p("other/me/vm"))
        self.write("other/me/vm/big.img", 10)
        self.snapshot("other.1", src="other")
        self.run_purge("-y", "-s", "snaps", "live/me/vm/big.img")
        self.assertTrue(self.exists("other/me/vm/big.img"))
        self.assertTrue(self.exists("snaps/other.1/me/vm/big.img"))

    def test_snapshot_path_for_already_renamed_file(self):
        self.snapshot("s1")
        os.rename(self.p("live/me/vm/big.img"), self.p("live/me/vm/new.img"))
        self.snapshot("s2")
        self.run_purge("-y", "-s", "snaps", "snaps/s1/me/vm/big.img")
        self.assertFalse(self.exists("snaps/s1/me/vm/big.img"))
        self.assertTrue(self.exists("live/me/vm/new.img"))
        self.assertTrue(self.exists("snaps/s2/me/vm/new.img"))
        self.assert_all_ro()

    def test_live_found_from_snapshot_path_and_deleted(self):
        # snapshot inside the live subvolume, like ~/.snapshots
        os.mkdir(self.p("live/.snapshots"))
        self.snapshot("h1", dest="live/.snapshots")
        r = self.run_purge("-y", "-s", "snaps", "live/.snapshots/h1/me/vm/big.img")
        self.assertIn(f"live subvolume: {self.live}", r.stdout)
        self.assertFalse(self.exists("live/me/vm/big.img"))
        self.assertFalse(self.exists("live/.snapshots/h1/me/vm/big.img"))

    def test_snapshots_only_keeps_live(self):
        self.snapshot("s1")
        self.run_purge("-y", "--snapshots-only", "-s", "snaps", "live/me/vm/big.img")
        self.assertTrue(self.exists("live/me/vm/big.img"))
        self.assertFalse(self.exists("snaps/s1/me/vm/big.img"))

    def test_dot_snapshots_dir_found_automatically(self):
        os.mkdir(self.p("live/.snapshots"))
        self.snapshot("h1", dest="live/.snapshots")
        self.snapshot("s1")
        self.run_purge("-y", "-s", "snaps", "live/me/vm/big.img")
        self.assertFalse(self.exists("live/.snapshots/h1/me/vm/big.img"))
        self.assertFalse(self.exists("snaps/s1/me/vm/big.img"))

    def test_multiple_snapshot_dirs(self):
        os.mkdir(self.p("snaps2"))
        self.snapshot("s1")
        self.snapshot("t1", dest="snaps2")
        self.run_purge("-y", "-s", "snaps", "-s", "snaps2", "live/me/vm/big.img")
        self.assertFalse(self.exists("snaps/s1/me/vm/big.img"))
        self.assertFalse(self.exists("snaps2/t1/me/vm/big.img"))

    def test_missing_snapshot_dir_is_error(self):
        r = self.run_purge("-n", "-s", "nope", "live/me/vm/big.img", ok=False)
        self.assertIn("does not exist", r.stderr)

    def test_nested_subvolume_in_live_is_skipped(self):
        btrfs("subvolume", "create", self.p("live/me/nested"))
        self.snapshot("s1")
        r = self.run_purge("-y", "-s", "snaps", "live/me")
        self.assertIn("contains nested subvolume", r.stdout)
        self.assertTrue(self.exists("live/me/nested"))
        # in the snapshot the nested subvol is just an empty placeholder: deleted
        self.assertFalse(self.exists("snaps/s1/me"))

    def test_subvolume_itself_refused(self):
        self.snapshot("s1")
        r = self.run_purge("-n", "-s", "snaps", "snaps/s1", ok=False)
        self.assertIn("is a subvolume itself", r.stderr)

    def test_symlink_argument_deletes_link_not_target(self):
        os.symlink("keep.txt", self.p("live/me/link"))
        self.snapshot("s1")
        self.run_purge("-y", "-s", "snaps", "live/me/link")
        self.assertFalse(self.exists("live/me/link"))
        self.assertFalse(self.exists("snaps/s1/me/link"))
        self.assertTrue(self.exists("live/me/keep.txt"))
        self.assertTrue(self.exists("snaps/s1/me/keep.txt"))

    def test_path_through_symlink_in_snapshot_skipped(self):
        # In s1, "me/vm" is a symlink pointing outside the snapshot.
        outside = self.p("outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "big.img"), "w") as f:
            f.write("must survive")
        shutil.rmtree(self.p("live/me/vm"))
        os.symlink(outside, self.p("live/me/vm"))
        self.snapshot("s1")
        os.unlink(self.p("live/me/vm"))
        os.mkdir(self.p("live/me/vm"))
        self.write("live/me/vm/big.img", 10)
        r = self.run_purge("-y", "-s", "snaps", "live/me/vm/big.img")
        self.assertIn("path goes through a symlink", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(outside, "big.img")))
        self.assertFalse(self.exists("live/me/vm/big.img"))

    def test_nothing_found(self):
        self.snapshot("s1")
        r = self.run_purge("-y", "-s", "snaps", "live/me/vm/absent")
        self.assertIn("Nothing to delete", r.stdout)

    def test_refuses_while_borg_runs(self):
        fake = subprocess.Popen(["borg", "60"], executable=shutil.which("sleep"))
        try:
            time.sleep(0.1)
            r = self.run_purge("-n", "-s", "snaps", "live/me/vm/big.img", ok=False)
            self.assertIn("borg", r.stderr)
        finally:
            fake.kill()
            fake.wait()

    def test_snapshot_restored_ro_when_delete_fails(self):
        s = bp.Subvol(self.snapshot("s1"))
        orig = bp.remove_at
        bp.remove_at = lambda root, rel: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                bp.delete_in(s, "me/vm/big.img")
        finally:
            bp.remove_at = orig
        self.assertTrue(bp.Subvol(s.path).readonly)
        self.assertTrue(self.exists("snaps/s1/me/vm/big.img"))

    def test_symlink_swapped_in_after_planning_does_not_escape(self):
        # Plan sees a real dir; before deletion it is replaced by a symlink
        # pointing outside. remove_at must refuse instead of following it.
        outside = self.p("outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "big.img"), "w") as f:
            f.write("must survive")
        s = bp.Subvol(self.live)
        shutil.rmtree(self.p("live/me/vm"))
        os.symlink(outside, self.p("live/me/vm"))
        with self.assertRaises(OSError):
            bp.delete_in(s, "me/vm/big.img")
        self.assertTrue(os.path.exists(os.path.join(outside, "big.img")))

    def test_symlink_swap_within_same_subvolume_does_not_redirect(self):
        # Symlink to another dir on the *same* subvolume: the device check
        # can't catch this, only O_NOFOLLOW does.
        os.makedirs(self.p("live/precious"))
        with open(self.p("live/precious/big.img"), "w") as f:
            f.write("must survive")
        s = bp.Subvol(self.live)
        shutil.rmtree(self.p("live/me/vm"))
        os.symlink("../precious", self.p("live/me/vm"))
        with self.assertRaises(OSError):
            bp.delete_in(s, "me/vm/big.img")
        self.assertTrue(self.exists("live/precious/big.img"))

    def test_symlinked_target_dir_is_unlinked_not_followed(self):
        outside = self.p("outside")
        os.makedirs(outside)
        open(os.path.join(outside, "f"), "w").close()
        shutil.rmtree(self.p("live/me/vm"))
        os.symlink(outside, self.p("live/me/vm"))
        bp.delete_in(bp.Subvol(self.live), "me/vm")
        self.assertFalse(self.exists("live/me/vm"))
        self.assertTrue(os.path.exists(os.path.join(outside, "f")))

    def test_bad_relative_paths_rejected(self):
        fd = os.open(self.live, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for rel in ("../x", "me/../../x", "", "/etc/passwd", "me//x"):
                with self.assertRaises(ValueError, msg=rel):
                    bp.remove_at(fd, rel)
        finally:
            os.close(fd)

    def test_mount_conflict_with_root_subvolume(self):
        # live subvolume mounted at / : mounts below / must still be seen
        mc = lambda t, mounts: bp.mount_conflict("/", t, mounts)
        self.assertEqual(mc("/srv/data", ["/", "/srv"]), "/srv")
        self.assertEqual(mc("/srv", ["/", "/srv/data"]), "/srv/data")
        self.assertIsNone(mc("/srv/data", ["/", "/home"]))

    def test_replaced_subvolume_not_used(self):
        # Planned s1; afterwards s1 is renamed away and a new snapshot takes
        # its path. Nothing may be deleted from either.
        planned = bp.Subvol(self.snapshot("s1"))
        os.rename(self.p("snaps/s1"), self.p("snaps/s1.old"))
        self.snapshot("s1")
        with self.assertRaisesRegex(OSError, "no longer the planned subvolume"):
            bp.delete_in(planned, "me/vm/big.img")
        self.assertTrue(self.exists("snaps/s1/me/vm/big.img"))
        self.assertTrue(self.exists("snaps/s1.old/me/vm/big.img"))
        self.assertTrue(bp.Subvol(self.p("snaps/s1")).readonly)
        self.assertTrue(bp.Subvol(self.p("snaps/s1.old")).readonly)

    def test_replaced_writable_subvolume_not_used(self):
        # Writable (live) subvolume: only the fd uuid check protects here.
        planned = bp.Subvol(self.live)
        os.rename(self.live, self.p("live.old"))
        btrfs("subvolume", "create", self.live)
        os.makedirs(self.p("live/me/vm"))
        self.write("live/me/vm/big.img", 10)
        with self.assertRaisesRegex(OSError, "no longer the planned subvolume"):
            bp.delete_in(planned, "me/vm/big.img")
        self.assertTrue(self.exists("live/me/vm/big.img"))
        self.assertTrue(self.exists("live.old/me/vm/big.img"))

    def test_rmtree_refuses_entry_swapped_for_symlink(self):
        # As if a listed directory was replaced by a symlink before being opened.
        # Target on the same subvolume, so only O_NOFOLLOW (not st_dev) stops it.
        os.makedirs(self.p("live/precious"))
        self.write("live/precious/f", 10)
        os.symlink("../precious", self.p("live/me/swapped"))
        fd = os.open(self.p("live/me"), os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(OSError):
                bp._rmtree_at(fd, "swapped", os.fstat(fd).st_dev)
        finally:
            os.close(fd)
        self.assertTrue(self.exists("live/precious/f"))

    def test_nested_subvolume_created_after_planning_not_traversed(self):
        s = bp.Subvol(self.live)
        btrfs("subvolume", "create", self.p("live/me/vm/nested"))
        self.write("live/me/vm/nested/inner", 10)
        with self.assertRaisesRegex(OSError, "another subvolume"):
            bp.delete_in(s, "me/vm")
        self.assertTrue(self.exists("live/me/vm/nested/inner"))

    def test_rmtree_deletes_deep_tree_with_symlinks(self):
        os.makedirs(self.p("live/me/vm/a/b/c"))
        self.write("live/me/vm/a/b/c/f", 10)
        os.symlink("/etc", self.p("live/me/vm/a/etc"))
        os.symlink("/etc/passwd", self.p("live/me/vm/a/b/passwd"))
        bp.delete_in(bp.Subvol(self.live), "me/vm")
        self.assertFalse(self.exists("live/me/vm"))
        self.assertTrue(os.path.exists("/etc/passwd"))

    def test_mount_conflict(self):
        root = os.path.realpath(self.live)
        t = root + "/me/vm"
        mc = lambda mounts: bp.mount_conflict(root, t, mounts)
        self.assertIsNone(mc([root, "/", root + "/other", root + "/me/vmx"]))
        self.assertEqual(mc([t]), t)                                # target is a mount
        self.assertEqual(mc([t + "/sub"]), t + "/sub")              # mount inside target
        self.assertEqual(mc([root + "/me"]), root + "/me")          # (bind) mount on the path

    def test_snapper_layout_found(self):
        # .snapshots/<N>/snapshot, as snapper does it
        for n in ("1", "2"):
            os.makedirs(self.p(f"snaps/{n}"))
            self.snapshot(f"{n}/snapshot")
        self.run_purge("-y", "-s", "snaps", "live/me/vm/big.img")
        self.assertFalse(self.exists("snaps/1/snapshot/me/vm/big.img"))
        self.assertFalse(self.exists("snaps/2/snapshot/me/vm/big.img"))
        for n in ("1", "2"):
            self.assertTrue(bp.Subvol(self.p(f"snaps/{n}/snapshot")).readonly)

    def test_snapper_layout_from_snapshot_path(self):
        for n in ("1", "2"):
            os.makedirs(self.p(f"snaps/{n}"))
            self.snapshot(f"{n}/snapshot")
        # Sibling snapshots in snaps/<N>/snapshot are found. (The live subvol is
        # not: here it is neither mounted nor an ancestor of the snapshots.)
        r = self.run_purge("-n", "snaps/1/snapshot/me/vm/big.img")
        self.assertIn("2 of 2 subvolumes", r.stdout)
        self.assertIn("snaps/2/snapshot/me/vm/big.img", r.stdout)

    def test_interrupt_stops_between_items_and_restores_ro(self):
        self.snapshot("s1")
        self.snapshot("s2")
        calls = []

        def interrupt(n, root_fd, rel):
            calls.append(n)
            os.kill(os.getpid(), signal.SIGINT)  # Ctrl-C in the middle of item 1

        code, out, err = self.run_main("-y", "--snapshots-only", "-s", self.snaps,
                                       self.p("live/me/vm/big.img"), hook=interrupt)
        self.assertIn("stopped", str(code))
        self.assertEqual(calls, [0])
        remaining = [n for n in ("s1", "s2") if self.exists(f"snaps/{n}/me/vm/big.img")]
        self.assertEqual(len(remaining), 1)
        self.assert_all_ro()

    # --- btrbk.conf ---------------------------------------------------------

    def write_conf(self, text):
        path = self.p("btrbk.conf")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_btrbk_conf_parsing(self):
        conf = self.write_conf("""
timestamp_format        long
snapshot_preserve       48h 7d 4w

# Var
volume /var
  snapshot_dir /var/.snapshots
  subvolume .

# Home
volume /var/home
  snapshot_dir /var/.snapshots   # same dir again
  subvolume .

volume /var/home/m3
  snapshot_dir .snapshots
  subvolume .
""")
        self.assertEqual(bp.btrbk_snapshot_dirs(conf),
                         ["/var/.snapshots", "/var/home/m3/.snapshots"])

    def test_btrbk_conf_global_and_default_dirs(self):
        conf = self.write_conf("snapshot_dir snaps\nvolume /a\nvolume /b\n"
                               "  snapshot_dir /abs\n")
        self.assertEqual(bp.btrbk_snapshot_dirs(conf), ["/a/snaps", "/b/snaps", "/abs"])
        conf = self.write_conf("volume /a\n  subvolume x\nvolume /b\n  snapshot_dir s\n")
        self.assertEqual(bp.btrbk_snapshot_dirs(conf), ["/a", "/b/s"])
        self.assertEqual(bp.btrbk_snapshot_dirs(self.p("missing.conf")), [])

    def test_btrbk_conf_used_for_discovery(self):
        self.snapshot("s1")
        conf = self.write_conf(f"volume {self.live}\n  snapshot_dir {self.snaps}\n"
                               "  subvolume .\n")
        self.run_purge("-y", "--btrbk-conf", conf, "live/me/vm/big.img")
        self.assertFalse(self.exists("snaps/s1/me/vm/big.img"))

    # --- path resolution ----------------------------------------------------

    def test_symlink_then_dotdot_resolved_like_the_kernel(self):
        # live/me/link -> ../other/sub, so live/me/link/../victim is
        # live/other/victim for the kernel, but live/me/victim lexically.
        os.makedirs(self.p("live/other/sub"))
        self.write("live/other/victim", 10)
        self.write("live/me/victim", 10)
        os.symlink("../other/sub", self.p("live/me/link"))
        self.snapshot("s1")
        self.run_purge("-y", "-s", "snaps", "live/me/link/../victim")
        self.assertFalse(self.exists("live/other/victim"))
        self.assertFalse(self.exists("snaps/s1/other/victim"))
        self.assertTrue(self.exists("live/me/victim"))
        self.assertTrue(self.exists("snaps/s1/me/victim"))

    def test_trailing_slash_accepted(self):
        self.snapshot("s1")
        self.run_purge("-y", "-s", "snaps", "live/me/vm/")
        self.assertFalse(self.exists("live/me/vm"))
        self.assertFalse(self.exists("snaps/s1/me/vm"))

    def test_root_and_dot_refused(self):
        for arg in ("/", "live/me/.", "live/me/.."):
            r = self.run_purge("-n", "-s", "snaps", arg, ok=False)
            self.assertNotEqual(r.returncode, 0, arg)
            self.assertIn("refusing", r.stderr, arg)

    def test_writable_snapshot_in_snapdir_counts_as_snapshot(self):
        self.snapshot("s1")
        btrfs("subvolume", "snapshot", self.live, self.p("snaps/rw"))  # writable
        r = self.run_purge("-n", "-s", "snaps", "snaps/rw/me/vm/big.img")
        self.assertIn("live subvolume:", r.stdout)       # treated as a snapshot
        self.assertIn("snaps/s1/me/vm/big.img", r.stdout)  # sibling found

    # --- btrbk running concurrently ------------------------------------------

    def test_live_copies_deleted_first(self):
        self.write("live/me/other.img", 10)
        self.snapshot("s1")
        r = self.run_purge("-y", "-s", "snaps", "live/me/vm/big.img", "live/me/other.img")
        deleted = [l.split()[1] for l in r.stdout.splitlines() if l.startswith("deleted")]
        self.assertEqual(len(deleted), 4)
        self.assertTrue(all("/live/" in d for d in deleted[:2]), deleted)
        self.assertTrue(all("/snaps/" in d for d in deleted[2:]), deleted)

    def test_rescan_catches_snapshot_taken_during_run(self):
        self.snapshot("s1")

        def btrbk_snapshots(n, root_fd, rel):
            if n == 0:  # before the live copy is gone
                self.snapshot("late")

        code, out, err = self.run_main("-y", "-s", self.snaps,
                                       self.p("live/me/vm/big.img"), hook=btrbk_snapshots)
        self.assertEqual(code, 0, out + err)
        self.assertIn("appeared during the run", out)
        self.assertFalse(self.exists("snaps/late/me/vm/big.img"))
        self.assert_all_ro()

    def test_snapshot_rotated_away_is_not_a_failure(self):
        self.snapshot("s1")
        self.snapshot("s2")

        def btrbk_rotates(n, root_fd, rel):
            if n == 0:
                remove_subvol(self.p("snaps/s2"))

        code, out, err = self.run_main("-y", "-s", self.snaps,
                                       self.p("live/me/vm/big.img"), hook=btrbk_rotates)
        self.assertEqual(code, 0, out + err)
        self.assertIn("gone", out)
        self.assertNotIn("FAILED", err)

    def test_snapshot_renamed_while_writable_still_restored(self):
        # ro is restored through the fd, so it reaches the snapshot even
        # after its path changed.
        self.snapshot("s1")

        def rename(n, root_fd, rel):
            if n == 1:  # the snapshot (n == 0 is the live copy), now writable
                os.rename(self.p("snaps/s1"), self.p("snaps/s1.moved"))

        self.run_main("-y", "-s", self.snaps, self.p("live/me/vm/big.img"), hook=rename)
        self.assertTrue(bp.Subvol(self.p("snaps/s1.moved")).readonly)

    @unittest.skipIf(os.geteuid() == 0, "root can read everything")
    def test_unreadable_reported_not_missed(self):
        os.chmod(self.p("live/me/vm"), 0)
        self.snapshot("s1")
        os.chmod(self.p("live/me/vm"), 0o755)
        r = self.run_purge("-n", "-s", "snaps", "live/me/vm/big.img")
        self.assertIn("permission denied", r.stdout)
        self.assertIn("2 of 2 subvolumes", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
