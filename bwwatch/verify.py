"""`bwwatch verify`: is the installation actually working, end to end?

Used by the installer after start-up (and by ``install.sh --check`` any time). It looks at what the
running service has really done - polled the cloud, read the heater, delivered alerts, written
backups - rather than just whether a process exists.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .db import DB_NAME, connect, init_schema, integrity_check, list_backups
from .readings import reading_from_row
from .service import STATUS_FILE, healthcheck
from .util import iso, local_time, truncate

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def _snapshot(cfg: Config, since: Optional[float] = None) -> Dict[str, Any]:
    """Everything verify needs, read from the data folder in one go.

    With ``since`` (an epoch time) it also records whether the service and a poll are newer than that moment -
    the installer uses it right after starting the service, so leftovers from an earlier run cannot pass for it.
    """
    alive, alive_msg = healthcheck(cfg.data_dir)
    snap: Dict[str, Any] = {"alive": alive, "alive_msg": alive_msg, "db": False, "status": None}
    try:
        snap["status"] = json.loads((cfg.data_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if since is not None:
        status = snap["status"] or {}
        snap["fresh_service"] = float(status.get("started_epoch") or 0) >= since
    path = cfg.data_dir / DB_NAME
    if not path.exists():
        return snap
    conn = connect(path)
    try:
        init_schema(conn)
        snap["db"] = True
        snap["integrity"] = integrity_check(conn)
        snap["journal"] = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        snap["polls"] = conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0]
        snap["last_poll"] = conn.execute("SELECT * FROM polls ORDER BY id DESC LIMIT 1").fetchone()
        snap["ok_polls"] = conn.execute("SELECT COUNT(*) FROM polls WHERE ok = 1").fetchone()[0]
        if since is not None:
            cutoff = iso(datetime.fromtimestamp(since, tz=timezone.utc))
            snap["fresh_polls"] = conn.execute("SELECT COUNT(*) FROM polls WHERE finished_at >= ?", (cutoff,)).fetchone()[0]
        snap["heaters"] = []
        for row in conn.execute("SELECT * FROM appliances ORDER BY name"):
            reading = conn.execute("SELECT * FROM readings WHERE mac = ? ORDER BY id DESC LIMIT 1", (row["mac"],)).fetchone()
            snap["heaters"].append((row["name"] or row["mac"], reading_from_row(reading).summary() if reading else None))
        counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status")}
        snap["outbox"] = counts
        snap["last_outbox_error"] = conn.execute(
            "SELECT last_error FROM outbox WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        snap["backups"] = len(list_backups(cfg.data_dir / "backups"))
    finally:
        conn.close()
    return snap


def _has_news(snap: Dict[str, Any], since: Optional[float]) -> bool:
    """Has a poll finished (since the restart, when ``since`` is given)?"""
    return bool(snap.get("fresh_polls") if since is not None else snap.get("polls"))


def run_checks(
    cfg: Config,
    wait: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    since: Optional[float] = None,
    progress: Optional[Callable[[str], None]] = None,
    now: Callable[[], float] = time.time,
) -> List[Check]:
    """Run every check; with ``wait`` > 0, give a just-started service that long to complete its first poll."""
    began = clock()
    deadline = began + max(0.0, wait)
    seen_poll_at: Optional[float] = None
    last_said = began
    snap = _snapshot(cfg, since)
    while True:
        if _has_news(snap, since) and seen_poll_at is None:
            seen_poll_at = clock()
        # done waiting once the first poll exists and its alerts have had a few seconds to go out
        pending = (snap.get("outbox") or {}).get("pending", 0)
        settled = seen_poll_at is not None and (pending == 0 or clock() - seen_poll_at >= 15)
        if settled or clock() >= deadline:
            break
        if progress is not None and clock() - last_said >= 10:
            last_said = clock()
            if since is not None and not snap.get("fresh_service"):
                progress("waiting for the service to start (%d s)" % (clock() - began))
            elif seen_poll_at is None:
                hold = max(0.0, float((snap.get("status") or {}).get("next_poll_epoch") or 0) - now())
                progress("waiting for the first check of your water heater (%d s)%s" % (
                    clock() - began, "; the safeguard holds the next poll for %d s more" % hold if hold > 1 else ""))
            else:
                progress("the first check is done; sending alerts (%d s)" % (clock() - began))
        sleep(2.0)
        snap = _snapshot(cfg, since)
    return _evaluate(cfg, snap, since, now())


def _evaluate(cfg: Config, snap: Dict[str, Any], since: Optional[float] = None, now: float = 0.0) -> List[Check]:
    checks: List[Check] = []
    log_hint = "See the log:  ./bwctl logs"
    restarted = since is None or snap.get("fresh_service")

    if since is not None and not restarted:
        checks.append(Check(FAIL, "Service", "the service has not started since the installer started it",
                            "Look at  ./bwctl logs  for why it stopped (a settings error is the usual reason)"))
    elif snap["alive"]:
        checks.append(Check(OK, "Service", "running - %s" % snap["alive_msg"]))
    else:
        checks.append(Check(FAIL, "Service", snap["alive_msg"], "Start it with  ./bwctl start  and look at  ./bwctl logs"))

    last = snap.get("last_poll")
    polled = snap.get("db") and last is not None and _has_news(snap, since)
    held_back = False
    if not polled:
        hold = max(0.0, float((snap.get("status") or {}).get("next_poll_epoch") or 0) - now) if restarted and now else 0.0
        held_back = hold > 1 and since is not None
        if held_back:
            checks.append(Check(WARN, "First poll", "held back for %d more seconds" % hold,
                                "That is the safeguard that never polls Bradford White within 5 minutes of the previous poll, "
                                "even after a restart. It will run by itself; check later with  ./install.sh --check"))
        else:
            checks.append(Check(FAIL, "First poll", "no poll has completed yet",
                                "Give it a minute and run  ./install.sh --check  again. " + log_hint))
    elif last["ok"]:
        checks.append(Check(OK, "Wave cloud", "last poll succeeded at %s" % local_time(last["finished_at"], cfg.display_tz)))
    else:
        error = truncate(last["error"] or "unknown error", 200)
        fix = "Look at the log:  ./bwctl logs"
        if "sign-in" in error.lower() or "login" in error.lower():
            fix = "Sign in again:  ./bwctl login"
        elif "403" in error:
            fix = "Wave refused this server. See the README, 'How often it contacts Bradford White' (VPS addresses)."
        checks.append(Check(FAIL, "Wave cloud", "last poll FAILED: %s" % error, fix))

    heaters = snap.get("heaters") or []
    if not heaters:
        checks.append(Check(WARN if last is not None or held_back else FAIL, "Water heater", "none found yet"))
    for name, summary in heaters:
        if summary and "no mode/temperature" not in summary:
            checks.append(Check(OK, "Water heater", "%s: %s" % (name, summary)))
        else:
            checks.append(Check(WARN, "Water heater", "%s: the status response had no mode or setpoint" % name))

    if cfg.channel_names:
        checks.append(Check(OK, "Alert channels", ", ".join(cfg.channel_names)))
    else:
        checks.append(Check(FAIL, "Alert channels", "none configured - nothing could alert you", "Run  ./install.sh --reconfigure"))

    outbox = snap.get("outbox") or {}
    sent, pending, dropped = outbox.get("sent", 0), outbox.get("pending", 0), outbox.get("dropped", 0)
    if sent:
        checks.append(Check(OK, "Alerts delivered", "%d delivered so far" % sent))
    elif pending:
        err = snap.get("last_outbox_error")
        checks.append(Check(WARN, "Alerts delivered", "%d waiting - last error: %s" % (pending, truncate(err[0] if err else "?", 160)),
                            "Test your channels:  ./bwctl test-notify"))
    elif dropped:
        checks.append(Check(WARN, "Alerts delivered", "%d alert(s) were dropped (no channel wanted them)" % dropped, "Check your channel settings"))
    else:
        checks.append(Check(WARN, "Alerts delivered", "none sent yet (the first ones go out after the first poll)"))

    if cfg.fault_request is not None:
        checks.append(Check(OK, "Fault history", "reading %s" % cfg.fault_request.describe()))
    else:
        checks.append(Check(WARN, "Fault history", "not configured: only settings and status flags are watched",
                            "Add the Notifications request - README, 'Finding the fault request'"))

    if not snap.get("db"):
        checks.append(Check(FAIL, "Database", "not created yet"))
    elif snap.get("integrity"):
        checks.append(Check(FAIL, "Database", "integrity check reported problems: %s" % "; ".join(snap["integrity"][:2])))
    else:
        checks.append(Check(OK if snap.get("journal") == "wal" else WARN, "Database",
                            "intact, journal mode %s, %d poll(s) recorded" % (snap.get("journal"), snap.get("polls", 0))))

    backups = snap.get("backups", 0)
    checks.append(Check(OK if backups else WARN, "Backups", "%d verified backup(s)" % backups if backups else "none yet (the first is made after the first poll)"))

    if cfg.heartbeat_url:
        checks.append(Check(OK, "Dead-man's switch", "configured (pinged after each good poll)"))
    return checks


def format_checks(checks: List[Check]) -> str:
    tag = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    lines = []
    width = max(len(c.name) for c in checks)
    for c in checks:
        lines.append("  %s %-*s  %s" % (tag[c.status], width, c.name, c.detail))
        if c.fix and c.status != OK:
            lines.append("         %-*s  -> %s" % (width, "", c.fix))
    return "\n".join(lines)


def overall_ok(checks: List[Check]) -> bool:
    return not any(c.status == FAIL for c in checks)
