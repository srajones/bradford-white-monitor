"""Small shared helpers: timestamps, durable file writes, secret scrubbing."""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    """UTC timestamp at second precision, always with a trailing Z."""
    return (dt or utcnow()).astimezone(timezone.utc).strftime(ISO_FMT)


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, ISO_FMT).replace(tzinfo=timezone.utc)


def hours_since(stamp: Optional[str], now: Optional[str] = None) -> float:
    """Hours between two of our ISO stamps; infinity if there is no earlier stamp."""
    if not stamp:
        return float("inf")
    try:
        end = parse_iso(now) if now else utcnow()
        return (end - parse_iso(stamp)).total_seconds() / 3600.0
    except ValueError:
        return float("inf")


def coerce_time(value: Any) -> Optional[str]:
    """Turn an API timestamp into our ISO form where that is unambiguous.

    Epoch seconds/milliseconds and ISO-8601 strings with a zone are converted.
    Anything else (including zone-less local times) is returned as text so no
    information is invented or lost.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value)
    is_number = isinstance(value, (int, float))
    if is_number or (isinstance(value, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", value.strip())):
        num = float(value)
        if num > 1e11:  # milliseconds
            num /= 1000.0
        if 946684800 <= num <= 4102444800:  # years 2000..2100
            return iso(datetime.fromtimestamp(num, timezone.utc))
        return str(value)
    text = str(value).strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text
    return iso(parsed)


def norm_key(key: Any) -> str:
    """Case/punctuation-insensitive form of a JSON key (``Fault_Code`` -> ``faultcode``)."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def local_time(stamp: Optional[str], tz_name: str = "UTC") -> str:
    """``2026-10-04T19:03:00Z`` -> ``2026-10-04 14:03 CDT``.

    Text that is not one of our own stamps (e.g. a zone-less time the API gave us) is
    returned unchanged rather than guessed at.
    """
    if not stamp:
        return "unknown"
    try:
        moment = parse_iso(stamp)
    except ValueError:
        return str(stamp)
    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - unknown zone name or no tz database installed
        zone = timezone.utc
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


# --- secret scrubbing -------------------------------------------------------
# Error text can end up in logs, the database and push notifications, so any
# token-looking material is removed before it gets there.
_SECRET_PATTERNS = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"eyJ[\w-]{8,}(?:\.[\w-]*){2,}"), "[redacted-token]"),
    (
        re.compile(
            r"(?i)\b(refresh_token|access_token|id_token|code|password|client_secret|token)"
            r"(=|\"\s*:\s*\")[^&\s\"']+"
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot[redacted]"),
]


def scrub(text: Any, limit: int = 500) -> str:
    out = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return truncate(" ".join(out.split()), limit)


# --- durable file writes ----------------------------------------------------
def fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace ``path`` with ``data`` so a crash leaves either old or new, never half.

    Write to a temp file in the same directory, fsync it, rename over the target,
    then fsync the directory so the rename itself is durable.
    """
    path = Path(path)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except FileNotFoundError:
            pass
        raise
    fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any, mode: int = 0o600) -> None:
    atomic_write(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"), mode)


# --- JWT peeking ------------------------------------------------------------
def jwt_claims(token: str) -> Dict[str, Any]:
    """Read (without verifying) the claims of a JWT; {} if it is not one."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - any malformed token just means "no claims"
        return {}
