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
    "faults", "faulthistory", "faultlist", "errorhistory", "errorlist", "errors",  # the Wave app: "error_history"
    "notifications", "notificationlist", "alerts",
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
    "title", "subject", "headline", "faultdescription", "faultmessage", "errorstring", "errordescription",
    "errormessage", "errortext", "faultstring", "description",
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
    state: Optional[str] = None  # "active" or "cleared" when the entry says so, else None (unknown)
    cleared_at: Optional[str] = None  # when the entry says it cleared, if it gives a time


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


# --- is the entry active or has it cleared? ----------------------------------
# The Wave app lists a fault as "Fault 10 / (Cleared) Superheat Fault" once it has gone away. Nobody has
# published how the API says so, so this looks for the usual ways: a "(Cleared)" marker in the text, a status
# word, a true/false flag, or a time it ended. Only explicit evidence counts; no evidence means "unknown".
CLEARED_MARK = re.compile(r"(?i)[(\[]\s*(?:cleared|resolved|recovered|restored|closed|inactive)\s*[)\]]\s*[-:\u2014]?\s*")
CLEARED_LEAD = re.compile(r"(?i)^\s*(?:cleared|resolved|recovered|restored)\b\s*[-:\u2014]*\s*")
PURE_CODE = re.compile(r"(?i)\s*(?:fault|error|alarm|alert)\s*(?:code\s*)?[#:]?\s*\d{1,5}\s*")
FAULT_NUMBER = re.compile(r"(?i)\b(?:fault|error|alarm|alert)\s*(?:code\s*)?[#:]?\s*(\d{1,5})\b")
STATE_KEYS = ("status", "state", "faultstatus", "alertstatus", "eventstatus", "alarmstatus", "notificationstatus", "condition")
CLEARED_FLAGS = ("cleared", "iscleared", "resolved", "isresolved", "recovered", "isrecovered", "closed", "isclosed",
                 "restored", "isrestored", "inactive", "isinactive")
ACTIVE_FLAGS = ("active", "isactive", "ongoing", "isongoing", "open", "isopen", "unresolved", "isunresolved")
CLEARED_TIMES = ("clearedat", "clearedtime", "cleareddate", "clearedon", "clearedtimestamp", "resolvedat", "resolvedtime",
                 "resolveddate", "recoveredat", "closedat", "restoredat", "endedat", "endtime", "enddate", "stoppedat")
CLEARED_WORDS = re.compile(r"(?i)\b(?:clear(?:ed)?|resolved|recovered|restored|closed|inactive|ended)\b")
ACTIVE_WORDS = re.compile(r"(?i)\b(?:active|open|raised|ongoing|triggered|unresolved|current)\b")
_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0"}


def _truth(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def detect_state(flat: Dict[str, Any], texts: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(state, cleared_at)`` for one entry: ``("cleared", time or None)``, ``("active", None)`` or ``(None, None)``."""
    cleared, active = False, False
    cleared_at: Optional[str] = None
    for key in CLEARED_TIMES:
        value = flat.get(key)
        if value not in (None, "", 0, "0", False):
            cleared = True
            cleared_at = cleared_at or coerce_time(value)
    for key in CLEARED_FLAGS:
        if key in flat:
            truth = _truth(flat[key])
            cleared, active = cleared or truth is True, active or truth is False
    for key in ACTIVE_FLAGS:
        if key in flat:
            truth = _truth(flat[key])
            active, cleared = active or truth is True, cleared or truth is False
    for key in STATE_KEYS:
        value = flat.get(key)
        if isinstance(value, str):
            if CLEARED_WORDS.search(value):
                cleared = True
            elif ACTIVE_WORDS.search(value):
                active = True
    if any(CLEARED_MARK.search(t) or CLEARED_LEAD.search(t) for t in texts):
        cleared = True
    if cleared:
        return "cleared", cleared_at
    return ("active", None) if active else (None, None)


def strip_state_marker(text: str) -> str:
    """The text without a leading/embedded "(Cleared)" marker (kept as is if nothing else would remain)."""
    stripped = CLEARED_LEAD.sub("", CLEARED_MARK.sub("", text)).strip(" -:\u2014")
    return stripped or text


# An id that is clearly unique per entry (a UUID, a long number, a long opaque string) identifies the entry on its
# own - even if Wave rewrites the entry when it clears. A short number may only be a list position, so it never does.
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


def looks_unique(ident: Optional[str]) -> bool:
    if not ident:
        return False
    text = str(ident).strip()
    if _UUID.fullmatch(text) or re.fullmatch(r"\d{6,}", text):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{16,}", text) and re.search(r"\d", text) and re.search(r"[A-Za-z]", text))


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
    shown = [str(t) for t in texts if t not in (None, "")]
    if code is None and not opts.code_field:  # the app's own wording: "Fault 10"
        for text in shown:
            found = FAULT_NUMBER.search(text)
            if found:
                code = found.group(1)
                break
    state, cleared_at = detect_state(flat, shown)
    wording = [t for t in shown if not PURE_CODE.fullmatch(t)] or shown  # "Fault 10" alone adds nothing next to the code
    description = " — ".join(strip_state_marker(t) for t in wording) or None

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
    elif looks_unique(ident):
        fingerprint = "id:" + str(ident).strip()  # unique by its look, so it also survives a rewrite when the fault clears
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
    return FaultEvent(fingerprint, code, truncate(description, 400) if description else None, occurred_at, raw_json,
                      state, cleared_at)


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
