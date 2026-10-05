"""SQLite storage: durable settings, schema, integrity checks, backups, recovery.

Why this is safe against unexpected shutdowns
---------------------------------------------
* WAL journal + ``synchronous=FULL``: a committed transaction is fsynced; a
  crash or power loss can lose an *uncommitted* transaction but never leaves the
  file half-written.
* Every poll is written in one ``BEGIN IMMEDIATE`` transaction, so a poll is
  either fully recorded or not recorded at all (and is simply redone next time).
* Integrity is verified at start-up and daily; backups are made with SQLite's
  online backup API (never by copying a live file), verified, then renamed into
  place atomically.
* If the database is ever found damaged it is moved aside (never deleted) and
  the newest verified backup is restored.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .util import fsync_dir, iso, utcnow

log = logging.getLogger("bwwatch.db")

DB_NAME = "bwwatch.db"
SCHEMA_VERSION = 3

SCHEMA: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS meta(
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS appliances(
        mac           TEXT PRIMARY KEY,
        name          TEXT,
        serial        TEXT,
        model         TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS polls(
        id          INTEGER PRIMARY KEY,
        started_at  TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        ok          INTEGER NOT NULL CHECK (ok IN (0, 1)),
        error       TEXT,
        appliances  INTEGER NOT NULL DEFAULT 0,
        events_seen INTEGER NOT NULL DEFAULT 0,
        new_faults  INTEGER NOT NULL DEFAULT 0
    )""",
    # The heater's reported settings at each poll (mode, setpoint, any temperature fields).
    """CREATE TABLE IF NOT EXISTS readings(
        id         INTEGER PRIMARY KEY,
        taken_at   TEXT NOT NULL,
        mac        TEXT NOT NULL,
        mode       TEXT,
        mode_value INTEGER,
        setpoint_f REAL,
        temps      TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS readings_latest ON readings(mac, id DESC)",
    # Raw API responses, stored only when their content changed since the last one.
    """CREATE TABLE IF NOT EXISTS snapshots(
        id       INTEGER PRIMARY KEY,
        taken_at TEXT NOT NULL,
        mac      TEXT NOT NULL,
        kind     TEXT NOT NULL CHECK (kind IN ('status', 'faults')),
        digest   TEXT NOT NULL,
        body     TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS snapshots_latest ON snapshots(mac, kind, id DESC)",
    # One row per fault. kind: event = an entry in the fault/notification history,
    # state = a fault flag seen in the status payload (open until it clears),
    # blob = a fault response we could not break into entries.
    # For an event, `state` says whether the entry is active or has cleared (NULL = it does not say), `cleared_at`
    # is the time the entry itself gives for that, and `cleared_seen_at` is when bwwatch saw an entry it had known as
    # active turn cleared (NULL for one that was already cleared the first time it was seen).
    """CREATE TABLE IF NOT EXISTS faults(
        id            INTEGER PRIMARY KEY,
        mac           TEXT NOT NULL,
        kind          TEXT NOT NULL CHECK (kind IN ('event', 'state', 'blob')),
        source        TEXT NOT NULL,
        fingerprint   TEXT NOT NULL,
        code          TEXT,
        description   TEXT,
        occurred_at   TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        cleared_at    TEXT,
        state         TEXT CHECK (state IN ('active', 'cleared')),
        cleared_seen_at TEXT,
        seen_count    INTEGER NOT NULL DEFAULT 1,
        baseline      INTEGER NOT NULL DEFAULT 0 CHECK (baseline IN (0, 1)),
        raw           TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS faults_event_fp ON faults(mac, fingerprint) WHERE kind IN ('event', 'blob')",
    "CREATE UNIQUE INDEX IF NOT EXISTS faults_state_open ON faults(mac, fingerprint) WHERE kind = 'state' AND cleared_at IS NULL",
    "CREATE INDEX IF NOT EXISTS faults_recent ON faults(first_seen_at)",
    # Notifications waiting to be delivered. Written in the same transaction as
    # the fault that caused them, so a recorded fault always has its alert queued.
    """CREATE TABLE IF NOT EXISTS outbox(
        id         INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        kind       TEXT NOT NULL,
        priority   INTEGER NOT NULL,
        title      TEXT NOT NULL,
        body       TEXT NOT NULL,
        fault_id   INTEGER REFERENCES faults(id),
        status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dropped')),
        attempts   INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        sent_at    TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(id) WHERE status = 'pending'",
)

# Added in schema 3 ("log everything we can read"). An older database gets exactly these on its next start.
SCHEMA_V3: Tuple[str, ...] = (
    # The latest value of every field the cloud has ever reported, per heater and source (list / status / ...).
    """CREATE TABLE IF NOT EXISTS field_state(
        mac             TEXT NOT NULL,
        source          TEXT NOT NULL,
        path            TEXT NOT NULL,
        value           TEXT,
        vtype           TEXT NOT NULL,
        first_seen_at   TEXT NOT NULL,
        last_changed_at TEXT NOT NULL,
        last_seen_at    TEXT NOT NULL,
        changes         INTEGER NOT NULL DEFAULT 0,
        present         INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (mac, source, path)
    ) WITHOUT ROWID""",
    # Every change of every field (and every time one appears or disappears): a complete, compact history.
    """CREATE TABLE IF NOT EXISTS observations(
        id        INTEGER PRIMARY KEY,
        taken_at  TEXT NOT NULL,
        mac       TEXT NOT NULL,
        source    TEXT NOT NULL,
        path      TEXT NOT NULL,
        event     TEXT NOT NULL CHECK (event IN ('start', 'new', 'changed', 'gone', 'back')),
        old_value TEXT,
        new_value TEXT,
        vtype     TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS observations_time ON observations(taken_at)",
    "CREATE INDEX IF NOT EXISTS observations_path ON observations(mac, source, path, id)",
    # Energy used per hour/day, split into heat pump and element: the clearest sign of what the heater is really doing.
    """CREATE TABLE IF NOT EXISTS energy_usage(
        mac              TEXT NOT NULL,
        view             TEXT NOT NULL,
        ts               TEXT NOT NULL,
        total_energy     REAL,
        heat_pump_energy REAL,
        element_energy   REAL,
        reported_minutes INTEGER,
        extra            TEXT,
        first_seen_at    TEXT NOT NULL,
        last_seen_at     TEXT NOT NULL,
        revisions        INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (mac, view, ts)
    ) WITHOUT ROWID""",
    # Every request bwwatch sent: what, when, the answer's status, how long it took and which headers came back.
    """CREATE TABLE IF NOT EXISTS api_calls(
        id       INTEGER PRIMARY KEY,
        taken_at TEXT NOT NULL,
        poll_id  INTEGER,
        kind     TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        status   INTEGER,
        ms       INTEGER,
        bytes    INTEGER,
        headers  TEXT,
        error    TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS api_calls_time ON api_calls(taken_at)",
    # The search for the Notifications request: every guess and what the server said (see discover.py).
    """CREATE TABLE IF NOT EXISTS discovery(
        id       INTEGER PRIMARY KEY,
        tried_at TEXT NOT NULL,
        key      TEXT NOT NULL UNIQUE,
        name     TEXT NOT NULL,
        method   TEXT NOT NULL,
        target   TEXT NOT NULL,
        body     TEXT,
        status   INTEGER,
        verdict  TEXT NOT NULL,
        note     TEXT,
        sample   TEXT,
        origin   TEXT NOT NULL DEFAULT 'builtin'
    )""",
)
SCHEMA = SCHEMA + SCHEMA_V3


class DatabaseTooNew(Exception):
    """The database was written by a newer bwwatch; refuse rather than guess."""


@dataclass
class Recovery:
    """What happened when a damaged database was found."""

    problem: str
    quarantined_to: Path
    restored_from: Optional[Path]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)  # we manage transactions
    conn.row_factory = sqlite3.Row
    mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
    if mode != "wal":
        # e.g. a filesystem without shared-memory support. Still crash-safe, just not WAL.
        log.warning("SQLite would not enter WAL mode (journal_mode=%s); continuing with that mode", mode)
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_size_limit=4194304")
    conn.execute("PRAGMA cell_size_check=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing write transaction (takes the write lock up front)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


@contextmanager
def savepoint(conn: sqlite3.Connection, name: str = "sp") -> Iterator[None]:
    """Nested all-or-nothing section inside :func:`tx`."""
    conn.execute("SAVEPOINT %s" % name)
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO %s" % name)
        conn.execute("RELEASE %s" % name)
        raise
    conn.execute("RELEASE %s" % name)


def init_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise DatabaseTooNew(
            "database schema v%d is newer than this bwwatch understands (v%d); upgrade bwwatch"
            % (version, SCHEMA_VERSION)
        )
    if version == SCHEMA_VERSION:
        return
    if version in (1, 2):  # older databases keep everything they have and gain what is new, in one transaction
        with tx(conn):
            if version == 1:  # 1 -> 2: events know whether they are active or cleared
                conn.execute("ALTER TABLE faults ADD COLUMN state TEXT CHECK (state IN ('active', 'cleared'))")
                conn.execute("ALTER TABLE faults ADD COLUMN cleared_seen_at TEXT")
            for statement in SCHEMA_V3:  # 2 -> 3: the logs of every field, energy use, requests and the search
                conn.execute(statement)
            conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        return
    with tx(conn):
        for statement in SCHEMA:
            conn.execute(statement)
        meta_set(conn, "created_at", iso())
        conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)


# --- meta key/value ---------------------------------------------------------
def meta_get(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --- integrity --------------------------------------------------------------
def integrity_check(conn: sqlite3.Connection, quick: bool = False) -> List[str]:
    """Empty list if the database is healthy, otherwise SQLite's complaints."""
    rows = [str(r[0]) for r in conn.execute("PRAGMA quick_check" if quick else "PRAGMA integrity_check")]
    return [] if rows == ["ok"] else rows


def looks_corrupt(exc: BaseException) -> bool:
    """Does this exception mean 'the file is damaged' (as opposed to disk full, locked, ...)?"""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    message = str(exc).lower()
    if any(word in message for word in ("malformed", "not a database", "corrupt")):
        return True
    # Python raises the bare DatabaseError class for SQLITE_CORRUPT / SQLITE_NOTADB;
    # disk-full, locked, read-only, cannot-open are OperationalError and friends.
    return type(exc) is sqlite3.DatabaseError


# --- backups ----------------------------------------------------------------
def list_backups(backups_dir: Path) -> List[Path]:
    """Backups, newest first (names embed a sortable UTC timestamp)."""
    if not backups_dir.is_dir():
        return []
    return sorted(backups_dir.glob("bwwatch-*.db"), key=lambda p: p.name, reverse=True)


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_now(conn: sqlite3.Connection, backups_dir: Path, keep: int) -> Path:
    """Write a verified, self-contained copy of the live database and prune old ones."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    for stale in backups_dir.glob(".bwwatch-*.tmp*"):  # left by a backup that was killed part-way
        try:
            if time.time() - stale.stat().st_mtime > 600:
                stale.unlink()
        except OSError:
            pass
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    final = backups_dir / ("bwwatch-%s.db" % stamp)
    tmp = backups_dir / (".%s.tmp" % final.name)
    if tmp.exists():
        tmp.unlink()
    target = sqlite3.connect(str(tmp), isolation_level=None)
    try:
        conn.backup(target)  # consistent snapshot, safe while the source is in use
        problems = integrity_check(target)
        if problems:
            raise sqlite3.DatabaseError("backup failed verification: %s" % "; ".join(problems[:3]))
        target.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal needed
    except BaseException:
        target.close()
        for leftover in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm"), Path(str(tmp) + "-journal")):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
        raise
    target.close()
    _fsync_file(tmp)
    os.replace(str(tmp), str(final))
    fsync_dir(backups_dir)
    for old in list_backups(backups_dir)[keep:]:
        try:
            old.unlink()
        except OSError as exc:
            log.warning("could not remove old backup %s: %s", old.name, exc)
    return final


# --- open / recover ---------------------------------------------------------
def quarantine(db_path: Path, corrupt_root: Path) -> Path:
    """Move a damaged database (and its -wal/-shm/-journal) aside. Nothing is deleted."""
    dest = corrupt_root / utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        src = db_path.with_name(db_path.name + suffix)
        if src.exists():
            shutil.move(str(src), str(dest / src.name))
    fsync_dir(dest)
    fsync_dir(db_path.parent)
    return dest


def restore_latest_backup(db_path: Path, backups_dir: Path) -> Optional[Path]:
    """Put the newest backup that passes verification in place of ``db_path``."""
    for candidate in list_backups(backups_dir):
        staging = db_path.with_name(db_path.name + ".restoring")
        try:
            shutil.copyfile(str(candidate), str(staging))
            check = sqlite3.connect(str(staging), isolation_level=None)
            try:
                problems = integrity_check(check)
            finally:
                check.close()
            if problems:
                log.error("backup %s failed verification (%s); trying an older one", candidate.name, problems[:1])
                staging.unlink()
                continue
            _fsync_file(staging)
            os.replace(str(staging), str(db_path))
            fsync_dir(db_path.parent)
            return candidate
        except (OSError, sqlite3.Error) as exc:
            log.error("could not restore from %s: %s", candidate.name, exc)
            try:
                staging.unlink()
            except OSError:
                pass
    return None


def open_database(data_dir: Path) -> Tuple[sqlite3.Connection, Optional[Recovery]]:
    """Open (creating if needed) the database, recovering automatically if it is damaged."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / DB_NAME
    backups = data_dir / "backups"
    existed = path.exists()
    problem: Optional[str] = None
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = connect(path)
        init_schema(conn)
        issues = integrity_check(conn)
        if not issues:
            return conn, None
        problem = "integrity_check reported: " + "; ".join(issues[:3])
    except DatabaseTooNew:
        if conn:
            conn.close()
        raise
    except sqlite3.DatabaseError as exc:
        if not looks_corrupt(exc):
            if conn:
                conn.close()
            raise  # disk full / locked / permissions: moving files around would only make it worse
        problem = "%s: %s" % (type(exc).__name__, exc)
    if conn:
        conn.close()
    if not existed:  # nothing to recover from; the failure was something else
        raise sqlite3.DatabaseError(problem)
    log.error("database is damaged (%s); recovering", problem)
    moved = quarantine(path, data_dir / "corrupt")
    restored = restore_latest_backup(path, backups)
    conn = connect(path)
    init_schema(conn)
    return conn, Recovery(problem or "unknown", moved, restored)


def checkpoint_and_close(conn: sqlite3.Connection) -> None:
    """Fold the WAL back into the main file and close (clean-shutdown path)."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        log.warning("final WAL checkpoint failed: %s", exc)
    finally:
        conn.close()


def table_counts(conn: sqlite3.Connection) -> List[Tuple[str, int]]:
    out = []
    for name in ("appliances", "polls", "readings", "snapshots", "faults", "outbox"):
        out.append((name, conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]))
    return out
