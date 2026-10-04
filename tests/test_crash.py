"""Hard-kill tests: SIGKILL at random moments must never corrupt or half-write anything.

``kill -9`` is what the kernel does on an out-of-memory kill and what `docker kill` does,
and it stops the process between any two instructions. It is NOT the same as pulling the
power (which also depends on the disk honouring fsync) - that part is what
``synchronous=FULL`` is for - but it is the failure that is practical to test thoroughly.
"""
from __future__ import annotations

import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bwwatch import db
from bwwatch.wave import TokenStore

ROOT = Path(__file__).resolve().parents[1]
ROUNDS = int(os.environ.get("BWWATCH_CRASH_ROUNDS", "25"))

POLL_CHILD = r'''
import random, sys
sys.path.insert(0, sys.argv[3])
from pathlib import Path
from bwwatch.config import Config
from bwwatch.cycle import ApplianceData, FetchResult, apply_cycle
from bwwatch.db import open_database

data = Path(sys.argv[1])
conn, _ = open_database(data)
cfg = Config.from_env({"DATA_DIR": str(data), "NTFY_TOPIC": "crash-test-topic-123"})
rng = random.Random(int(sys.argv[2]))
n = 0
while True:
    n += 1
    events = [{"id": rng.randrange(10**9), "faultCode": rng.randrange(1, 50), "message": "m" * rng.randrange(1, 300),
               "timestamp": 1760000000 + n} for _ in range(rng.randint(0, 5))]
    a = ApplianceData(mac="AA:BB", name="Heater", serial="S1", model="M", listing={},
                      status={"mode": "Heat Pump", "heatModeValue": 3, "setpointFahrenheit": 120 + rng.randrange(3)},
                      status_fetched=True, faults={"notifications": events}, faults_fetched=True)
    apply_cycle(conn, cfg, FetchResult(started_at="2026-01-01T00:00:00Z", appliances=[a]))
    print(n, flush=True)          # printed only AFTER the transaction committed
'''

TOKEN_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from bwwatch.wave import TokenStore
store = TokenStore(Path(sys.argv[1]) / "token.json")
n = 0
while True:
    n += 1
    store.save("token-%08d-" % n + "x" * 2000)
    print(n, flush=True)
'''

BACKUP_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from bwwatch.db import open_database, backup_now
data = Path(sys.argv[1])
conn, _ = open_database(data)
while True:
    backup_now(conn, data / "backups", keep=3)
    print("ok", flush=True)
'''


