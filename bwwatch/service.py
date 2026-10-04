"""The long-running service: poll on a schedule, deliver alerts, keep the database safe."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from . import __version__
from .config import MIN_POLL_SECONDS, Config
from .cycle import FetchResult, Outcome, apply_cycle, deliver_outbox, enqueue, fetch
from .db import (
    Recovery,
    backup_now,
    checkpoint_and_close,
    integrity_check,
    looks_corrupt,
    meta_get,
    meta_set,
    open_database,
    tx,
)
from .errors import AuthError, WaveError
from .notify import Message, Notifier
from .privs import acquire_lock
from .util import atomic_write_json, hours_since, iso, scrub
from .wave import TokenManager, TokenStore, WaveApi, http_request

log = logging.getLogger("bwwatch.service")

STATUS_FILE = "status.json"
# After the sign-in server rejects us we stop calling it until a new `login` arrives,
# apart from one retry this often in case the rejection was a temporary glitch.
AUTH_RETRY_SECONDS = 6 * 3600
# Consecutive failures before the polling interval starts to stretch.
BACKOFF_AFTER_FAILURES = 3
BACKOFF_CAP_SECONDS = 1800.0


class Service:
    def __init__(self, cfg: Config, *, stop: Optional[threading.Event] = None, notifier: Optional[Notifier] = None):
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.notifier = notifier or Notifier(cfg)
        self.store = TokenStore(cfg.data_dir / "token.json")
        self.tokens = TokenManager(cfg, self.store)
        self.api = WaveApi(cfg, self.tokens, stop=self.stop)
        self.conn: Optional[sqlite3.Connection] = None
        self.started_at = iso()
        self.started_epoch = time.time()
        self.last_attempt_epoch: Optional[float] = None
        self.last_success_epoch: Optional[float] = None
        self.next_poll_epoch: Optional[float] = None  # when the loop will poll next (for `status` / `verify`)
        self.failures = 0
        self.last_error = ""
        self.pending = 0
        self._direct_alert_at = 0.0
        self._auth_failed_at: Optional[float] = None  # monotonic time of the last sign-in rejection
        self._auth_failed_stamp: Optional[int] = None  # token-file mtime when that happened

    # --- how often we talk to the Wave cloud --------------------------------
    def _token_stamp(self) -> Optional[int]:
        try:
            return self.store.path.stat().st_mtime_ns
        except OSError:
            return None

    def _enter_auth_cooldown(self) -> None:
        self._auth_failed_at = time.monotonic()
        self._auth_failed_stamp = self._token_stamp()

    def _auth_cooldown_active(self) -> bool:
        """True while we are waiting for the owner to run `login` again (see AUTH_RETRY_SECONDS)."""
        if self._auth_failed_at is None:
            return False
        if self._token_stamp() != self._auth_failed_stamp:  # a new login arrived
            self._auth_failed_at = None
            return False
        if time.monotonic() - self._auth_failed_at >= AUTH_RETRY_SECONDS:
            return False  # time for one more try
        return True

    def next_delay(self) -> float:
        """Seconds until the next poll: the interval, stretched after failures or a 429, never below the floor."""
        interval = float(self.cfg.interval)
        delay = interval
        if self.failures >= BACKOFF_AFTER_FAILURES:
            cap = max(interval, BACKOFF_CAP_SECONDS)
            delay = min(interval * (2 ** min(self.failures - 2, 20)), cap)
        asked = max(self.api.retry_after, self.tokens.retry_after)
        if asked > 0:
            delay = max(delay, asked)
        return max(delay, float(MIN_POLL_SECONDS))

    def _mark_poll_started(self) -> None:
        """Remember when we last contacted the cloud, so restarts cannot cause rapid repeat polls."""
        if self.conn is None:
            return
        try:
            meta_set(self.conn, "poll.started_epoch", "%.0f" % time.time())
        except sqlite3.Error as exc:
            log.warning("could not record the poll time: %s", exc)

    def seconds_until_poll_allowed(self) -> float:
        """Even right after a restart, never poll within MIN_POLL_SECONDS of the previous poll."""
        if self.conn is None:
            return 0.0
        try:
            last = float(meta_get(self.conn, "poll.started_epoch") or 0)
        except (ValueError, sqlite3.Error):
            return 0.0
        return min(float(MIN_POLL_SECONDS), max(0.0, last + MIN_POLL_SECONDS - time.time()))

    # --- lifecycle ----------------------------------------------------------
    def open(self) -> Optional[Recovery]:
        self.conn, recovery = open_database(self.cfg.data_dir)
        if recovery is not None:
            self._queue_recovery_alert(recovery)
        return recovery

    def startup(self, recovery: Optional[Recovery]) -> None:
        cfg = self.cfg
        for warning in cfg.warnings:
            log.warning(warning)
        if not self.notifier.channels:
            log.warning(
                "NO NOTIFICATION CHANNEL IS CONFIGURED: faults will be logged but nobody will be alerted. "
                "Set NTFY_TOPIC (or Telegram / email / webhook) in .env."
            )
        if cfg.fault_request is None:
            log.warning(
                "BW_FAULT_REQUEST is not set: the fault/notification history is not being read yet, "
                "only the settings and fault-like fields in the status data. See the README."
            )
        if not self.tokens.has_credentials():
            log.warning("not signed in yet: run  docker compose run --rm bwwatch login")
        assert self.conn is not None
        with tx(self.conn):
            if hours_since(meta_get(self.conn, "startup.notice_at")) >= 1:
                now = iso()
                enqueue(
                    self.conn,
                    kind="info",
                    title="bwwatch started",
                    body="bwwatch %s is running and polls every %d minutes.\nAlert channels: %s.\nFault history: %s."
                    % (
                        __version__,
                        max(1, cfg.interval // 60),
                        ", ".join(self.notifier.channels) or "NONE",
                        "configured" if cfg.fault_request else "NOT configured yet (BW_FAULT_REQUEST)",
                    ),
                    priority=2,
                    now=now,
                )
                meta_set(self.conn, "startup.notice_at", now)
        log.info(
            "bwwatch %s started: every %ds, channels=%s, fault request=%s",
            __version__,
            cfg.interval,
            ",".join(self.notifier.channels) or "none",
            cfg.fault_request.describe() if cfg.fault_request else "not set",
        )

    def shutdown(self) -> None:
        if self.conn is not None:
            self.write_status()
            checkpoint_and_close(self.conn)
            self.conn = None

    # --- one poll -----------------------------------------------------------
    def cycle(self) -> Outcome:
        started = iso()
        self.last_attempt_epoch = time.time()
        self._mark_poll_started()
        self.write_status()
        self.api.retry_after = 0.0
        self.tokens.retry_after = 0.0
        if self._auth_cooldown_active():
            log.warning("sign-in was rejected earlier; not contacting the sign-in server until a new `login` (or the 6-hour retry)")
            fetched = FetchResult(
                started_at=started,
                errors=[
                    "sign-in: still waiting for you to run `login` again (the sign-in server is not contacted "
                    "until then, apart from one retry every %d hours)" % (AUTH_RETRY_SECONDS // 3600)
                ],
                auth_error=True,
            )
        else:
            try:
                fetched = fetch(self.cfg, self.api)
                self._auth_failed_at = None
            except AuthError as exc:
                log.error("sign-in problem: %s", exc)
                self._enter_auth_cooldown()
                fetched = FetchResult(started_at=started, errors=["sign-in: %s" % scrub(exc, 300)], auth_error=True)
            except WaveError as exc:
                log.warning("poll failed: %s", exc)
                fetched = FetchResult(started_at=started, errors=[scrub(exc, 300)])
            except Exception as exc:  # noqa: BLE001 - a bug while fetching must not stop the monitor
                log.exception("unexpected error while fetching")
                fetched = FetchResult(started_at=started, errors=["internal error: %s: %s" % (type(exc).__name__, scrub(exc, 200))])
        outcome = self._record(fetched)
        if outcome.ok:
            self.failures = 0
            self.last_success_epoch = time.time()
            self.last_error = ""
        else:
            self.failures += 1
            self.last_error = outcome.error
        log.log(logging.INFO if outcome.ok else logging.WARNING, "poll %s: %s", "ok" if outcome.ok else "FAILED", outcome.summary())
        self.deliver()
        self.write_status()
        if outcome.ok:
            self.ping_heartbeat()
        return outcome

    def _record(self, fetched: FetchResult) -> Outcome:
        assert self.conn is not None
        try:
            return apply_cycle(self.conn, self.cfg, fetched)
        except sqlite3.Error as exc:
            return self._database_trouble(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected error while recording the poll")
            try:  # second chance: record just the failure so health alerting still works
                again = FetchResult(
                    started_at=fetched.started_at,
                    errors=["internal error while recording: %s: %s" % (type(exc).__name__, scrub(exc, 200))],
                )
                return apply_cycle(self.conn, self.cfg, again)
            except Exception as exc2:  # noqa: BLE001
                return self._database_trouble(exc2)

    def _database_trouble(self, exc: BaseException) -> Outcome:
        log.error("database error: %s", exc)
        self.direct_alert(
            "bwwatch cannot write its database",
            "Polls are not being logged: %s\nCheck free disk space on the server and the ./data folder." % scrub(exc, 200),
        )
        if looks_corrupt(exc):
            self.recover_database(str(exc))
        return Outcome(ok=False, error="database error: %s" % scrub(exc, 200))

    # --- alerts -------------------------------------------------------------
    def deliver(self) -> None:
        if self.conn is None:
            return
        try:
            self.pending = deliver_outbox(self.conn, self.notifier, self.cfg)
        except sqlite3.Error as exc:
            log.error("could not update the outbox: %s", exc)

    def direct_alert(self, title: str, body: str) -> None:
        """Alert without touching the database (for when the database is the problem). Throttled to hourly."""
        if time.monotonic() - self._direct_alert_at < 3600 and self._direct_alert_at:
            return
        self._direct_alert_at = time.monotonic()
        results = self.notifier.send(Message(title, body, 5, ("warning",)))
        if results and all(results.values()):
            log.error("could not deliver the alert either: %s", results)

    def _queue_recovery_alert(self, recovery: Recovery) -> None:
        assert self.conn is not None
        if recovery.restored_from is not None:
            outcome = (
                "Restored from backup %s. Anything logged after that backup is missing; faults still listed by the "
                "Wave cloud will be picked up again on the next poll." % recovery.restored_from.name
            )
        else:
            outcome = "No usable backup existed, so a fresh database was started."
        with tx(self.conn):
            enqueue(
                self.conn,
                kind="health",
                title="bwwatch database was damaged and has been recovered",
                body="The log database failed its integrity check (%s).\nThe damaged files were kept in %s.\n%s"
                % (scrub(recovery.problem, 200), recovery.quarantined_to, outcome),
                priority=5,
                now=iso(),
            )

    def recover_database(self, reason: str) -> None:
        log.error("re-opening the database to recover: %s", reason)
        try:
            if self.conn is not None:
                self.conn.close()
        except sqlite3.Error:
            pass
        self.conn = None
        self.conn, recovery = open_database(self.cfg.data_dir)
        if recovery is not None:
            self._queue_recovery_alert(recovery)

    # --- housekeeping -------------------------------------------------------
    def maintenance(self) -> None:
        """Daily integrity check and periodic verified backup."""
        conn = self.conn
        if conn is None:
            return
        try:
            if hours_since(meta_get(conn, "maint.integrity_at")) >= 24:
                problems = integrity_check(conn)
                meta_set(conn, "maint.integrity_at", iso())
                if problems:
                    self.recover_database("integrity_check: " + "; ".join(problems[:2]))
                    return
            if hours_since(meta_get(conn, "maint.backup_at")) >= self.cfg.backup_every_hours:
                path = backup_now(conn, self.cfg.data_dir / "backups", self.cfg.backup_keep)
                meta_set(conn, "maint.backup_at", iso())
                log.info("backup written: %s", path.name)
        except (sqlite3.Error, OSError) as exc:
            log.error("maintenance failed: %s", exc)
            self._maintenance_alert(exc)

    def _maintenance_alert(self, exc: BaseException) -> None:
        text = "Backup or integrity check failed: %s\nCheck free disk space on the server." % scrub(exc, 200)
        try:
            assert self.conn is not None
            if hours_since(meta_get(self.conn, "maint.alert_at")) < 24:
                return
            with tx(self.conn):
                now = iso()
                enqueue(self.conn, kind="health", title="bwwatch backup problem", body=text, priority=4, now=now)
                meta_set(self.conn, "maint.alert_at", now)
        except (sqlite3.Error, AssertionError):
            self.direct_alert("bwwatch backup problem", text)

    def ping_heartbeat(self) -> None:
        """Tell an external dead-man's-switch (e.g. healthchecks.io) that polling is alive."""
        if not self.cfg.heartbeat_url:
            return
        try:  # the URL is a secret (it identifies your check), so it is never shown in errors
            resp = http_request("GET", self.cfg.heartbeat_url, headers={"User-Agent": "bwwatch"}, timeout=10, label="heartbeat")
        except WaveError as exc:
            log.warning("heartbeat ping failed: %s", scrub(exc, 150))
            return
        if not 200 <= resp.status < 300:
            log.warning("heartbeat ping was not accepted (HTTP %d): is HEARTBEAT_URL correct?", resp.status)

    def write_status(self) -> None:
        data = {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "started_epoch": self.started_epoch,
            "updated_epoch": time.time(),
            "interval": self.cfg.interval,
            "last_attempt_epoch": self.last_attempt_epoch,
            "last_success_epoch": self.last_success_epoch,
            "next_poll_epoch": self.next_poll_epoch,
            "consecutive_failures": self.failures,
            "last_error": self.last_error,
            "pending_alerts": self.pending,
            "channels": list(self.notifier.channels),
            "fault_request": bool(self.cfg.fault_request),
        }
        try:
            atomic_write_json(self.cfg.data_dir / STATUS_FILE, data, mode=0o644)
        except OSError as exc:
            log.warning("could not write %s: %s", STATUS_FILE, exc)

    # --- main loop ----------------------------------------------------------
    def run(self) -> int:
        lock = acquire_lock(self.cfg.data_dir / "bwwatch.lock")
        try:
            recovery = self.open()
            self.startup(recovery)
            hold = self.seconds_until_poll_allowed()
            if hold > 0:
                log.info("the last poll was under %d minutes ago; waiting %d s before polling again", MIN_POLL_SECONDS // 60, hold)
            next_poll = time.monotonic() + hold
            self.next_poll_epoch = time.time() + hold
            self.write_status()
            retry_at: Optional[float] = None
            while not self.stop.is_set():
                now = time.monotonic()
                if now >= next_poll:
                    began = now
                    self.cycle()
                    self.maintenance()
                    delay = self.next_delay()
                    if delay > self.cfg.interval:
                        log.warning("next poll in %d s (stretched because of recent failures or a rate-limit request)", delay)
                    next_poll = max(began + delay, time.monotonic() + 5)
                    self.next_poll_epoch = time.time() + max(0.0, next_poll - time.monotonic())
                    retry_at = time.monotonic() + self.cfg.notify_retry_seconds if self.pending else None
                elif self.pending:
                    if retry_at is None:
                        retry_at = now + self.cfg.notify_retry_seconds
                    elif now >= retry_at:
                        self.deliver()
                        self.write_status()
                        retry_at = time.monotonic() + self.cfg.notify_retry_seconds if self.pending else None
                wait = next_poll - time.monotonic()
                if self.pending and retry_at is not None:
                    wait = min(wait, retry_at - time.monotonic())
                self.stop.wait(max(0.5, min(wait, 60.0)))
                if not self.stop.is_set():
                    self.write_status()
            log.info("stop requested; shutting down cleanly")
            return 0
        finally:
            self.shutdown()
            lock.close()


def healthcheck(data_dir: Path, now: Optional[float] = None) -> Tuple[bool, str]:
    """Is the service alive and polling? Used by the Docker healthcheck."""
    path = Path(data_dir) / STATUS_FILE
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "no status file yet - is the service running?"
    except (OSError, ValueError) as exc:
        return False, "status file unreadable: %s" % exc
    now = time.time() if now is None else now
    interval = float(status.get("interval") or 3600)
    idle = now - float(status.get("updated_epoch") or 0)
    if idle > 900:
        return False, "the service loop has been silent for %d seconds" % idle
    last_attempt = status.get("last_attempt_epoch") or status.get("started_epoch") or 0
    if now - float(last_attempt) > 2 * interval + 300:
        return False, "no poll has been attempted for %d seconds" % (now - float(last_attempt))
    reference = status.get("last_success_epoch") or status.get("started_epoch") or 0
    if now - float(reference) > 3 * interval + 600:
        return False, "no successful poll for %d seconds (last error: %s)" % (now - float(reference), status.get("last_error") or "none")
    return True, "ok (%d consecutive failed poll(s))" % int(status.get("consecutive_failures") or 0)
