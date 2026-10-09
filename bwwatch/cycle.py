"""One poll: fetch from the Wave cloud (network only), then record it (database only).

Splitting the two keeps the important part - :func:`apply_cycle` - a pure function of
(database, configuration, what was fetched), written inside ONE transaction. The
transaction records the poll, any new faults *and* the notifications they require, so
a fault that is on disk always has its alert queued, and a crash mid-poll simply
means the poll is redone.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import observe
from .config import Config, RequestSpec
from .db import meta_get, meta_set, savepoint, tx
from .errors import AuthError, WaveError
from .faults import FaultEvent, digest, extract_events, find_active_lists, normalize_event, scan_state
from .notify import Message, Notifier
from .readings import extract_reading, reading_changes, reading_from_row
from .util import hours_since, iso, local_time, parse_iso, scrub, truncate
from .wave import CallRecord, WaveApi

log = logging.getLogger("bwwatch.cycle")

ACCOUNT = "account"  # pseudo-appliance for fault requests that are not per-heater
MAX_SNAPSHOT_CHARS = 262144
OUTBOX_MAX_AGE_DAYS = 7
TAGS = {
    "fault": ("rotating_light",),
    "health": ("warning",),
    "recovered": ("white_check_mark",),
    "cleared": ("white_check_mark",),
    "info": ("information_source",),
    "setting": ("gear",),
}


# --- fetching (network only) ------------------------------------------------
@dataclass
class ApplianceData:
    mac: str
    name: str
    serial: str
    model: str
    listing: Dict[str, Any]
    status: Any = None
    status_fetched: bool = False
    faults: Any = None
    faults_fetched: bool = False
    energy_hourly: Any = None
    energy_hourly_fetched: bool = False
    energy_daily: Any = None
    energy_daily_fetched: bool = False
    errors: List[str] = field(default_factory=list)
    extras: Dict[int, Any] = field(default_factory=dict)  # further answers to log field by field, by number


@dataclass
class FetchResult:
    started_at: str
    appliances: List[ApplianceData] = field(default_factory=list)
    account_faults: Any = None
    account_faults_fetched: bool = False
    errors: List[str] = field(default_factory=list)
    auth_error: bool = False
    calls: List[CallRecord] = field(default_factory=list)  # every request this poll made, for the request log

    def all_errors(self) -> List[str]:
        out = list(self.errors)
        for appliance in self.appliances:
            out.extend("%s: %s" % (appliance.name, err) for err in appliance.errors)
        return out


def mac_of(item: Dict[str, Any]) -> Optional[str]:
    for key in ("macAddress", "mac_address", "mac"):
        value = item.get(key)
        if value:
            return str(value)
    return None


def _try(errors: List[str], label: str, fn: Callable[[], Any]) -> Tuple[bool, Any]:
    try:
        return True, fn()
    except AuthError:
        raise
    except WaveError as exc:
        errors.append("%s: %s" % (label, scrub(exc, 300)))
        return False, None


def fetch(
    cfg: Config,
    api: WaveApi,
    *,
    resolve_fault: Optional[Callable[["ApplianceData"], Optional[Any]]] = None,
) -> FetchResult:
    """Read the appliance list, each heater's status and (if configured or learned) the fault history.

    ``resolve_fault``, given the first heater, may return a request to use when ``BW_FAULT_REQUEST``
    is unset (the service uses it to try ``/wave/getApplianceErrors``). ``check`` does not pass one,
    so it still writes nothing and does not guess.
    """
    result = FetchResult(started_at=iso())
    items = api.list_appliances()  # signs in first; AuthError / TransientError propagate
    if not items:
        result.errors.append("the account has no appliances")
    prepared: List[ApplianceData] = []
    for item in items:
        mac = mac_of(item)
        if not mac:
            result.errors.append("an appliance in the list has no macAddress")
            continue
        prepared.append(ApplianceData(
            mac=mac,
            name=str(item.get("friendlyName") or mac),
            serial=str(item.get("serialNumber") or ""),
            model=str(item.get("applianceType") or ""),
            listing=item,
        ))
    fault_spec = cfg.fault_request
    if fault_spec is None and resolve_fault is not None and prepared:
        try:
            fault_spec = resolve_fault(prepared[0])
        except AuthError:
            raise
        except WaveError as exc:
            result.errors.append("fault request: %s" % scrub(exc, 300))
    if fault_spec is not None and not fault_spec.per_appliance:
        ok, payload = _try(result.errors, "fault request", lambda: api.call(fault_spec))
        result.account_faults, result.account_faults_fetched = payload, ok
    for data in prepared:
        ctx = {"mac": data.mac, "serial": data.serial, "name": data.name}
        if cfg.status_request is not None:
            spec = cfg.status_request
            data.status_fetched, data.status = _try(data.errors, "status", lambda: api.call(spec, ctx))
        if fault_spec is not None and fault_spec.per_appliance:
            data.faults_fetched, data.faults = _try(data.errors, "fault request", lambda: api.call(fault_spec, ctx))
        if getattr(cfg, "log_energy", True):
            hourly_spec = RequestSpec("POST", "/wave/getEnergyUsage", {"mac_address": data.mac, "view_type": "hourly"})
            data.energy_hourly_fetched, data.energy_hourly = _try(
                [], "energy usage (hourly)", lambda: api.call(hourly_spec, ctx)
            )
            daily_spec = RequestSpec("POST", "/wave/getEnergyUsage", {"mac_address": data.mac, "view_type": "daily"})
            data.energy_daily_fetched, data.energy_daily = _try(
                [], "energy usage (daily)", lambda: api.call(daily_spec, ctx)
            )
        result.appliances.append(data)
    return result


# --- messages ---------------------------------------------------------------
@dataclass
class NewFault:
    fault_id: int
    mac: str
    name: str
    kind: str  # event | state | blob
    code: Optional[str]
    description: Optional[str]
    occurred_at: Optional[str]
    note: str = ""
    state: Optional[str] = None  # "active" / "cleared" when the entry says so
    cleared_at: Optional[str] = None
    again: bool = False  # an entry we had seen cleared is active again


def _who(name: str, mac: str) -> str:
    return "%s (%s)" % (name, mac) if name and name != mac else mac


def fault_alert(cfg: Config, fault: NewFault, detected_at: str) -> Tuple[str, str]:
    if fault.kind == "blob":
        title = "Wave fault data changed — %s" % fault.name
        body = [
            "The fault/notification response changed, and bwwatch could not split it into entries.",
            "Appliance: %s" % _who(fault.name, fault.mac),
            "Detected: %s" % local_time(detected_at, cfg.display_tz),
            "See the stored raw response:  docker compose exec bwwatch bwwatch faults --raw",
        ]
        if fault.description:
            body.insert(1, truncate(fault.description, 300))
        return title, "\n".join(body)
    tag = " (cleared)" if fault.state == "cleared" else " (active again)" if fault.again else ""
    if fault.code:
        title = "Water heater fault %s%s — %s" % (truncate(fault.code, 40), tag, fault.name)
    else:
        title = "Water heater fault alert%s — %s" % (tag, fault.name)
    lines = ["Appliance: %s" % _who(fault.name, fault.mac)]
    if fault.code:
        lines.append("Fault code: %s" % fault.code)
    if fault.description:
        lines.append("Detail: %s" % truncate(fault.description, 300))
    if fault.occurred_at:
        lines.append("Reported: %s" % local_time(fault.occurred_at, cfg.display_tz))
    if fault.state == "cleared":
        lines.append("Status: cleared%s - it had already cleared by the time bwwatch checked."
                     % (" at %s" % local_time(fault.cleared_at, cfg.display_tz) if fault.cleared_at else ""))
    elif fault.again:
        lines.append("Status: ACTIVE again - it had cleared earlier.")
    elif fault.state == "active":
        lines.append("Status: active")
    lines.append("Detected: %s" % local_time(detected_at, cfg.display_tz))
    if fault.note:
        lines.append(fault.note)
    return title, "\n".join(lines)


def _span(start: Optional[str], end: Optional[str]) -> Optional[str]:
    """'42 minutes' / '2 h 5 min' between two of our timestamps, or None if either is missing or not a stamp."""
    try:
        seconds = (parse_iso(end or "") - parse_iso(start or "")).total_seconds()
    except ValueError:
        return None
    if seconds < 0:
        return None
    minutes = int(round(seconds / 60.0))
    if minutes < 1:
        return "under a minute"
    if minutes < 120:
        return "%d minute%s" % (minutes, "" if minutes == 1 else "s")
    return "%d h %02d min" % (minutes // 60, minutes % 60)


def cleared_alert(cfg: Config, name: str, mac: str, row: sqlite3.Row, cleared_at: Optional[str], detected_at: str) -> Tuple[str, str]:
    """The alert for a fault that was active when bwwatch last looked and has now cleared."""
    code = row["code"]
    title = "Water heater fault %s cleared — %s" % (truncate(code, 40), name) if code else "Water heater fault cleared — %s" % name
    lines = ["Appliance: %s" % _who(name, mac)]
    if code:
        lines.append("Fault code: %s" % code)
    if row["description"]:
        lines.append("Detail: %s" % truncate(row["description"], 300))
    began = row["occurred_at"] or row["first_seen_at"]
    lines.append("Began: %s" % local_time(began, cfg.display_tz))
    if cleared_at:
        lines.append("Cleared: %s" % local_time(cleared_at, cfg.display_tz))
        lasted = _span(began, cleared_at)
        if lasted:
            lines.append("It lasted about %s." % lasted)
    else:
        lines.append("Cleared: some time between %s and %s (bwwatch only notices on its checks)."
                     % (local_time(row["last_seen_at"], cfg.display_tz), local_time(detected_at, cfg.display_tz)))
    lines.append("Noticed: %s" % local_time(detected_at, cfg.display_tz))
    return title, "\n".join(lines)


def enqueue(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    body: str,
    priority: int,
    now: str,
    fault_id: Optional[int] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO outbox(created_at, kind, priority, title, body, fault_id) VALUES(?,?,?,?,?,?)",
        (now, kind, priority, truncate(title, 200), truncate(body, 3000), fault_id),
    )
    return int(cur.lastrowid)


# --- recording (database only) ---------------------------------------------
@dataclass
class Outcome:
    ok: bool = False
    error: str = ""
    poll_id: int = 0
    appliances: int = 0
    events_seen: int = 0
    new_faults: List[NewFault] = field(default_factory=list)
    cleared: int = 0
    setting_changes: int = 0
    baselines: int = 0
    field_changes: int = 0

    def summary(self) -> str:
        return "%d appliance(s), %d fault entr%s known, %d new fault(s), %d setting change(s)%s" % (
            self.appliances,
            self.events_seen,
            "y" if self.events_seen == 1 else "ies",
            len(self.new_faults),
            self.setting_changes,
            "" if self.ok else " - ERRORS: " + self.error,
        )


def _upsert_appliance(conn: sqlite3.Connection, a: ApplianceData, now: str) -> None:
    conn.execute(
        """INSERT INTO appliances(mac, name, serial, model, first_seen_at, last_seen_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(mac) DO UPDATE SET name = excluded.name, serial = excluded.serial,
               model = excluded.model, last_seen_at = excluded.last_seen_at""",
        (a.mac, a.name, a.serial, a.model, now, now),
    )


def _snapshot(conn: sqlite3.Connection, mac: str, kind: str, payload: Any, volatile: Any, now: str) -> bool:
    """Store the raw response if (ignoring volatile keys) it differs from the last one."""
    fingerprint = digest(payload, volatile)
    last = conn.execute(
        "SELECT digest FROM snapshots WHERE mac = ? AND kind = ? ORDER BY id DESC LIMIT 1", (mac, kind)
    ).fetchone()
    if last is not None and last["digest"] == fingerprint:
        return False
    body = json.dumps(payload, ensure_ascii=False, default=str)[:MAX_SNAPSHOT_CHARS]
    conn.execute(
        "INSERT INTO snapshots(taken_at, mac, kind, digest, body) VALUES(?,?,?,?,?)",
        (now, mac, kind, fingerprint, body),
    )
    return True


def _record_calls(conn: sqlite3.Connection, calls: List[CallRecord], poll_id: int) -> None:
    """The request log: what was asked, how the server answered and how long it took (never the query string)."""
    conn.executemany(
        "INSERT INTO api_calls(taken_at, poll_id, kind, endpoint, status, ms, bytes, headers, error) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (c.at, poll_id, c.kind, c.endpoint, c.status, c.ms, c.size, json.dumps(c.headers, sort_keys=True) if c.headers else None, c.error)
            for c in calls
        ],
    )


def _record_fields(
    conn: sqlite3.Connection, cfg: Config, a: ApplianceData, now: str, outcome: Outcome, notes: List[str]
) -> List[observe.Change]:
    """Log every field of everything the cloud said about this heater; returns the changes worth looking at."""
    volatile = cfg.fault_options.volatile
    news: List[observe.Change] = []

    def one(source: str, payload: Any, **kwargs: Any) -> None:
        changes, first = observe.record(conn, a.mac, source, payload, now, volatile, **kwargs)
        if first:
            if source == "status" and changes:
                notes.append("Logging every field of the status answer (%d so far): ./bwctl fields lists them, ./bwctl changes shows what changed." % len(changes))
            return
        outcome.field_changes += len(changes)
        news.extend(changes)

    one("list", a.listing)
    if a.status_fetched:
        one("status", a.status)
    if a.faults_fetched:
        one("faults", a.faults, skip_lists=True)
    for number, payload in sorted(a.extras.items()):
        one("extra%d" % number, payload)
    return news


def _watch_alert(conn: sqlite3.Connection, cfg: Config, a: ApplianceData, changes: List[observe.Change], now: str) -> None:
    """BW_WATCH_FIELDS: tell the owner when a field they care about changes."""
    if cfg.watch_fields is None:
        return
    hits = [c for c in changes if cfg.watch_fields.search(c.name)]
    if not hits:
        return
    lines = [
        "%s: %s -> %s%s" % (c.name, c.old if c.old is not None else "(not there)", c.new if c.new is not None else "(gone)",
                            "" if c.event == "changed" else "   [%s]" % c.event)
        for c in hits[:12]
    ]
    if len(hits) > 12:
        lines.append("... and %d more" % (len(hits) - 12))
    enqueue(conn, kind="setting", title="Wave field changed — %s" % a.name,
            body="%s\nNoticed: %s" % ("\n".join(lines), local_time(now, cfg.display_tz)), priority=3, now=now)


def _record_status(conn: sqlite3.Connection, cfg: Config, a: ApplianceData, now: str, outcome: Outcome, notes: List[str]) -> None:
    reading = extract_reading(a.status)
    if reading is None:
        return
    previous = conn.execute("SELECT * FROM readings WHERE mac = ? ORDER BY id DESC LIMIT 1", (a.mac,)).fetchone()
    conn.execute(
        "INSERT INTO readings(taken_at, mac, mode, mode_value, setpoint_f, temps) VALUES(?,?,?,?,?,?)",
        (now, a.mac, reading.mode, reading.mode_value, reading.setpoint_f, json.dumps(reading.temps) if reading.temps else None),
    )
    if previous is None:
        notes.append("Current settings: %s." % reading.summary())
        return
    if cfg.notify_status_changes:
        changes = reading_changes(reading_from_row(previous), reading)
        if changes:
            outcome.setting_changes += 1
            enqueue(
                conn,
                kind="setting",
                title="Water heater setting changed — %s" % a.name,
                body="%s\nNow: %s\nNoticed: %s" % ("\n".join(changes), reading.summary(), local_time(now, cfg.display_tz)),
                priority=3,
                now=now,
            )


def _record_energy(conn: sqlite3.Connection, mac: str, view: str, payload: Any, now: str) -> int:
    if not isinstance(payload, list):
        return 0
    count = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        ts = item.get("timestamp") or item.get("ts")
        if not ts:
            continue
        total = item.get("total_energy")
        hp = item.get("heat_pump_energy")
        el = item.get("element_energy")
        mins = item.get("reported_minutes")
        known = {"timestamp", "ts", "total_energy", "heat_pump_energy", "element_energy", "reported_minutes"}
        extra = {k: v for k, v in item.items() if k not in known}
        extra_json = json.dumps(extra, sort_keys=True) if extra else None

        conn.execute(
            """INSERT INTO energy_usage(mac, view, ts, total_energy, heat_pump_energy, element_energy,
                                       reported_minutes, extra, first_seen_at, last_seen_at, revisions)
               VALUES(?,?,?,?,?,?,?,?,?,?,0)
               ON CONFLICT(mac, view, ts) DO UPDATE SET
                 total_energy = excluded.total_energy,
                 heat_pump_energy = excluded.heat_pump_energy,
                 element_energy = excluded.element_energy,
                 reported_minutes = excluded.reported_minutes,
                 extra = excluded.extra,
                 last_seen_at = excluded.last_seen_at,
                 revisions = revisions + 1""",
            (mac, view, str(ts), total, hp, el, mins, extra_json, now, now),
        )
        count += 1
    return count


def describe_event(ev: FaultEvent, tz: str) -> str:
    bits = []
    if ev.code:
        bits.append("code %s" % ev.code)
    if ev.description:
        bits.append(truncate(ev.description, 80))
    text = " — ".join(bits) if bits else "entry"
    text = "%s (%s)" % (text, local_time(ev.occurred_at, tz)) if ev.occurred_at else text
    return text + (" [cleared]" if ev.state == "cleared" else " [ACTIVE]" if ev.state == "active" else "")


def _baseline_history_note(events: List[FaultEvent], tz: str) -> str:
    if not events:
        return "Fault history: no entries yet."
    stamped = [e for e in events if e.occurred_at and _is_iso(e.occurred_at)]
    latest = sorted(stamped, key=lambda e: e.occurred_at or "", reverse=True) if len(stamped) == len(events) else events
    shown = "; ".join(describe_event(e, tz) for e in latest[:3])
    return "Fault history: %d existing entr%s recorded without alerting. Most recent: %s." % (
        len(events),
        "y" if len(events) == 1 else "ies",
        shown,
    )


def _is_iso(text: str) -> bool:
    try:
        parse_iso(text)
        return True
    except ValueError:
        return False


def _apply_state_change(
    conn: sqlite3.Connection, cfg: Config, mac: str, name: str, row: sqlite3.Row, ev: FaultEvent, now: str, outcome: Outcome
) -> Optional[NewFault]:
    """An entry we already know: has it cleared (or come back) since we last looked?"""
    if ev.state is None or ev.state == row["state"]:
        return None
    if ev.state == "cleared":
        conn.execute(
            "UPDATE faults SET state = 'cleared', cleared_at = COALESCE(?, cleared_at), cleared_seen_at = ?, raw = ? WHERE id = ?",
            (ev.cleared_at, now, ev.raw, row["id"]),
        )
        outcome.cleared += 1
        if row["state"] == "active" and cfg.notify_cleared:  # we had seen it active, so this is news
            title, body = cleared_alert(cfg, name, mac, row, ev.cleared_at, now)
            enqueue(conn, kind="cleared", title=title, body=body, priority=3, now=now, fault_id=row["id"])
        return None
    # it had cleared and is active again
    conn.execute("UPDATE faults SET state = 'active', cleared_at = NULL, cleared_seen_at = NULL, raw = ? WHERE id = ?", (ev.raw, row["id"]))
    if row["state"] == "cleared":
        return NewFault(row["id"], mac, name, "event", ev.code or row["code"], ev.description or row["description"],
                        ev.occurred_at or row["occurred_at"], state="active", again=True)
    return None


def _merge_clearing(
    conn: sqlite3.Connection, cfg: Config, mac: str, name: str, ev: FaultEvent, present: set, now: str, outcome: Outcome
) -> bool:
    """A new-looking entry that is cleared, while an active one with the same code has vanished from the list.

    If Wave rewrites an entry when it clears (and gives it a new time), that is the SAME fault clearing, not a second
    fault: the old row is updated and re-keyed rather than reporting the fault twice. An active entry that is still
    listed is never merged (that would be a separate occurrence).
    """
    if not ev.code:
        return False
    candidates = conn.execute(
        "SELECT id, fingerprint, state, code, description, occurred_at, first_seen_at, last_seen_at FROM faults "
        "WHERE mac = ? AND kind = 'event' AND state = 'active' AND code = ? ORDER BY first_seen_at DESC, id DESC",
        (mac, ev.code),
    ).fetchall()
    row = next((r for r in candidates if r["fingerprint"] not in present), None)
    if row is None:
        return False
    conn.execute(
        """UPDATE faults SET fingerprint = ?, state = 'cleared', cleared_at = ?, cleared_seen_at = ?, last_seen_at = ?,
                             seen_count = seen_count + 1, raw = ? WHERE id = ?""",
        (ev.fingerprint, ev.cleared_at, now, now, ev.raw, row["id"]),
    )
    outcome.cleared += 1
    if cfg.notify_cleared:
        title, body = cleared_alert(cfg, name, mac, row, ev.cleared_at, now)
        enqueue(conn, kind="cleared", title=title, body=body, priority=3, now=now, fault_id=row["id"])
    return True


def _record_fault_payload(
    conn: sqlite3.Connection, cfg: Config, mac: str, name: str, payload: Any, now: str, outcome: Outcome, notes: List[str]
) -> List[NewFault]:
    baseline_key = "baseline:%s:history" % mac
    first = meta_get(conn, baseline_key) is None
    events, where = extract_events(payload, cfg.fault_options)
    created: List[NewFault] = []
    if events is None:
        fingerprint = "blob:" + digest(payload, cfg.fault_options.volatile)[:32]
        row = conn.execute(
            "SELECT id FROM faults WHERE mac = ? AND fingerprint = ? AND kind IN ('event', 'blob')", (mac, fingerprint)
        ).fetchone()
        if row is not None:
            conn.execute("UPDATE faults SET last_seen_at = ?, seen_count = seen_count + 1 WHERE id = ?", (now, row[0]))
        else:
            description = "Fault response changed; it could not be split into entries (%s)" % where
            cur = conn.execute(
                """INSERT INTO faults(mac, kind, source, fingerprint, code, description, occurred_at,
                                      first_seen_at, last_seen_at, baseline, raw)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (mac, "blob", "fault_history", fingerprint, None, description, None, now, now, 1 if first else 0,
                 truncate(json.dumps(payload, ensure_ascii=False, default=str), 20000)),
            )
            if not first:
                created.append(NewFault(int(cur.lastrowid), mac, name, "blob", None, description, None))
        if first:
            notes.append("Fault history: response format not recognised (%s); any change to it will still be reported." % where)
    else:
        outcome.events_seen += len(events)
        present = {ev.fingerprint for ev in events}
        for ev in events:
            row = conn.execute(
                "SELECT id, state, code, description, occurred_at, first_seen_at, last_seen_at FROM faults "
                "WHERE mac = ? AND fingerprint = ? AND kind IN ('event', 'blob')", (mac, ev.fingerprint)
            ).fetchone()
            if row is not None:
                conn.execute("UPDATE faults SET last_seen_at = ?, seen_count = seen_count + 1 WHERE id = ?", (now, row["id"]))
                again = _apply_state_change(conn, cfg, mac, name, row, ev, now, outcome)
                if again is not None:
                    created.append(again)
                continue
            if ev.state == "cleared" and not first and _merge_clearing(conn, cfg, mac, name, ev, present, now, outcome):
                continue
            # An entry that is ACTIVE right now is news even on the very first poll; old entries are not.
            history = first and ev.state != "active"
            cur = conn.execute(
                """INSERT INTO faults(mac, kind, source, fingerprint, code, description, occurred_at,
                                      first_seen_at, last_seen_at, baseline, raw, state, cleared_at, cleared_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mac, "event", "fault_history", ev.fingerprint, ev.code, ev.description, ev.occurred_at, now, now,
                 1 if history else 0, ev.raw, ev.state, ev.cleared_at, None),  # cleared_seen_at: only when we SAW it clear
            )
            if not history:
                created.append(NewFault(int(cur.lastrowid), mac, name, "event", ev.code, ev.description, ev.occurred_at,
                                        state=ev.state, cleared_at=ev.cleared_at))

        active_lists = find_active_lists(payload, cfg.fault_options.list_path)
        if active_lists:
            active_codes = set()
            active_fps = set()
            for _k, lst in active_lists:
                for item in lst:
                    if isinstance(item, dict):
                        a_ev = normalize_event(item, cfg.fault_options)
                        if a_ev.code:
                            active_codes.add(a_ev.code)
                        active_fps.add(a_ev.fingerprint)
            open_active = conn.execute(
                "SELECT id, fingerprint, code, description, occurred_at, first_seen_at, last_seen_at, state "
                "FROM faults WHERE mac = ? AND kind = 'event' AND state = 'active'",
                (mac,),
            ).fetchall()
            for r in open_active:
                if (r["code"] is None or r["code"] not in active_codes) and r["fingerprint"] not in active_fps:
                    conn.execute(
                        "UPDATE faults SET state = 'cleared', cleared_seen_at = ?, last_seen_at = ? WHERE id = ?",
                        (now, now, r["id"]),
                    )
                    outcome.cleared += 1
                    if cfg.notify_cleared and not first:
                        title, body = cleared_alert(cfg, name, mac, r, None, now)
                        enqueue(conn, kind="cleared", title=title, body=body, priority=3, now=now, fault_id=r["id"])

        if first:
            notes.append(_baseline_history_note(events, cfg.display_tz))
    if first:
        meta_set(conn, baseline_key, now)
        outcome.baselines += 1
    return created


def _record_state(
    conn: sqlite3.Connection, cfg: Config, a: ApplianceData, now: str, outcome: Outcome, notes: List[str]
) -> List[NewFault]:
    """Track fault-looking fields in the list/status payloads as episodes (open -> cleared)."""
    if cfg.status_request is not None and not a.status_fetched:
        return []  # missing data must not look like "everything cleared"
    findings = scan_state({"list": a.listing, "status": a.status if a.status_fetched else None}, cfg.scan_ignore)
    current = {"state:%s=%s" % (path, value): (path, value) for path, value in findings}
    open_rows = {
        row["fingerprint"]: row
        for row in conn.execute(
            "SELECT id, fingerprint, code, description FROM faults WHERE mac = ? AND kind = 'state' AND cleared_at IS NULL",
            (a.mac,),
        )
    }
    baseline_key = "baseline:%s:flags" % a.mac
    first = meta_get(conn, baseline_key) is None
    created: List[NewFault] = []
    for fingerprint, (path, value) in current.items():
        if fingerprint in open_rows:
            conn.execute(
                "UPDATE faults SET last_seen_at = ?, seen_count = seen_count + 1 WHERE id = ?",
                (now, open_rows[fingerprint]["id"]),
            )
            continue
        description = "Status field %s" % path
        cur = conn.execute(
            """INSERT INTO faults(mac, kind, source, fingerprint, code, description, occurred_at,
                                  first_seen_at, last_seen_at, baseline, raw)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (a.mac, "state", "status_scan", fingerprint, truncate(value, 60), description, None, now, now,
             1 if first else 0, json.dumps({"path": path, "value": value})),
        )
        if not first:
            created.append(
                NewFault(
                    int(cur.lastrowid), a.mac, a.name, "state", truncate(value, 60), description, None,
                    note="Detected from a fault-like field in the Wave data (a heuristic).",
                )
            )
    for fingerprint, row in open_rows.items():
        if fingerprint in current:
            continue
        conn.execute("UPDATE faults SET cleared_at = ?, last_seen_at = ? WHERE id = ?", (now, now, row["id"]))
        outcome.cleared += 1
        if cfg.notify_cleared and not first:
            enqueue(
                conn,
                kind="cleared",
                title="Fault flag cleared — %s" % a.name,
                body="%s (was %s) is no longer reported.\nNoticed: %s"
                % (row["description"], row["code"], local_time(now, cfg.display_tz)),
                priority=3,
                now=now,
            )
    if first:
        meta_set(conn, baseline_key, now)
        if current:
            shown = "; ".join("%s = %s" % (p, truncate(v, 60)) for p, v in list(current.values())[:3])
            notes.append("Fault-like status fields already active at start: %s." % shown)
    return created


