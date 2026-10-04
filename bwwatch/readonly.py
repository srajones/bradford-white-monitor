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

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _words(segment: str) -> List[str]:
    """``getApplianceList`` -> ['get', 'appliance', 'list'] (also splits _ - and .)."""
    out: List[str] = []
    for chunk in re.split(r"[_\-\s.]+", segment):
        out.extend(word.lower() for word in _WORD_RE.findall(chunk))
    return out


def check_read_only(method: str, target: str, body: Any = None) -> None:
    """Raise :class:`ReadOnlyViolation` unless the request is clearly a read."""
    verb = str(method).upper()
    if verb not in ("GET", "POST"):
        raise ReadOnlyViolation("%s requests are never sent (bwwatch only reads)" % verb)
    parts = urllib.parse.urlsplit(target)
    if parts.scheme and parts.scheme.lower() not in ("http", "https"):
        raise ReadOnlyViolation("only http(s) addresses are allowed")
    if "{" in parts.path or "}" in parts.path:
        raise ReadOnlyViolation(
            "placeholders such as {mac} are only allowed in the query string or JSON body, never in the path"
        )
    segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    if not segments:
        raise ReadOnlyViolation("the request has no endpoint path")
    for segment in segments:
        if segment in (".", ".."):
            raise ReadOnlyViolation("'.' and '..' are not allowed in the path")
        for word in _words(segment):
            if word in MUTATING_WORDS:
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
    names = [norm_key(k) for k, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    if isinstance(body, dict):
        names += [norm_key(k) for k in body]
    bad = sorted(set(n for n in names if n in WRITE_PARAM_NAMES))
    if bad:
        raise ReadOnlyViolation(
            "the request carries %s, which only a settings change would send" % ", ".join("'%s'" % b for b in bad)
        )
