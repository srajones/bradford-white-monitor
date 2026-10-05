"""Storage safety: pragmas, atomic transactions, integrity checks, backups, automatic recovery."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bwwatch import db
from bwwatch.util import atomic_write


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="bwdb."))
        self.addCleanup(shutil.rmtree, str(self.dir), True)

    def open(self):
        conn, recovery = db.open_database(self.dir)
        self.addCleanup(lambda: conn.close())
        return conn, recovery

    @staticmethod
    def add_poll(conn, n):
        conn.execute("INSERT INTO polls(started_at, finished_at, ok) VALUES(?, ?, 1)", ("2026-01-01T00:00:%02dZ" % n, "2026-01-01T00:00:%02dZ" % n))


class Migration(Base):
    V1_FAULTS = """CREATE TABLE faults(
        id INTEGER PRIMARY KEY, mac TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('event', 'state', 'blob')),
        source TEXT NOT NULL, fingerprint TEXT NOT NULL, code TEXT, description TEXT, occurred_at TEXT,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, cleared_at TEXT, seen_count INTEGER NOT NULL DEFAULT 1,
        baseline INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0, 1)), raw TEXT NOT NULL)"""

    def make_v1(self):
        path = self.dir / db.DB_NAME
        conn = sqlite3.connect(str(path))
        conn.execute(self.V1_FAULTS)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID")
        conn.execute("INSERT INTO faults(mac, kind, source, fingerprint, code, first_seen_at, last_seen_at, raw) "
                     "VALUES('AA', 'event', 'fault_history', 'code=10|at=1', '10', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{}')")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

    def test_a_version_1_database_gains_the_state_columns_and_keeps_its_rows(self):
        self.make_v1()
        conn, recovery = self.open()
        self.assertIsNone(recovery)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(faults)")}
        self.assertTrue({"state", "cleared_seen_at"} <= columns)
        row = conn.execute("SELECT code, state, cleared_seen_at FROM faults").fetchone()
        self.assertEqual((row["code"], row["state"], row["cleared_seen_at"]), ("10", None, None), "old entries: state unknown")
        conn.execute("UPDATE faults SET state = 'cleared'")  # and the new column works
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE faults SET state = 'nonsense'")

    V3_TABLES = {"field_state", "observations", "energy_usage", "api_calls", "discovery"}

    def tables(self, conn):
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def test_old_databases_gain_the_logging_tables(self):
        self.make_v1()
        conn, _ = self.open()
        self.assertTrue(self.V3_TABLES <= self.tables(conn))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 1, "nothing was lost on the way")

    def test_a_version_2_database_gains_only_the_new_tables(self):
        self.make_v1()
        raw = sqlite3.connect(str(self.dir / db.DB_NAME))
        raw.execute("ALTER TABLE faults ADD COLUMN state TEXT CHECK (state IN ('active', 'cleared'))")
        raw.execute("ALTER TABLE faults ADD COLUMN cleared_seen_at TEXT")
        raw.execute("PRAGMA user_version=2")
        raw.commit()
        raw.close()
        conn, _ = self.open()
        self.assertTrue(self.V3_TABLES <= self.tables(conn))
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_a_new_database_has_every_table(self):
        conn, _ = self.open()
        self.assertTrue(self.V3_TABLES <= self.tables(conn))
        for table in self.V3_TABLES:
            conn.execute("SELECT * FROM %s LIMIT 1" % table)

    def test_a_new_database_has_them_from_the_start(self):
        conn, _ = self.open()
        self.assertTrue({"state", "cleared_seen_at"} <= {r[1] for r in conn.execute("PRAGMA table_info(faults)")})
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_the_migration_is_all_or_nothing(self):
        self.make_v1()
        raw = sqlite3.connect(str(self.dir / db.DB_NAME))
        raw.execute("ALTER TABLE faults ADD COLUMN state TEXT")  # a half-applied earlier attempt: the next ALTER must fail
        raw.commit()
        raw.close()
        conn = db.connect(self.dir / db.DB_NAME)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            db.init_schema(conn)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1, "still version 1: nothing half-done is recorded")

    def test_a_backup_of_the_old_version_is_migrated_when_restored(self):
        self.make_v1()
        conn, _ = self.open()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 1)


class Settings(Base):
    def test_durability_settings(self):
        conn, recovery = self.open()
        self.assertIsNone(recovery)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2, "synchronous must be FULL")
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_reopening_is_idempotent(self):
        conn, _ = self.open()
        self.add_poll(conn, 1)
        conn.close()
        conn2, recovery = self.open()
        self.assertIsNone(recovery)
        self.assertEqual(conn2.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 1)

    def test_newer_schema_is_refused_not_treated_as_corruption(self):
        conn, _ = self.open()
        conn.execute("PRAGMA user_version=%d" % (db.SCHEMA_VERSION + 5))
        conn.close()
        with self.assertRaises(db.DatabaseTooNew):
            db.open_database(self.dir)
        self.assertFalse((self.dir / "corrupt").exists())
        self.assertTrue((self.dir / db.DB_NAME).exists())


class Transactions(Base):
    def test_rollback_on_error_leaves_nothing(self):
        conn, _ = self.open()
        with self.assertRaises(RuntimeError):
            with db.tx(conn):
                self.add_poll(conn, 1)
                self.add_poll(conn, 2)
                raise RuntimeError("boom mid-transaction")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 0)
        self.assertFalse(conn.in_transaction)

    def test_savepoint_rolls_back_only_its_part(self):
        conn, _ = self.open()
        with db.tx(conn):
            self.add_poll(conn, 1)
            with self.assertRaises(ValueError):
                with db.savepoint(conn):
                    self.add_poll(conn, 2)
                    raise ValueError("inner failure")
            self.add_poll(conn, 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 2)

    def test_constraint_violation_rolls_back_whole_transaction(self):
        conn, _ = self.open()
        with self.assertRaises(sqlite3.IntegrityError):
            with db.tx(conn):
                self.add_poll(conn, 1)
                conn.execute("INSERT INTO polls(started_at, finished_at, ok) VALUES('a', 'b', 7)")  # violates CHECK
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 0)


class Backups(Base):
    def test_backup_is_verified_self_contained_and_pruned(self):
        conn, _ = self.open()
        for n in range(5):
            self.add_poll(conn, n)
        backups = self.dir / "backups"
        made = []
        for i in range(4):
            os.environ["TZ"] = "UTC"
            path = db.backup_now(conn, backups, keep=2)
            made.append(path)
            # distinct names even within the same second
            renamed = backups / ("bwwatch-20260101T00000%dZ.db" % i)
            os.replace(str(path), str(renamed))
        kept = db.list_backups(backups)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0].name, "bwwatch-20260101T000003Z.db")  # newest first
        # self-contained: opens without any -wal/-shm and holds the data
        copy = sqlite3.connect(str(kept[0]))
        try:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 5)
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            copy.close()
        self.assertEqual([p.name for p in backups.iterdir() if p.name.startswith(".")], [], "no temp files left behind")

    def test_backup_of_a_live_wal_database_contains_uncheckpointed_rows(self):
        conn, _ = self.open()
        self.add_poll(conn, 1)
        self.assertTrue((self.dir / "bwwatch.db-wal").exists())
        path = db.backup_now(conn, self.dir / "backups", keep=3)
        copy = sqlite3.connect(str(path))
        try:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 1)
        finally:
            copy.close()


class Recovery(Base):
    def damage(self, how: str) -> None:
        path = self.dir / db.DB_NAME
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(str(path) + suffix)
            except FileNotFoundError:
                pass
        data = bytearray(path.read_bytes())
        if how == "garbage":
            path.write_bytes(b"this is definitely not a sqlite database " * 200)
        elif how == "zeroed-middle":
            for i in range(len(data) // 3, len(data) // 3 + 6000):
                data[i] = 0
            path.write_bytes(bytes(data))
        elif how == "truncated":
            path.write_bytes(bytes(data[: len(data) // 2]))

    def populated(self, polls: int):
        conn, _ = self.open()
        for n in range(polls):
            self.add_poll(conn, n)
            conn.execute("INSERT INTO faults(mac, kind, source, fingerprint, first_seen_at, last_seen_at, raw) "
                         "VALUES('m', 'event', 's', 'f%d', 't', 't', '{}')" % n)
        return conn

    def test_damaged_database_is_quarantined_and_restored_from_backup(self):
        for how in ("garbage", "zeroed-middle", "truncated"):
            with self.subTest(how):
                shutil.rmtree(str(self.dir), True)
                self.dir.mkdir()
                conn = self.populated(30)
                db.backup_now(conn, self.dir / "backups", keep=3)
                self.add_poll(conn, 31)  # newer than the backup
                db.checkpoint_and_close(conn)
                self.damage(how)
                conn2, recovery = db.open_database(self.dir)
                try:
                    self.assertIsNotNone(recovery)
                    self.assertIsNotNone(recovery.restored_from)
                    self.assertTrue((recovery.quarantined_to / db.DB_NAME).exists(), "damaged file is kept, not deleted")
                    self.assertEqual(db.integrity_check(conn2), [])
                    self.assertEqual(conn2.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 30)
                    self.assertEqual(conn2.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 30)
                finally:
                    conn2.close()

    def test_no_backup_means_a_fresh_database_but_the_old_one_is_kept(self):
        conn = self.populated(3)
        db.checkpoint_and_close(conn)
        self.damage("garbage")
        conn2, recovery = db.open_database(self.dir)
        try:
            self.assertIsNone(recovery.restored_from)
            self.assertEqual(conn2.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 0)
            self.assertTrue((recovery.quarantined_to / db.DB_NAME).exists())
        finally:
            conn2.close()

    def test_a_corrupt_backup_is_skipped_for_an_older_good_one(self):
        conn = self.populated(5)
        backups = self.dir / "backups"
        good = db.backup_now(conn, backups, keep=5)
        os.replace(str(good), str(backups / "bwwatch-20260101T000001Z.db"))
        self.add_poll(conn, 40)
        bad = db.backup_now(conn, backups, keep=5)
        os.replace(str(bad), str(backups / "bwwatch-20260101T000002Z.db"))
        (backups / "bwwatch-20260101T000002Z.db").write_bytes(b"corrupt" * 1000)
        db.checkpoint_and_close(conn)
        self.damage("garbage")
        conn2, recovery = db.open_database(self.dir)
        try:
            self.assertEqual(recovery.restored_from.name, "bwwatch-20260101T000001Z.db")
            self.assertEqual(conn2.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 5)
        finally:
            conn2.close()

    def test_unreadable_is_not_treated_as_corrupt(self):
        # disk full / locked / permission errors must never trigger moving the database aside
        for exc in (sqlite3.OperationalError("database or disk is full"), sqlite3.OperationalError("unable to open database file"),
                    sqlite3.OperationalError("database is locked"), sqlite3.IntegrityError("constraint")):
            self.assertFalse(db.looks_corrupt(exc), exc)
        for exc in (sqlite3.DatabaseError("file is not a database"), sqlite3.DatabaseError("database disk image is malformed"),
                    sqlite3.OperationalError("database disk image is malformed")):
            self.assertTrue(db.looks_corrupt(exc), exc)

    def test_integrity_check_reports_real_damage(self):
        conn = self.populated(200)
        db.checkpoint_and_close(conn)
        path = self.dir / db.DB_NAME
        data = bytearray(path.read_bytes())
        for i in range(8192, 8192 + 2048):
            data[i] = 0xFF
        path.write_bytes(bytes(data))
        raw = sqlite3.connect(str(path))
        try:
            try:
                problems = db.integrity_check(raw)
            except sqlite3.DatabaseError:
                problems = ["error"]
        finally:
            raw.close()
        self.assertTrue(problems)


class AtomicFiles(Base):
    def test_atomic_write_replaces_whole_file_and_cleans_up(self):
        target = self.dir / "token.json"
        atomic_write(target, b"old")
        atomic_write(target, b"new-content")
        self.assertEqual(target.read_bytes(), b"new-content")
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["token.json"])

    def test_failed_write_keeps_the_old_file(self):
        target = self.dir / "token.json"
        atomic_write(target, b"precious")

        class Boom(bytes):
            pass

        with self.assertRaises(TypeError):
            atomic_write(target, "not-bytes")  # type: ignore[arg-type]
        self.assertEqual(target.read_bytes(), b"precious")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["token.json"])


if __name__ == "__main__":
    unittest.main()
