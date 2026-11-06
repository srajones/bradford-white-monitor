"""The read-only guarantee - the one file to read to check bwwatch cannot change your heater.

bwwatch only ever *reads* from the Wave cloud. That has to be a property of the code
rather than of good behaviour, because the Wave API changes settings with ordinary
GET requests (``/wave/changeSetpoint``, ``/wave/changeOpMode``), so "GET only" would
not be enough.

Every request to the Wave API passes through :func:`check_read_only` - once when the
settings are loaded and again immediately before the request is sent - and there is
no setting, flag or environment variable that turns it off.

A request is allowed only if ALL of these hold:

1. The method is GET (or POST, only for verb-style reads such as ``getEnergyUsage``).
   PUT, PATCH, DELETE and everything else are never sent.
2. No path segment contains an action word (change, set, update, delete, reset,
   clear, ack, ...), anywhere in the path.
3. The endpoint name reads as a read: it starts with get/list/fetch/... or ends
   with list/history/notifications/faults/...
4. Nothing in the query string or JSON body is named like a setting
   (temperature, mode, setpoint, ...).
5. There are no ``{placeholders}`` in the path, so data can never become part of the
   endpoint name.

What this cannot know is what an *unfamiliar* endpoint does on the server. That is why
the only Wave endpoints the program contacts are the two built-in reads plus the single
fault/notification read the owner configures - and why the owner should paste only a
request the Wave app itself makes when it merely *shows* notifications.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, FrozenSet, List

from .errors import ReadOnlyViolation
from .util import norm_key

# The two calls the community client uses to change the heater (both are plain GETs).
KNOWN_WRITE_ENDPOINTS: FrozenSet[str] = frozenset({"changesetpoint", "changeopmode"})

# Endpoint names must START with one of these...
READ_VERBS: FrozenSet[str] = frozenset(
    {"get", "list", "fetch", "read", "query", "find", "search", "retrieve", "load", "view", "show"}
)
# ...or END with one of these (a noun-style name such as `applianceNotifications`).
READ_NOUNS: FrozenSet[str] = frozenset(
    {
        "list", "lists", "history", "notification", "notifications", "alert", "alerts", "alarm", "alarms",
        "fault", "faults", "event", "events", "message", "messages", "diagnostic", "diagnostics",
        "error", "errors", "log", "logs", "status", "state", "info", "details", "usage", "summary",
        "report", "reports", "records", "items", "entries", "codes", "data", "feed", "inbox", "timeline",
        "overview",
    }
)
# Any of these words anywhere in the path means "this does something" -> refused.
MUTATING_WORDS: FrozenSet[str] = frozenset(
    {
        "change", "set", "update", "delete", "remove", "reset", "clear", "ack", "acknowledge", "dismiss",
        "mark", "post", "send", "create", "add", "put", "toggle", "enable", "disable", "write", "save",
        "submit", "register", "unregister", "subscribe", "unsubscribe", "restart", "reboot", "restore",
        "install", "upgrade", "cancel", "confirm", "apply", "patch", "replace", "rename", "pair",
        "unpair", "unlink", "invite", "revoke", "grant", "execute", "trigger", "activate", "deactivate",
        "unlock", "adjust", "modify", "edit", "configure", "insert", "upload", "override", "calibrate",
        "flush", "purge", "wipe", "erase", "terminate",
    }
)
# Parameter names that only a settings change would carry.
WRITE_PARAM_NAMES: FrozenSet[str] = frozenset(
    {
        "temperature", "setpoint", "setpointfahrenheit", "setpointtemperature", "mode", "opmode",
        "heatmode", "heatmodevalue", "newsetpoint", "newmode", "newtemperature",
    }
)

SAFE_PREFIX_WORDS: FrozenSet[str] = frozenset({"setpoint", "setpoints", "cleared", "setting", "settings"})

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _fully_unquote(s: str) -> str:
    """Iteratively unquote percent-encoded characters until fixed point."""
    for _ in range(10):
        unquoted = urllib.parse.unquote(s)
        if unquoted == s:
            break
        s = unquoted
    return s


def _words(segment: str) -> List[str]:
    """``getApplianceList`` -> ['get', 'appliance', 'list'] (also splits _ - and .)."""
    out: List[str] = []
    for chunk in re.split(r"[_\-\s.]+", segment):
        out.extend(word.lower() for word in _WORD_RE.findall(chunk))
    return out


def _collect_keys(obj: Any) -> List[str]:
    """Recursively collect and normalize all dictionary keys across nested dicts and lists."""
    keys: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(norm_key(_fully_unquote(str(k))))
            keys.extend(_collect_keys(v))
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            keys.extend(_collect_keys(item))
    return keys


def check_read_only(method: str, target: str, body: Any = None) -> None:
    """Raise :class:`ReadOnlyViolation` unless the request is clearly a read."""
    verb = str(method).upper()
    if verb not in ("GET", "POST"):
        raise ReadOnlyViolation("%s requests are never sent (bwwatch only reads)" % verb)
    if "\r" in target or "\n" in target:
        raise ReadOnlyViolation("control characters such as CR/LF are not allowed in target URL")
    parts = urllib.parse.urlsplit(target)
    if parts.scheme and parts.scheme.lower() not in ("http", "https"):
        raise ReadOnlyViolation("only http(s) addresses are allowed")

    unquoted_path = _fully_unquote(parts.path)
    if (
        "{" in parts.path
        or "}" in parts.path
        or "{" in unquoted_path
        or "}" in unquoted_path
    ):
        raise ReadOnlyViolation(
            "placeholders such as {mac} are only allowed in the query string or JSON body, never in the path"
        )
    if "\\" in target or "\\" in unquoted_path:
        raise ReadOnlyViolation("backslashes are not allowed in the path")

    segments = [s for s in unquoted_path.split("/") if s]
    if not segments:
        raise ReadOnlyViolation("the request has no endpoint path")

    for segment in segments:
        if segment in (".", "..") or ".." in segment:
            raise ReadOnlyViolation("'.' and '..' are not allowed in the path")
        norm_seg = norm_key(segment)
        if norm_seg in KNOWN_WRITE_ENDPOINTS or any(kw in norm_seg for kw in KNOWN_WRITE_ENDPOINTS):
            raise ReadOnlyViolation("'%s' changes the water heater's settings and is never called" % segment)
        for word in _words(segment):
            if word in MUTATING_WORDS:
                raise ReadOnlyViolation(
                    "'%s' contains the action word '%s'. bwwatch never changes anything on the water heater "
                    "and has no setting that allows it - change settings in the Wave app" % (segment, word)
                )
            if word not in SAFE_PREFIX_WORDS:
                if any(word.startswith(m) for m in MUTATING_WORDS):
                    raise ReadOnlyViolation(
                        "'%s' contains the action word '%s'. bwwatch never changes anything on the water heater "
                        "and has no setting that allows it - change settings in the Wave app" % (segment, word)
                    )
                if any(m in word for m in MUTATING_WORDS if len(m) >= 5):
                    raise ReadOnlyViolation(
                        "'%s' contains the action word '%s'. bwwatch never changes anything on the water heater "
                        "and has no setting that allows it - change settings in the Wave app" % (segment, word)
                    )

    name = segments[-1]
    if norm_key(name) in KNOWN_WRITE_ENDPOINTS:
        raise ReadOnlyViolation("'%s' changes the water heater's settings and is never called" % name)
    words = _words(name)
    verb_first = bool(words) and words[0] in READ_VERBS
    noun_last = bool(words) and words[-1] in READ_NOUNS
    if not (verb_first or noun_last):
        raise ReadOnlyViolation(
            "'%s' does not look like a read. Endpoint names must start with get/list/fetch/... "
            "or end with list/history/notifications/faults/..." % name
        )
    if verb == "POST" and not verb_first:
        raise ReadOnlyViolation("POST is only accepted for get.../list.../fetch... style endpoints, not '%s'" % name)

    names: List[str] = []
    for q_str in (parts.query, _fully_unquote(parts.query)):
        if not q_str:
            continue
        norm_q = q_str.replace(";", "&").replace("\r", "&").replace("\n", "&")
        for k, v in urllib.parse.parse_qsl(norm_q, keep_blank_values=True):
            for item in (k, _fully_unquote(k), v, _fully_unquote(v)):
                item_str = str(item)
                names.append(norm_key(item_str))
                for sub in re.split(r"[;&\r\n]+", item_str):
                    if "=" in sub:
                        names.append(norm_key(sub.split("=", 1)[0]))
                    else:
                        names.append(norm_key(sub))
    if body is not None:
        if isinstance(body, (str, bytes)):
            try:
                parsed_body = json.loads(body)
                names += _collect_keys(parsed_body)
            except Exception:
                pass
        else:
            names += _collect_keys(body)

    bad = sorted(set(n for n in names if n in WRITE_PARAM_NAMES))
    if bad:
        raise ReadOnlyViolation(
            "the request carries %s, which only a settings change would send" % ", ".join("'%s'" % b for b in bad)
        )
