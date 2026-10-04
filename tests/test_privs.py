"""Running as an unprivileged user inside the container (needs root to test the hand-over)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import os, sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from bwwatch.privs import prepare_runtime
data = Path(sys.argv[1])
prepare_runtime(data)
probe = data / "created-after-drop.txt"
probe.write_text("x")
print(os.geteuid(), os.getegid(), oct(probe.stat().st_mode & 0o777), os.getgroups())
try:
    os.setuid(0)
    print("REGAINED-ROOT")
except PermissionError:
    print("cannot regain root")
'''


@unittest.skipUnless(os.geteuid() == 0, "needs root to test the privilege hand-over")
class DropPrivileges(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bwpriv."))
        self.tmp.chmod(0o755)
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def run_child(self, data: Path) -> str:
        done = subprocess.run([sys.executable, "-c", SCRIPT, str(data), str(ROOT)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_fresh_bind_mount_just_works(self):
        data = self.tmp / "data"  # as Docker creates a missing bind-mount source: root-owned
        data.mkdir()
        out = self.run_child(data)
        self.assertIn("10001 10001 0o600 []", out, "unprivileged, no extra groups, new files private")
        self.assertIn("cannot regain root", out)
        self.assertEqual((data.stat().st_uid, data.stat().st_gid), (10001, 10001))
        self.assertEqual((data / "created-after-drop.txt").stat().st_uid, 10001)

    def test_only_bwwatchs_own_files_are_handed_over(self):
        data = self.tmp / "data"
        (data / "backups").mkdir(parents=True)
        (data / "corrupt" / "20260101T000000Z").mkdir(parents=True)
        mine = [data / "bwwatch.db", data / "bwwatch.db-wal", data / "token.json", data / "backups" / "bwwatch-20260101T000000Z.db",
                data / "corrupt" / "20260101T000000Z" / "bwwatch.db"]
        theirs = [data / "notes.txt", data / "photos"]
        for path in mine + theirs[:1]:
            path.write_text("x")
        theirs[1].mkdir()
        (theirs[1] / "a.jpg").write_text("x")
        self.run_child(data)
        for path in mine:
            self.assertEqual(path.stat().st_uid, 10001, str(path))
        for path in theirs + [theirs[1] / "a.jpg"]:
            self.assertEqual(path.stat().st_uid, 0, "%s belongs to the user and must not be touched" % path)

    def test_symlinks_are_never_followed(self):
        data = self.tmp / "data"
        (data / "backups").mkdir(parents=True)
        outside = self.tmp / "outside.txt"
        outside.write_text("secret")
        (data / "backups" / "link").symlink_to(outside)
        (data / "token.json").symlink_to(outside)
        self.run_child(data)
        self.assertEqual(outside.stat().st_uid, 0, "the target of a symlink is not re-owned")


class Unprivileged(unittest.TestCase):
    def test_unwritable_directory_gets_a_plain_explanation(self):
        from bwwatch.errors import ConfigError
        from bwwatch.privs import prepare_runtime

        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        with tempfile.TemporaryDirectory() as raw:
            os.chmod(raw, 0o500)
            with self.assertRaises(ConfigError) as ctx:
                prepare_runtime(Path(raw) / "data")
            self.assertIn("chown", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
