"""Turning unknown-shaped JSON into fault entries.

Nothing public documents the Wave fault/notification response, so this module
reads whatever comes back as leniently as it safely can. The raw response is
always stored as well, so a wrong guess here never loses data - it can be
re-read later once the real format is known.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .config import FaultOptions
from .util import coerce_time, norm_key, truncate

_MISSING = object()

# --- stable hashing ---------------------------------------------------------


def strip_volatile(obj: Any, volatile: FrozenSet[str]) -> Any:
    """Copy of ``obj`` without keys that change on every call (request ids, read flags...)."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v, volatile) for k, v in obj.items() if norm_key(k) not in volatile}
    if isinstance(obj, list):
        return [strip_volatile(v, volatile) for v in obj]
    return obj


def canonical(obj: Any, volatile: FrozenSet[str] = frozenset()) -> str:
    return json.dumps(
        strip_volatile(obj, volatile), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def digest(obj: Any, volatile: FrozenSet[str] = frozenset()) -> str:
    return hashlib.sha256(canonical(obj, volatile).encode("utf-8")).hexdigest()


# --- finding the list of entries -------------------------------------------
PREFERRED_LIST_KEYS = (
    "faults", "faulthistory", "faultlist", "notifications", "notificationlist", "alerts",
    "alarms", "events", "history", "items", "records", "results", "messages", "data",
)


def _dig(payload: Any, path: str) -> Any:
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict):
            key = next((k for k in cur if k == part), None)
            if key is None:
                key = next((k for k in cur if norm_key(k) == norm_key(part)), None)
            if key is None:
                return _MISSING
            cur = cur[key]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def _auto_find(payload: Any, depth: int) -> Tuple[Optional[list], str]:
    if isinstance(payload, list):
        return payload, "$"
    if not isinstance(payload, dict) or depth > 2:
        return None, "no list found"
    lists = {k: v for k, v in payload.items() if isinstance(v, list)}
    by_norm = {norm_key(k): k for k in lists}
    for wanted in PREFERRED_LIST_KEYS:
        if wanted in by_norm:
            key = by_norm[wanted]
            return lists[key], key
    dict_lists = [(k, v) for k, v in lists.items() if v and all(isinstance(x, dict) for x in v)]
    if dict_lists:
        key, items = max(dict_lists, key=lambda kv: len(kv[1]))
        return items, key
    for key, value in payload.items():  # e.g. {"data": {"notifications": [...]}}
        if isinstance(value, dict):
            found, where = _auto_find(value, depth + 1)
            if found is not None:
                return found, key if where == "$" else "%s.%s" % (key, where)
    if lists:  # only empty (or scalar) lists: a recognised "nothing here"
        key = next(iter(lists))
        return lists[key], key
    return None, "no list found"


def find_event_list(payload: Any, list_path: str = "") -> Tuple[Optional[list], str]:
    """Locate the list of fault/notification entries; ``(None, why)`` if there is none."""
    if list_path:
        found = _dig(payload, list_path)
        if found is _MISSING:
            return None, "BW_FAULT_LIST_PATH %r was not found in the response" % list_path
        if not isinstance(found, list):
            return None, "BW_FAULT_LIST_PATH %r is not a list" % list_path
        return found, list_path
    return _auto_find(payload, 0)


# --- one entry --------------------------------------------------------------
CODE_KEYS = (
    "faultcode", "errorcode", "alarmcode", "alertcode", "eventcode", "notificationcode",
    "faultnumber", "code", "fault", "error", "alarm", "alert",
)
TEXT_KEYS = (
    "title", "subject", "headline", "faultdescription", "faultmessage", "description",
    "message", "msg", "text", "body", "detail", "details", "summary", "name", "notification",
)
TIME_KEYS = (
    "occurredat", "eventtime", "faulttime", "timestamp", "datetime", "time", "date",
    "createdat", "created", "reportedat", "receivedat", "sentat", "updatedat", "ts",
)
ID_KEYS = ("id", "eventid", "faultid", "notificationid", "alertid", "messageid", "uuid", "guid", "_id")


@dataclass(frozen=True)
class FaultEvent:
    fingerprint: str
    code: Optional[str]
    description: Optional[str]
    occurred_at: Optional[str]
    raw: str