def _queue_fault_alerts(conn: sqlite3.Connection, cfg: Config, faults: List[NewFault], now: str) -> None:
    shown, extra = faults[: cfg.max_alerts_per_cycle], faults[cfg.max_alerts_per_cycle:]
    for fault in shown:
        title, body = fault_alert(cfg, fault, now)
        enqueue(conn, kind="fault", title=title, body=body, priority=cfg.fault_priority, now=now, fault_id=fault.fault_id)
    if extra:
        lines = ["- %s" % truncate("%s %s" % (f.code or "fault", f.description or ""), 100) for f in extra[:10]]
        enqueue(
            conn,
            kind="fault",
            title="%d more new fault entries" % len(extra),
            body="Too many new entries in one poll to alert on each:\n%s\nSee: docker compose exec bwwatch bwwatch faults" % "\n".join(lines),
            priority=cfg.fault_priority,
            now=now,
        )


def health_alert(cfg: Config, kind: str, streak: int, error: str) -> Tuple[str, str]:
    if kind == "auth":
        return (
            "Wave sign-in needs attention — faults are NOT being monitored",
            "bwwatch could not sign in to the Wave cloud, so it cannot see fault codes right now.\n"
            "Reason: %s\n"
            "Fix: on the server run  docker compose run --rm bwwatch login  and follow the prompts." % error,
        )
    return (
        "Wave monitoring is failing",
        "The last %d poll(s) failed, so a fault could be missed.\nLatest error: %s\n"
        "bwwatch keeps retrying every %d minutes." % (streak, error, max(1, cfg.interval // 60)),
    )


def _update_health(conn: sqlite3.Connection, cfg: Config, outcome: Outcome, auth_error: bool, now: str) -> None:
    streak = int(meta_get(conn, "health.streak", "0") or 0)
    alerted = meta_get(conn, "health.alerted", "0") == "1"
    if outcome.ok:
        if alerted:
            enqueue(
                conn,
                kind="recovered",
                title="Wave monitoring is working again",
                body="Polling succeeded again after %d failed poll(s) since %s."
                % (streak, local_time(meta_get(conn, "health.first_failure_at"), cfg.display_tz)),
                priority=3,
                now=now,
            )
        meta_set(conn, "health.streak", 0)
        meta_set(conn, "health.alerted", 0)
        meta_set(conn, "health.last_ok_at", now)
        return
    streak += 1
    if streak == 1:
        meta_set(conn, "health.first_failure_at", now)
    kind = "auth" if auth_error else "api"
    if not alerted:
        due = auth_error or streak >= cfg.health_after_failures
    else:
        due = hours_since(meta_get(conn, "health.last_alert_at"), now) >= cfg.health_repeat_hours or (
            kind == "auth" and meta_get(conn, "health.last_kind") != "auth"
        )
    if due:
        title, body = health_alert(cfg, kind, streak, outcome.error)
        enqueue(conn, kind="health", title=title, body=body, priority=5 if auth_error else 4, now=now)
        meta_set(conn, "health.alerted", 1)
        meta_set(conn, "health.last_alert_at", now)
    meta_set(conn, "health.streak", streak)
    meta_set(conn, "health.last_kind", kind)


def _section(conn: sqlite3.Connection, errors: List[str], label: str, fn: Callable[[], Any]) -> Any:
    """Run one parsing/recording step so that a bug in it cannot lose the rest of the poll."""
    try:
        with savepoint(conn, "section"):
            return fn()
    except sqlite3.Error:
        raise  # a database problem is not a parsing problem: abort and roll the poll back
    except Exception as exc:  # noqa: BLE001
        log.exception("internal error while %s", label)
        errors.append("internal error while %s: %s: %s" % (label, type(exc).__name__, scrub(exc, 200)))
        return None


def apply_cycle(conn: sqlite3.Connection, cfg: Config, fetched: FetchResult, *, now: Optional[str] = None) -> Outcome:
    """Record one poll atomically: the poll row, faults, readings, queued alerts and health state."""
    now = now or iso()
    errors = fetched.all_errors()
    outcome = Outcome(appliances=len(fetched.appliances))
    new_faults: List[NewFault] = []
    notes: Dict[str, List[str]] = {}
    names: Dict[str, str] = {}
    with tx(conn):
        for a in fetched.appliances:
            names[a.mac] = a.name
            note = notes.setdefault(a.mac, [])
            _upsert_appliance(conn, a, now)
            if cfg.log_fields:
                changed = _section(conn, errors, "logging every field of %s" % a.name,
                                   lambda a=a, note=note: _record_fields(conn, cfg, a, now, outcome, note))
                if changed:
                    _section(conn, errors, "checking the watched fields of %s" % a.name,
                             lambda a=a, changed=changed: _watch_alert(conn, cfg, a, changed, now))
            if a.status_fetched:
                # Raw responses are stored first, in their own savepoint, so a parsing bug can never lose them.
                _section(conn, errors, "storing the status response of %s" % a.name,
                         lambda a=a: _snapshot(conn, a.mac, "status", a.status, cfg.fault_options.volatile, now))
                _section(conn, errors, "recording the settings of %s" % a.name,
                         lambda a=a, note=note: _record_status(conn, cfg, a, now, outcome, note))
            if a.faults_fetched:
                _section(conn, errors, "storing the fault response for %s" % a.name,
                         lambda a=a: _snapshot(conn, a.mac, "faults", a.faults, cfg.fault_options.volatile, now))
                found = _section(conn, errors, "reading the fault history for %s" % a.name,
                                 lambda a=a, note=note: _record_fault_payload(conn, cfg, a.mac, a.name, a.faults, now, outcome, note))
                new_faults.extend(found or [])
            if cfg.scan_status:
                found = _section(conn, errors, "scanning the status of %s" % a.name,
                                 lambda a=a, note=note: _record_state(conn, cfg, a, now, outcome, note))
                new_faults.extend(found or [])
            if a.energy_hourly_fetched and a.energy_hourly:
                _section(conn, errors, "recording hourly energy usage for %s" % a.name,
                         lambda a=a: _record_energy(conn, a.mac, "hourly", a.energy_hourly, now))
            if a.energy_daily_fetched and a.energy_daily:
                _section(conn, errors, "recording daily energy usage for %s" % a.name,
                         lambda a=a: _record_energy(conn, a.mac, "daily", a.energy_daily, now))
        if fetched.account_faults_fetched:
            names[ACCOUNT] = "your Wave account"
            note = notes.setdefault(ACCOUNT, [])
            _section(conn, errors, "storing the account-level fault response",
                     lambda: _snapshot(conn, ACCOUNT, "faults", fetched.account_faults, cfg.fault_options.volatile, now))
            found = _section(conn, errors, "reading the account-level fault history",
                             lambda: _record_fault_payload(conn, cfg, ACCOUNT, "your Wave account", fetched.account_faults, now, outcome, note))
            new_faults.extend(found or [])
        for mac, lines in notes.items():
            if not lines:
                continue
            body = "bwwatch is now monitoring %s.\n%s" % (names.get(mac, mac), "\n".join(lines))
            if cfg.fault_request is None and not a.faults_fetched and mac != ACCOUNT:
                body += (
                    "\nNote: BW_FAULT_REQUEST is not set, so the fault/notification history is NOT being read yet. "
                    "bwwatch tries GET /wave/getApplianceErrors on its own (./bwctl discover)."
                )
            enqueue(conn, kind="info", title="Watching %s" % names.get(mac, mac), body=body, priority=3, now=now)
        _queue_fault_alerts(conn, cfg, new_faults, now)
        outcome.new_faults = new_faults
        outcome.ok = not errors
        outcome.error = scrub("; ".join(errors), 500) if errors else ""
        cur = conn.execute(
            "INSERT INTO polls(started_at, finished_at, ok, error, appliances, events_seen, new_faults) VALUES(?,?,?,?,?,?,?)",
            (fetched.started_at, now, 1 if outcome.ok else 0, outcome.error or None, outcome.appliances,
             outcome.events_seen, len(new_faults)),
        )
        outcome.poll_id = int(cur.lastrowid)
        if fetched.calls:  # a bug in the request log must never cost the poll itself, so its errors are only logged
            _section(conn, [], "storing the request log", lambda: _record_calls(conn, fetched.calls, outcome.poll_id))
        _update_health(conn, cfg, outcome, fetched.auth_error, now)
    return outcome


# --- delivering -------------------------------------------------------------
def _message_data(conn: sqlite3.Connection, row: sqlite3.Row) -> Optional[Dict[str, Any]]:
    """Structured details (appliance + fault) for channels that can use them, e.g. Home Assistant."""
    if not row["fault_id"]:
        return None
    fault = conn.execute(
        """SELECT f.id, f.mac, f.kind, f.source, f.code, f.description, f.occurred_at, f.first_seen_at,
                  a.name AS appliance_name, a.serial AS appliance_serial
           FROM faults f LEFT JOIN appliances a ON a.mac = f.mac WHERE f.id = ?""",
        (row["fault_id"],),
    ).fetchone()
    if fault is None:
        return None
    return {
        "appliance": {
            "name": fault["appliance_name"] or fault["mac"],
            "mac": fault["mac"],
            "serial": fault["appliance_serial"],
        },
        "fault": {
            "id": fault["id"],
            "code": fault["code"],
            "description": fault["description"],
            "occurred_at": fault["occurred_at"],
            "detected_at": fault["first_seen_at"],
            "kind": fault["kind"],
            "source": fault["source"],
        },
    }


def deliver_outbox(conn: sqlite3.Connection, notifier: Notifier, cfg: Config, *, limit: int = 25) -> int:
    """Send pending notifications, oldest first. Returns how many are still pending."""
    rows = conn.execute("SELECT * FROM outbox WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
    for row in rows:
        now = iso()
        if not notifier.channels:
            conn.execute(
                "UPDATE outbox SET status = 'dropped', last_error = ? WHERE id = ?",
                ("no notification channel is configured", row["id"]),
            )
            continue
        if not notifier.channels_for(row["kind"]):
            conn.execute(
                "UPDATE outbox SET status = 'dropped', last_error = ? WHERE id = ?",
                ("no channel is set to receive '%s' alerts" % row["kind"], row["id"]),
            )
            continue
        if hours_since(row["created_at"], now) > OUTBOX_MAX_AGE_DAYS * 24:
            conn.execute("UPDATE outbox SET status = 'dropped', last_error = 'expired' WHERE id = ?", (row["id"],))
            continue
        results = notifier.send(
            Message(
                row["title"],
                row["body"],
                row["priority"],
                TAGS.get(row["kind"], ()),
                kind=row["kind"],
                data=_message_data(conn, row),
                time=row["created_at"],
            )
        )
        failures = {name: err for name, err in results.items() if err}
        if len(failures) < len(results):  # at least one channel got it
            conn.execute(
                "UPDATE outbox SET status = 'sent', sent_at = ?, attempts = attempts + 1, last_error = ? WHERE id = ?",
                (now, "; ".join("%s: %s" % kv for kv in failures.items()) or None, row["id"]),
            )
            if failures:
                log.warning("alert %d delivered, but some channels failed: %s", row["id"], failures)
            else:
                log.info("alert %d delivered via %s: %s", row["id"], ", ".join(results), row["title"])
        else:
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                ("; ".join("%s: %s" % kv for kv in failures.items()), row["id"]),
            )
            log.warning("alert %d could not be delivered (will retry): %s", row["id"], failures)
            break  # the rest would fail the same way; try again on the retry timer
    return int(conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0])
