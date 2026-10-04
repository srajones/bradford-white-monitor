"""Reading the heater's current settings out of the status payload (read-only).

The Wave status response carries the operating mode and the setpoint. The community
client reports it does *not* include the tank temperature, so any temperature-like
number that does appear is recorded as a bonus, but none is required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .util import norm_key

# From the community client's mode enum (heatModeValue).
MODE_NAMES = {1: "Hybrid", 2: "Hybrid Plus", 3: "Heat Pump", 4: "Electric", 5: "Vacation"}


@dataclass(frozen=True)
class Reading:
    mode: Optional[str] = None
    mode_value: Optional[int] = None
    setpoint_f: Optional[float] = None
    temps: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        bits: List[str] = []
        if self.mode or self.mode_value is not None:
            bits.append("mode %s" % (self.mode or MODE_NAMES.get(self.mode_value or 0, self.mode_value)))
        if self.setpoint_f is not None:
            bits.append("setpoint %s°F" % _fmt(self.setpoint_f))
        for key, value in sorted(self.temps.items()):
            bits.append("%s %s°F" % (key, _fmt(value)))
        return ", ".join(bits) if bits else "no mode/temperature data in the status response"


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else "%.1f" % value


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_reading(payload: Any) -> Optional[Reading]:
    """Mode / setpoint / temperature fields of a status payload, or None if it has none."""
    if not isinstance(payload, dict):
        return None
    flat = {norm_key(k): v for k, v in payload.items() if not isinstance(v, (dict, list))}
    mode_value = _as_int(flat.get("heatmodevalue"))
    mode_raw = flat.get("mode")
    mode = str(mode_raw).strip() if mode_raw not in (None, "") else None
    if mode is None and mode_value in MODE_NAMES:
        mode = MODE_NAMES[mode_value]
    setpoint = _as_float(flat.get("setpointfahrenheit", flat.get("setpoint")))
    temps: Dict[str, float] = {}
    for key, value in payload.items():
        name = norm_key(key)
        if "temp" in name and "setpoint" not in name and isinstance(value, (int, float)) and not isinstance(value, bool):
            temps[str(key)] = float(value)
    if mode is None and mode_value is None and setpoint is None and not temps:
        return None
    return Reading(mode, mode_value, setpoint, temps)


def reading_from_row(row: Any) -> Reading:
    temps: Dict[str, float] = {}
    if row["temps"]:
        try:
            loaded = json.loads(row["temps"])
            temps = {str(k): float(v) for k, v in loaded.items()}
        except (ValueError, TypeError, AttributeError):
            temps = {}
    return Reading(row["mode"], row["mode_value"], row["setpoint_f"], temps)


def reading_changes(old: Reading, new: Reading) -> List[str]:
    """Human-readable differences in the *settings* (mode, setpoint). Temperatures drift; they are not 'changes'."""
    changes: List[str] = []
    if (old.mode_value, (old.mode or "").lower()) != (new.mode_value, (new.mode or "").lower()):
        before = old.mode or MODE_NAMES.get(old.mode_value or 0, old.mode_value)
        after = new.mode or MODE_NAMES.get(new.mode_value or 0, new.mode_value)
        changes.append("mode %s → %s" % (before, after))
    if old.setpoint_f != new.setpoint_f:
        changes.append("setpoint %s → %s°F" % (_fmt(old.setpoint_f), _fmt(new.setpoint_f)))
    return changes