def _flatten(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Scalars by normalised key; entries of nested objects count too (top level wins)."""
    flat: Dict[str, Any] = {}
    for key, value in ev.items():
        if not isinstance(value, (dict, list)):
            flat.setdefault(norm_key(key), value)
    for value in ev.values():
        if isinstance(value, dict):
            for key, inner in value.items():
                if not isinstance(inner, (dict, list)):
                    flat.setdefault(norm_key(key), inner)
    return flat


def _first(flat: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        value = flat.get(key)
        if value not in (None, ""):
            return value
    return None


_RELATIVE_TIME = re.compile(
    r"(?i)\b(ago|just now|yesterday|today|tomorrow|now)\b|^\s*\d+\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$"
)


def _stable_time(occurred_at: Optional[str], raw: Any) -> Optional[str]:
    """The time as an identity ingredient: absolute stamps only (never 'five minutes ago')."""
    if raw in (None, ""):
        return None
    if occurred_at and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", occurred_at):
        return occurred_at
    text = str(raw).strip()
    return None if _RELATIVE_TIME.search(text) else text


def normalize_event(item: Any, opts: FaultOptions) -> FaultEvent:
    if not isinstance(item, dict):
        text = truncate(str(item), 300)
        return FaultEvent("h:" + digest(item)[:32], None, text, None, json.dumps(item, default=str, ensure_ascii=False))
    flat = _flatten(item)
    raw_json = json.dumps(item, default=str, ensure_ascii=False, sort_keys=True)

    code_value = flat.get(norm_key(opts.code_field)) if opts.code_field else _first(flat, CODE_KEYS)
    code = str(code_value) if code_value not in (None, "") else None

    if opts.text_field:
        texts = [flat.get(norm_key(opts.text_field))]
    else:
        texts, seen = [], set()
        for key in TEXT_KEYS:
            value = flat.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in seen:
                seen.add(value.strip())
                texts.append(value.strip())
            if len(texts) == 2:
                break
    description = " — ".join(str(t) for t in texts if t not in (None, "")) or None

    time_value = flat.get(norm_key(opts.time_field)) if opts.time_field else _first(flat, TIME_KEYS)
    occurred_at = coerce_time(time_value)

    if opts.id_fields:
        parts = [str(flat.get(norm_key(f), "")) for f in opts.id_fields]
        ident = "|".join(parts) if any(parts) else None
    else:
        ident_value = _first(flat, ID_KEYS)
        ident = str(ident_value) if ident_value is not None else None

    if opts.id_fields and ident:
        fingerprint = "id:" + ident  # the owner said which fields identify an entry: trust that alone
    else:
        # An id alone is not trusted: if it were positional (0, 1, 2... newest first) a brand-new fault would
        # reuse a known id and its alert would be missed. Adding the code and an absolute time makes that
        # impossible; relative times ("2 hours ago") are left out because they change on every poll.
        stable_time = _stable_time(occurred_at, time_value)
        parts = []
        if ident:
            parts.append("id=" + ident)
        if code:
            parts.append("code=" + code)
        if stable_time:
            parts.append("at=" + stable_time)
        if ident or (code and stable_time):
            fingerprint = "|".join(parts)
        else:
            fingerprint = "h:" + digest(item, opts.volatile)[:32]
    return FaultEvent(fingerprint, code, truncate(description, 400) if description else None, occurred_at, raw_json)


def extract_events(payload: Any, opts: FaultOptions) -> Tuple[Optional[List[FaultEvent]], str]:
    """Entries found in a fault response, or ``(None, why)`` if it has no recognisable list."""
    items, where = find_event_list(payload, opts.list_path)
    if items is None:
        return None, where
    events: List[FaultEvent] = []
    seen = set()
    for item in items:
        if opts.match is not None and not opts.match.search(json.dumps(item, default=str, ensure_ascii=False)):
            continue
        event = normalize_event(item, opts)
        if event.fingerprint in seen:
            continue
        seen.add(event.fingerprint)
        events.append(event)
    return events, where


# --- fault flags inside status payloads -------------------------------------
FAULT_KEY_RE = re.compile(r"fault|error|alarm|alert|diagnos|malfunction|trouble|lockout|dtc|warning|problem", re.I)
CLEAR_STRINGS = frozenset(
    {
        "", "0", "false", "none", "null", "no", "ok", "normal", "n/a", "na", "nominal", "clear", "cleared",
        "good", "off", "inactive", "no fault", "no faults", "no error", "no errors", "no alarm", "no alarms",
        "no alert", "no alerts", "no warning", "no warnings",
    }
)


def is_clear(value: Any) -> bool:
    """Does this value mean 'nothing wrong'?"""
    if value is None or value is False:
        return True
    if value is True:
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in CLEAR_STRINGS
    if isinstance(value, dict):
        return all(is_clear(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(is_clear(v) for v in value)
    return False


def _compact(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return truncate(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str), 200)
    return truncate(str(value), 200)


def scan_state(obj: Any, ignore: Optional["re.Pattern[str]"] = None, path: str = "") -> List[Tuple[str, str]]:
    """Fault-looking keys with a non-clear value, as ``(json_path, value_text)``.

    A heuristic safety net for when the fault flag lives inside the list/status
    payloads. Keys that merely configure alerts (``alertsEnabled``...) are ignored.
    """
    found: List[Tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            where = "%s.%s" % (path, key) if path else str(key)
            if ignore is not None and ignore.search(str(key)):
                continue
            if FAULT_KEY_RE.search(str(key)):
                if not is_clear(value):
                    found.append((where, _compact(value)))
            else:
                found.extend(scan_state(value, ignore, where))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(scan_state(value, ignore, "%s[%d]" % (path, index)))
    return found
