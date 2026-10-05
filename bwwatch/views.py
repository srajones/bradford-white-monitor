"""Looking at what has been logged: every field, every change, the energy use, the search for the request.

These commands only read the database. ``./bwctl fields`` is the answer to "what does the cloud tell us?";
``./bwctl changes`` answers "what changed, and when?" - most usefully around the time of a fault.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence
from zoneinfo import ZoneInfo

from .config import Config
from .db import DB_NAME, connect, init_schema
from .util import iso, local_time, parse_iso, truncate


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(c) if c is not None else "" for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip(), "  ".join("-" * w for w in widths)]
    lines.extend("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells)
    return "\n".join(lines)


def open_db(cfg: Config) -> Optional[sqlite3.Connection]:
    if not (cfg.data_dir / DB_NAME).exists():
        return None
    conn = connect(cfg.data_dir / DB_NAME)
    init_schema(conn)
    return conn


def parse_when(text: str, tz_name: str = "UTC") -> str:
    """``2026-10-04 13:15`` (in your time zone), ``2026-10-04T17:15:00Z`` or just ``2026-10-04`` -> our UTC stamp."""
    raw = text.strip()
    if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", raw):
        return raw
    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        zone = timezone.utc
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return iso(datetime.strptime(raw, fmt).replace(tzinfo=zone))
        except ValueError:
            continue
    raise ValueError("cannot read %r as a time; use something like  2026-10-04 13:15  (in your time zone)" % text)


def _pattern(text: Optional[str]) -> Optional["re.Pattern[str]"]:
    if not text:
        return None
    try:
        return re.compile(text, re.I)
    except re.error as exc:
        raise ValueError("--match is not a valid regular expression: %s" % exc)


def _shown(value: Optional[str], limit: int = 40) -> str:
    return "(none)" if value is None else truncate(value, limit)


def cmd_fields(cfg: Config, args: argparse.Namespace) -> int:
    """Every field the cloud has ever reported, with its current value and how often it has changed."""
    conn = open_db(cfg)
    if conn is None:
        print("No database yet - has the service run?")
        return 1
    try:
        wanted = _pattern(args.match)
        rows = [
            r for r in conn.execute(
                "SELECT s.*, a.name AS appliance FROM field_state s LEFT JOIN appliances a ON a.mac = s.mac "
                "ORDER BY s.mac, s.source, s.path"
            )
            if wanted is None or wanted.search("%s.%s" % (r["source"], r["path"]))
        ]
        if not rows:
            print("No fields logged yet." if wanted is None else "No logged field matches %r." % args.match)
            return 0
        several = len({r["mac"] for r in rows}) > 1
        lines = []
        for r in rows:
            value = _shown(r["value"]) if r["present"] else "(gone; was %s)" % _shown(r["value"], 24)
            row = ["%s.%s" % (r["source"], r["path"]), value, r["changes"], local_time(r["first_seen_at"], cfg.display_tz),
                   local_time(r["last_changed_at"], cfg.display_tz) if r["changes"] else "never"]
            if several:
                row.insert(0, r["appliance"] or r["mac"])
            lines.append(row)
        print(table((("HEATER",) if several else ()) + ("FIELD", "VALUE NOW", "CHANGES", "FIRST SEEN", "LAST CHANGE"), lines))
        changed = sum(1 for r in rows if r["changes"])
        print("\n%d field(s); %d have changed at least once. History of one:  ./bwctl changes --match <part of its name>" % (len(rows), changed))
    finally:
        conn.close()
    return 0


def cmd_changes(cfg: Config, args: argparse.Namespace) -> int:
    """What changed, oldest first: by default the last 24 hours; ``--around`` shows the hours around a moment."""
    conn = open_db(cfg)
    if conn is None:
        print("No database yet - has the service run?")
        return 1
    try:
        wanted = _pattern(args.match)
        where: List[str] = []
        params: List[Any] = []
        if args.around:
            centre = parse_when(args.around, cfg.display_tz)
            span = timedelta(minutes=args.minutes)
            where += ["o.taken_at >= ?", "o.taken_at <= ?"]
            params += [iso(parse_iso(centre) - span), iso(parse_iso(centre) + span)]
        elif args.hours > 0:
            where.append("o.taken_at >= ?")
            params.append(iso(datetime.now(timezone.utc) - timedelta(hours=args.hours)))
        if not args.all:  # the starting picture (the first values seen) is not a change
            where.append("o.event != 'start'")
        rows = [
            r for r in conn.execute(
                "SELECT o.*, a.name AS appliance FROM observations o LEFT JOIN appliances a ON a.mac = o.mac "
                + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY o.id", params)
            if wanted is None or wanted.search("%s.%s" % (r["source"], r["path"]))
        ]
        if not rows:
            print("Nothing changed in that time." if not args.all else "Nothing logged in that time.")
            return 0
        shown = rows[-args.limit:] if args.limit > 0 else rows
        several = len({r["mac"] for r in rows}) > 1
        lines = []
        for r in shown:
            what = {"changed": "%s -> %s" % (_shown(r["old_value"], 30), _shown(r["new_value"], 30)),
                    "start": "first seen: %s" % _shown(r["new_value"], 40),
                    "new": "appeared: %s" % _shown(r["new_value"], 40),
                    "gone": "disappeared (was %s)" % _shown(r["old_value"], 30),
                    "back": "came back: %s (was %s)" % (_shown(r["new_value"], 30), _shown(r["old_value"], 20))}[r["event"]]
            row = [local_time(r["taken_at"], cfg.display_tz), "%s.%s" % (r["source"], r["path"]), what]
            if several:
                row.insert(1, r["appliance"] or r["mac"])
            lines.append(row)
        print(table(("WHEN",) + (("HEATER",) if several else ()) + ("FIELD", "WHAT HAPPENED"), lines))
        if len(shown) < len(rows):
            print("\n(showing the latest %d of %d; use --limit 0 for all)" % (len(shown), len(rows)))
    finally:
        conn.close()
    return 0


def cmd_calls(cfg: Config, args: argparse.Namespace) -> int:
    """What was asked of the cloud: requests per endpoint in the window, status codes, speed, and any 429s."""
    conn = open_db(cfg)
    if conn is None:
        print("No database yet - has the service run?")
        return 1
    try:
        since = iso(datetime.now(timezone.utc) - timedelta(hours=args.hours)) if args.hours > 0 else "0000"
        rows = conn.execute(
            """SELECT endpoint,
                      COUNT(*) AS n,
                      SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END) AS limited,
                      SUM(CASE WHEN status IS NULL OR status >= 400 THEN 1 ELSE 0 END) AS failed,
                      ROUND(AVG(ms)) AS avg_ms,
                      MIN(status) AS lo,
                      MAX(status) AS hi
               FROM api_calls WHERE taken_at >= ? GROUP BY endpoint ORDER BY n DESC""",
            (since,),
        ).fetchall()
        if not rows:
            print("No requests logged in that time.")
            return 0
        window = "the last %g h" % args.hours if args.hours > 0 else "everything logged"
        print("Requests, %s:" % window)
        print(table(
            ("ENDPOINT", "COUNT", "FAILED", "429s", "AVG MS", "STATUS"),
            [(r["endpoint"], r["n"], r["failed"], r["limited"], int(r["avg_ms"] or 0),
              "%s-%s" % (r["lo"] if r["lo"] is not None else "-", r["hi"] if r["hi"] is not None else "-")) for r in rows],
        ))
        total = sum(r["n"] for r in rows)
        limited = sum(r["limited"] for r in rows)
        print("\n%d request(s).%s" % (total, "  The cloud asked us to slow down (HTTP 429)." if limited else ""))
    finally:
        conn.close()
    return 0


def cmd_energy(cfg: Config, args: argparse.Namespace) -> int:
    """Show recorded energy usage: heat pump vs backup electric element (kWh)."""
    conn = open_db(cfg)
    if conn is None:
        print("No database yet - has the service run?")
        return 1
    try:
        view = getattr(args, "view", "hourly") or "hourly"
        limit = getattr(args, "limit", 24)
        where = ["e.view = ?"]
        params: List[Any] = [view]
        if getattr(args, "mac", None):
            where.append("e.mac = ?")
            params.append(args.mac)
        if getattr(args, "element_only", False):
            where.append("e.element_energy > 0")

        query = (
            "SELECT e.*, a.name AS appliance FROM energy_usage e "
            "LEFT JOIN appliances a ON a.mac = e.mac "
            + ("WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY e.ts DESC LIMIT ?"
        )
        params.append(limit if limit > 0 else 500)
        rows = conn.execute(query, params).fetchall()
        if not rows:
            print("No energy usage records found for view %r." % view)
            return 0
        several = len({r["mac"] for r in rows}) > 1
        lines = []
        for r in reversed(rows):
            hp = "%.3f" % r["heat_pump_energy"] if r["heat_pump_energy"] is not None else "-"
            el = "%.3f" % r["element_energy"] if r["element_energy"] is not None else "-"
            tot = "%.3f" % r["total_energy"] if r["total_energy"] is not None else "-"
            mins = str(r["reported_minutes"]) if r["reported_minutes"] is not None else "-"
            flag = " [BACKUP ELEMENT]" if (r["element_energy"] or 0) > 0.05 else ""
            row = [
                local_time(r["ts"], cfg.display_tz),
                hp,
                el + flag,
                tot,
                mins,
            ]
            if several:
                row.insert(1, r["appliance"] or r["mac"])
            lines.append(row)
        headers = ("WHEN",) + (("HEATER",) if several else ()) + ("HEAT PUMP (kWh)", "ELEMENT (kWh)", "TOTAL (kWh)", "MINS")
        print("Energy usage (%s view, latest %d entries):" % (view, len(rows)))
        print(table(headers, lines))
    finally:
        conn.close()
    return 0


def cmd_readings(cfg: Config, args: argparse.Namespace) -> int:
    """Show recorded tank temperatures and operating settings over time."""
    conn = open_db(cfg)
    if conn is None:
        print("No database yet - has the service run?")
        return 1
    try:
        limit = getattr(args, "limit", 25)
        rows = conn.execute(
            """SELECT r.*, a.name AS appliance FROM readings r
               LEFT JOIN appliances a ON a.mac = r.mac
               ORDER BY r.id DESC LIMIT ?""",
            (limit if limit > 0 else 500,),
        ).fetchall()
        if not rows:
            print("No status readings recorded yet.")
            return 0
        several = len({r["mac"] for r in rows}) > 1
        lines = []
        for r in reversed(rows):
            temps_str = "-"
            if r["temps"]:
                try:
                    loaded = json.loads(r["temps"])
                    temps_str = ", ".join("%s: %s°F" % (k, v) for k, v in loaded.items())
                except Exception:
                    temps_str = str(r["temps"])
            sp = "%s°F" % r["setpoint_f"] if r["setpoint_f"] is not None else "-"
            row = [
                local_time(r["taken_at"], cfg.display_tz),
                r["mode"] or "-",
                sp,
                temps_str,
            ]
            if several:
                row.insert(1, r["appliance"] or r["mac"])
            lines.append(row)
        headers = ("TIME",) + (("HEATER",) if several else ()) + ("MODE", "SETPOINT", "TEMPERATURES")
        print("Heater status readings (latest %d entries):" % len(rows))
        print(table(headers, lines))
    finally:
        conn.close()
    return 0


def add_arguments(sub: Any) -> None:
    """The sub-commands this module provides (called by cli.build_parser)."""
    p = sub.add_parser("fields", help="list every field the cloud reports, with its current value and change count")
    p.add_argument("--match", help="only fields whose name matches this (case-insensitive) pattern, e.g. 'compressor|error'")
    p = sub.add_parser("changes", help="what changed and when (default: the last 24 hours)")
    p.add_argument("--hours", type=float, default=24.0, help="how far back to look (0 = everything; default 24)")
    p.add_argument("--match", help="only fields whose name matches this (case-insensitive) pattern")
    p.add_argument("--around", metavar="TIME", help="show the changes around a moment, e.g. a fault: '2026-10-04 13:15' (your time zone)")
    p.add_argument("--minutes", type=int, default=180, help="with --around: how many minutes either side (default 180)")
    p.add_argument("--limit", type=int, default=200, help="show at most this many, the latest (0 = all; default 200)")
    p.add_argument("--all", action="store_true", help="include each source's starting picture (the first values seen)")
    p = sub.add_parser("calls", help="the request log: how many calls, how fast, and any HTTP 429 slow-downs")
    p.add_argument("--hours", type=float, default=24.0, help="how far back to look (0 = everything; default 24)")
    p = sub.add_parser("energy", help="show recorded energy usage (heat pump vs backup element in kWh)")
    p.add_argument("--view", choices=("hourly", "daily"), default="hourly", help="hourly or daily view (default: hourly)")
    p.add_argument("--limit", type=int, default=24, help="how many entries to show (default 24; 0 = all)")
    p.add_argument("--mac", help="filter by appliance mac address")
    p.add_argument("--element-only", action="store_true", help="only show intervals where the backup element fired")
    p = sub.add_parser("readings", help="show recorded tank temperatures and operating settings over time")
    p.add_argument("--limit", type=int, default=25, help="how many entries to show (default 25; 0 = all)")

