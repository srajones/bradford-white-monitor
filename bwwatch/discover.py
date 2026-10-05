"""Find which form of ``/wave/getApplianceErrors`` the cloud accepts, and remember it.

The Wave app (v1.1.3371) reads the Notifications screen from that path. Its code does not say
which parameter name it sends, so the three forms it uses elsewhere are tried, read-only, at most
once a day, until one returns JSON. An explicit ``BW_FAULT_REQUEST`` always wins.
``./bwctl discover --reset`` forgets a remembered form.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .config import ConfigError, RequestSpec
from .db import meta_delete, meta_get, meta_set, tx
from .errors import AuthError, TransientError, WaveError
from .util import hours_since, iso, scrub

log = logging.getLogger("bwwatch.discover")

META_KEY = "learned.fault_request"
ATTEMPT_KEY = "learned.fault_attempt_at"
RETRY_HOURS = 24
GAP_SECONDS = 3.0

# Order matters: the first one that returns JSON is kept. These are the query shapes
# present as plain text in the app (`?macAddress=`, `?mac_address=`, `&serialNumber=`).
CANDIDATES = (
    "GET /wave/getApplianceErrors?macAddress={mac}",
    "GET /wave/getApplianceErrors?mac_address={mac}",
    "GET /wave/getApplianceErrors?macAddress={mac}&serialNumber={serial}",
)


def load(conn) -> Optional[RequestSpec]:
    """The form remembered from an earlier poll, or None."""
    text = (meta_get(conn, META_KEY) or "").strip()
    if not text:
        return None
    try:
        return RequestSpec.parse(text, label="learned fault request")
    except ConfigError as exc:
        log.warning("forgetting a stored fault request that no longer parses (%s)", exc)
        meta_delete(conn, META_KEY)
        return None


def forget(conn) -> None:
    """Drop the remembered form so the next poll tries again."""
    meta_delete(conn, META_KEY)
    meta_delete(conn, ATTEMPT_KEY)


def _shape(payload: Any) -> str:
    if isinstance(payload, dict):
        keys = ", ".join(sorted(str(k) for k in payload)[:12])
        return "JSON object, keys: %s" % (keys or "(none)")
    if isinstance(payload, list):
        return "JSON list of %d" % len(payload)
    return type(payload).__name__


def _record(conn, text: str, spec: RequestSpec, status: Optional[int], verdict: str, note: str, sample: str = "") -> None:
    conn.execute(
        """INSERT INTO discovery(tried_at, key, name, method, target, body, status, verdict, note, sample, origin)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             tried_at=excluded.tried_at, status=excluded.status, verdict=excluded.verdict,
             note=excluded.note, sample=excluded.sample""",
        (iso(), text, "getApplianceErrors", spec.method, spec.target, None, status, verdict, note[:300], sample[:300], "builtin"),
    )


def try_candidates(api, appliance, conn, stop=None, *, force: bool = False, gap: float = GAP_SECONDS) -> Optional[RequestSpec]:
    """Try the three forms. Returns the one that answered with JSON, or None.

    A recent miss (see ``RETRY_HOURS``) is not tried again unless ``force``. Raises ``AuthError``
    (the sign-in is broken) and stops on HTTP 429 without burning the daily attempt.
    """
    if not force and hours_since(meta_get(conn, ATTEMPT_KEY)) < RETRY_HOURS:
        return None
    ctx = {"mac": appliance.mac, "serial": appliance.serial or "", "name": appliance.name, "account_id": ""}
    found: Optional[str] = None
    stopped = False
    limited = False
    for index, text in enumerate(CANDIDATES):
        spec = RequestSpec.parse(text, label="fault candidate")
        if "serial" in spec.placeholders() and not ctx["serial"]:
            with tx(conn):
                _record(conn, text, spec, None, "skipped", "this heater has no serial number")
            continue
        if index and gap > 0:
            if stop is not None and stop.wait(gap):
                stopped = True
                break
            if stop is None:
                time.sleep(gap)
        try:
            payload = api.call(spec, ctx)
        except AuthError:
            raise
        except TransientError as exc:
            with tx(conn):
                _record(conn, text, spec, 429, "rate-limited", scrub(exc, 200))
            limited = True
            break
        except WaveError as exc:
            with tx(conn):
                _record(conn, text, spec, None, "no", scrub(exc, 200))
            continue
        if isinstance(payload, (dict, list)):
            with tx(conn):
                _record(conn, text, spec, 200, "yes", "answered with JSON", _shape(payload))
            found = text
            break
        with tx(conn):
            _record(conn, text, spec, 200, "no", "the answer was not JSON")
    if stopped or limited:
        return RequestSpec.parse(found, label="learned fault request") if found else None
    with tx(conn):
        if found:
            meta_set(conn, META_KEY, found)
            meta_delete(conn, ATTEMPT_KEY)
        else:
            meta_set(conn, ATTEMPT_KEY, iso())
    return RequestSpec.parse(found, label="learned fault request") if found else None
