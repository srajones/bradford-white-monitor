"""Keeping every field the Wave cloud tells us about, so nothing that could explain a fault is thrown away.

Every answer (the appliance list, the status, the fault history, any extra request you add) is split into its
individual fields - ``status.setpointFahrenheit``, ``list.accessLevel``, ``extra1.compressor.state`` and so on.
For each field the latest value is remembered, and **every change** is written to a log: when it first appeared,
when it changed from what to what, when it vanished and when it came back. Nothing is written for a field that is
the same as last time, so even a check every five minutes stays small, and the whole history of a field is one
query away (``./bwctl changes`` and ``./bwctl fields``).

The raw answers are kept as well (``snapshots``, whenever one differs from the last), so even a field this module
could not make sense of is never lost.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .util import iso, norm_key, parse_iso, truncate

MAX_FIELDS = 4000  # one answer never produces more than this many fields
MAX_DEPTH = 12
MAX_LIST = 100  # only the first entries of a list are followed
MAX_TEXT = 400

Value = Tuple[Optional[str], str]  # (text, type)


@dataclass(frozen=True)
class Change:
    source: str
    path: str
    event: str  # start (the first values seen) | new (a field that appeared later) | changed | gone | back
    old: Optional[str]
    new: Optional[str]

    @property
    def name(self) -> str:
        return "%s.%s" % (self.source, self.path)


def scalar(value: Any) -> Value:
    """A JSON scalar as ``(text, type)``; the type keeps ``"1"`` (text) apart from ``1`` (a number)."""
    if value is None:
        return None, "null"
    if isinstance(value, bool):
        return ("true" if value else "false"), "bool"
    if isinstance(value, int):
        return str(value), "int"
    if isinstance(value, float):
        return format(value, ".15g"), "float"
    return truncate(str(value), MAX_TEXT), "str"


def flatten(payload: Any, volatile: FrozenSet[str] = frozenset(), skip_lists: bool = False) -> Dict[str, Value]:
    """``{"a": {"b": [1, 2]}}`` -> ``{"a.b[0]": ("1", "int"), "a.b[1]": ("2", "int")}``.

    Keys that change on every call (request ids and the like, ``volatile``) are left out. With ``skip_lists`` only
    plain fields are kept: used for event logs such as the fault history, whose entries have their own table.
    """
    out: Dict[str, Value] = {}

    def walk(obj: Any, path: str, depth: int) -> None:
        if len(out) >= MAX_FIELDS or depth > MAX_DEPTH:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if norm_key(key) in volatile:
                    continue
                walk(value, "%s.%s" % (path, key) if path else str(key), depth + 1)
        elif isinstance(obj, (list, tuple)):
            if skip_lists:
                return
            for index, value in enumerate(obj[:MAX_LIST]):
                walk(value, "%s[%d]" % (path, index), depth + 1)
        else:
            out[path or "$"] = scalar(obj)

    walk(payload, "", 0)
    return out


def record(
    conn: sqlite3.Connection,
    mac: str,
    source: str,
    payload: Any,
    now: str,
    volatile: FrozenSet[str] = frozenset(),
    skip_lists: bool = False,
) -> Tuple[List[Change], bool]:
    """Log what is new or different in ``payload`` since the last look.

    Returns ``(changes, first_look)``; on the very first look of a source every field is "new", which is the
    starting picture rather than news.
    """
    current = flatten(payload, volatile, skip_lists)
    known = {
        row["path"]: row
        for row in conn.execute("SELECT path, value, vtype, present FROM field_state WHERE mac = ? AND source = ?", (mac, source))
    }
    first_look = not known
    changes: List[Change] = []

    def log(path: str, event: str, old: Optional[str], new: Optional[str], vtype: Optional[str]) -> None:
        conn.execute(
            "INSERT INTO observations(taken_at, mac, source, path, event, old_value, new_value, vtype) VALUES(?,?,?,?,?,?,?,?)",
            (now, mac, source, path, event, old, new, vtype),
        )
        changes.append(Change(source, path, event, old, new))

    for path, (value, vtype) in current.items():
        row = known.get(path)
        if row is None:
            conn.execute(
                """INSERT INTO field_state(mac, source, path, value, vtype, first_seen_at, last_changed_at, last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (mac, source, path, value, vtype, now, now, now),
            )
            log(path, "start" if first_look else "new", None, value, vtype)
        elif not row["present"]:
            conn.execute(
                "UPDATE field_state SET value = ?, vtype = ?, present = 1, last_changed_at = ?, changes = changes + 1 "
                "WHERE mac = ? AND source = ? AND path = ?",
                (value, vtype, now, mac, source, path),
            )
            log(path, "back", row["value"], value, vtype)
        elif row["value"] != value or row["vtype"] != vtype:
            conn.execute(
                "UPDATE field_state SET value = ?, vtype = ?, last_changed_at = ?, changes = changes + 1 "
                "WHERE mac = ? AND source = ? AND path = ?",
                (value, vtype, now, mac, source, path),
            )
            log(path, "changed", row["value"], value, vtype)
    for path, row in known.items():
        if path not in current and row["present"]:
            conn.execute(
                "UPDATE field_state SET present = 0, last_changed_at = ?, changes = changes + 1 WHERE mac = ? AND source = ? AND path = ?",
                (now, mac, source, path),
            )
            log(path, "gone", row["value"], None, row["vtype"])
    conn.execute("UPDATE field_state SET last_seen_at = ? WHERE mac = ? AND source = ? AND present = 1", (now, mac, source))
    return changes, first_look


def prune(conn: sqlite3.Connection, keep_days: int, now_iso: str) -> int:
    """Forget field changes and request records older than ``keep_days`` (0 keeps everything). Returns rows removed."""
    if keep_days <= 0:
        return 0
    cutoff = iso(parse_iso(now_iso) - timedelta(days=keep_days))
    removed = 0
    for table in ("observations", "api_calls"):
        removed += conn.execute("DELETE FROM %s WHERE taken_at < ?" % table, (cutoff,)).rowcount
    removed += conn.execute("DELETE FROM field_state WHERE present = 0 AND last_changed_at < ?", (cutoff,)).rowcount
    return removed