class Child:
    """A subprocess whose acknowledgements (lines it printed after committing) we collect."""

    def __init__(self, script: str, *args: str):
        # every child script reads (its own args..., repo root) from sys.argv
        self.proc = subprocess.Popen(
            [sys.executable, "-c", script, *args, str(ROOT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.lines = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line.strip())

    def kill(self):
        self.proc.kill()
        self.proc.wait()
        self.reader.join(5)
        self.stderr = self.proc.stderr.read()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def acknowledged(self) -> int:
        numbers = [int(x) for x in self.lines if x.isdigit()]
        return max(numbers) if numbers else 0


def invariants(test: unittest.TestCase, conn: sqlite3.Connection, acknowledged_total: int, rounds_done: int) -> int:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    polls = one("SELECT COUNT(*) FROM polls")
    test.assertEqual(db.integrity_check(conn), [])
    test.assertEqual(one("SELECT COUNT(*) FROM readings"), polls, "a poll and its reading are written together or not at all")
    if polls:
        test.assertEqual(one("SELECT MAX(id) FROM polls"), polls, "no gaps: a half-written poll left nothing behind")
    # durability of acknowledged commits: none lost; at most one unacknowledged commit per killed child
    test.assertGreaterEqual(polls, acknowledged_total, "a commit the child had reported as done was lost")
    test.assertLessEqual(polls, acknowledged_total + rounds_done, "more commits than could have happened")
    # a recorded fault always has its alert queued, and vice versa
    fault_ids = {r[0] for r in conn.execute("SELECT id FROM faults WHERE baseline = 0 AND kind = 'event'")}
    alert_ids = [r[0] for r in conn.execute("SELECT fault_id FROM outbox WHERE kind = 'fault' AND fault_id IS NOT NULL")]
    test.assertEqual(sorted(alert_ids), sorted(fault_ids), "every fault has exactly one queued alert")
    test.assertEqual(len(alert_ids), len(set(alert_ids)))
    test.assertEqual(one("SELECT COALESCE(SUM(new_faults), 0) FROM polls"), len(fault_ids))
    test.assertEqual(one("SELECT COUNT(*) FROM outbox WHERE kind = 'fault' AND fault_id IS NULL"), 0)
    return polls


class KillNine(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bwcrash."))
        self.addCleanup(shutil.rmtree, str(self.dir), True)

    def test_polling_survives_repeated_sigkill(self):
        rng = random.Random(4242)
        acknowledged = 0
        polls_seen = []
        for round_no in range(1, ROUNDS + 1):
            child = Child(POLL_CHILD, str(self.dir), str(rng.randrange(10**6)))
            time.sleep(rng.uniform(0.12, 0.7))  # sometimes still importing, sometimes mid-write, sometimes mid-first-poll
            child.kill()
            acknowledged += child.acknowledged()
            conn, recovery = db.open_database(self.dir)
            try:
                self.assertIsNone(recovery, "round %d: SIGKILL must never look like corruption (%s)" % (round_no, child.stderr[-300:]))
                polls_seen.append(invariants(self, conn, acknowledged, round_no))
            finally:
                db.checkpoint_and_close(conn)
            # the number of polls recorded is whatever survived; it must never go backwards
            self.assertEqual(polls_seen, sorted(polls_seen))
            # an unacknowledged-but-committed poll counts as acknowledged from now on
            acknowledged = polls_seen[-1]
        self.assertGreater(polls_seen[-1], ROUNDS, "the children made real progress (%d polls)" % polls_seen[-1])
        self.assertFalse((self.dir / "corrupt").exists())

    def test_token_file_is_never_torn(self):
        rng = random.Random(7)
        last_ack = 0
        store = TokenStore(self.dir / "token.json")
        for _ in range(max(10, ROUNDS // 2)):
            child = Child(TOKEN_CHILD, str(self.dir))
            time.sleep(rng.uniform(0.12, 0.5))
            child.kill()
            last_ack = max(last_ack, child.acknowledged())
            saved = store.load()
            if last_ack:
                self.assertIsNotNone(saved, "the token file must always be present and parseable")
                token = saved["refresh_token"]
                self.assertTrue(token.startswith("token-") and token.endswith("x" * 2000) and len(token) == 6 + 8 + 1 + 2000, "complete token, never a fragment")
        leftovers = [p.name for p in self.dir.iterdir() if p.name != "token.json"]
        # a killed writer may leave its temp file behind, but it never replaces the real one
        for name in leftovers:
            self.assertTrue(name.startswith(".token.json.") and name.endswith(".tmp"), name)

    def test_backups_are_never_half_written(self):
        conn, _ = db.open_database(self.dir)
        for i in range(3000):
            conn.execute("INSERT INTO faults(mac, kind, source, fingerprint, first_seen_at, last_seen_at, raw) VALUES('m', 'event', 's', ?, 't', 't', ?)",
                         ("f%d" % i, "x" * 400))
        db.checkpoint_and_close(conn)
        rng = random.Random(99)
        for _ in range(max(8, ROUNDS // 3)):
            child = Child(BACKUP_CHILD, str(self.dir))
            time.sleep(rng.uniform(0.2, 0.8))
            child.kill()
            for backup in db.list_backups(self.dir / "backups"):
                check = sqlite3.connect(str(backup))
                try:
                    self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok", backup.name)
                    self.assertEqual(check.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 3000, backup.name)
                finally:
                    check.close()
        names = [p.name for p in (self.dir / "backups").iterdir()]
        self.assertTrue(all(n.startswith("bwwatch-") or n.startswith(".bwwatch-") for n in names), names)
        # and the real database was untouched by all of that
        conn, recovery = db.open_database(self.dir)
        try:
            self.assertIsNone(recovery)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 3000)
        finally:
            conn.close()

    def test_a_database_killed_mid_write_can_be_opened_by_a_plain_sqlite_client(self):
        """Not just by bwwatch: the file on disk is an ordinary, recoverable SQLite database."""
        child = Child(POLL_CHILD, str(self.dir), "1")
        time.sleep(0.6)
        child.kill()
        raw = sqlite3.connect(str(self.dir / "bwwatch.db"))
        try:
            self.assertEqual(raw.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertGreater(raw.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 0)
        finally:
            raw.close()


if __name__ == "__main__":
    unittest.main()
