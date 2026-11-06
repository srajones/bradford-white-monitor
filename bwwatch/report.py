"""Generate a markdown report and CSV export files from the bwwatch database."""
from __future__ import annotations

import csv
import html
import json
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Set

from .config import Config, monitored_macs
from .cycle import ACCOUNT
from .db import DB_NAME, connect, init_schema
from .util import csv_safe, iso, local_time, parse_iso, truncate


def generate_report(cfg: Config, out_dir: Optional[Path] = None) -> Path:
    """Export faults.csv, readings.csv, energy.csv and a summary report.md."""
    target_dir = out_dir or (cfg.data_dir / "exports")
    target_dir.mkdir(parents=True, exist_ok=True)

    db_path = cfg.data_dir / DB_NAME
    if not db_path.exists():
        raise RuntimeError("No database found at %s. Has the watcher run yet?" % db_path)

    conn = connect(db_path)
    init_schema(conn)

    try:
        monitored = monitored_macs(conn, cfg)
        # 1. Export CSVs
        _export_table(
            conn,
            target_dir / "faults.csv",
            """SELECT f.id, f.first_seen_at, f.last_seen_at, f.cleared_at, a.name AS appliance,
                      f.mac, f.kind, f.code, f.description, f.occurred_at, f.state, f.cleared_seen_at,
                      f.seen_count, f.baseline, f.source
               FROM faults f LEFT JOIN appliances a ON a.mac = f.mac ORDER BY f.id DESC""",
            monitored=monitored,
            allow_account=True,
        )

        _export_table(
            conn,
            target_dir / "readings.csv",
            """SELECT r.taken_at, a.name AS appliance, r.mac, r.mode, r.mode_value, r.setpoint_f, r.temps
               FROM readings r LEFT JOIN appliances a ON a.mac = r.mac ORDER BY r.taken_at DESC""",
            monitored=monitored,
        )

        _export_table(
            conn,
            target_dir / "energy.csv",
            """SELECT e.ts AS timestamp, a.name AS appliance, e.mac, e.view, e.total_energy,
                      e.heat_pump_energy, e.element_energy, e.reported_minutes
               FROM energy_usage e LEFT JOIN appliances a ON a.mac = e.mac ORDER BY e.ts DESC""",
            monitored=monitored,
        )

        # 2. Build Markdown report
        md_text = _build_markdown_report(conn, cfg, monitored=monitored)
        report_path = target_dir / "report.md"
        report_path.write_text(md_text, encoding="utf-8")
        return target_dir
    finally:
        conn.close()


def _clean_cell(val: Any, max_len: int = 50) -> str:
    if val is None:
        return "-"
    s = truncate(str(val), max_len)
    s = html.escape(s)
    return s.replace("|", "\\|").replace("\n", " ").replace("\r", "")


def _export_table(
    conn: sqlite3.Connection,
    out_file: Path,
    query: str,
    monitored: Optional[Set[str]] = None,
    allow_account: bool = False,
) -> int:
    cursor = conn.execute(query)
    count = 0
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([d[0] for d in cursor.description])
        for row in cursor:
            if monitored is not None:
                mac = row["mac"]
                if mac not in monitored and not (allow_account and mac == ACCOUNT):
                    continue
            writer.writerow([csv_safe(v) if v is not None else "" for v in tuple(row)])
            count += 1
    return count


def _build_markdown_report(
    conn: sqlite3.Connection, cfg: Config, monitored: Optional[Set[str]] = None
) -> str:
    now_str = local_time(iso(), cfg.display_tz)
    lines: List[str] = [
        "# Bradford White Water Heater Report",
        f"*Generated on {now_str}*",
        "",
        "## Appliance Summary",
    ]

    # Appliances & latest reading
    appliances = [
        a for a in conn.execute("SELECT * FROM appliances ORDER BY name").fetchall()
        if monitored is None or a["mac"] in monitored
    ]
    is_multi = len(appliances) > 1
    app_by_mac = {a["mac"]: (a["name"] or a["mac"]) for a in appliances}

    if not appliances:
        lines.append("*No appliance records in database yet.*")
    else:
        for app in appliances:
            mac = app["mac"]
            name = app["name"] or mac
            serial = app["serial"] or "Unknown"
            model = app["model"] or "Aerotherm"
            reading = conn.execute(
                "SELECT * FROM readings WHERE mac = ? ORDER BY id DESC LIMIT 1", (mac,)
            ).fetchone()

            lines.append(f"### {name}")
            lines.append(f"- **MAC Address**: `{mac}`")
            lines.append(f"- **Serial Number**: `{serial}`")
            lines.append(f"- **Model / Type**: {model}")

            if reading:
                mode = reading["mode"] or "Unknown"
                sp = f"{reading['setpoint_f']}°F" if reading["setpoint_f"] else "Unknown"
                last_time = local_time(reading["taken_at"], cfg.display_tz)
                lines.append(f"- **Current Mode**: {mode}")
                lines.append(f"- **Setpoint**: {sp}")
                if reading["temps"]:
                    try:
                        temps = json.loads(reading["temps"])
                        temps_str = ", ".join(f"{k}: {v}°F" for k, v in temps.items())
                        lines.append(f"- **Tank Temperatures**: {temps_str}")
                    except Exception:
                        pass
                lines.append(f"- **Last Status Update**: {last_time}")
            lines.append("")

    # Faults
    lines.append("## Fault History")
    faults = [
        f for f in conn.execute(
            """SELECT f.*, a.name AS appliance_name FROM faults f
               LEFT JOIN appliances a ON a.mac = f.mac ORDER BY f.id DESC"""
        ).fetchall()
        if monitored is None or f["mac"] in monitored or f["mac"] == ACCOUNT
    ]

    if not faults:
        lines.append("No faults have been recorded.")
    else:
        if is_multi:
            lines.append("| ID | Appliance | Code | Description | State | Reported | Cleared | Duration |")
            lines.append("|---|---|---|---|---|---|---|---|")
        else:
            lines.append("| ID | Code | Description | State | Reported | Cleared | Duration |")
            lines.append("|---|---|---|---|---|---|---|")
        for f in faults:
            fid = f["id"]
            app_name = _clean_cell(f["appliance_name"] or ("Account" if f["mac"] == ACCOUNT else f["mac"]), 30)
            code = _clean_cell(f["code"], 20)
            desc = _clean_cell(f["description"] or "No description", 50)
            state = _clean_cell(f["state"] or "unknown", 20)
            rep = _clean_cell(local_time(f["occurred_at"] or f["first_seen_at"], cfg.display_tz), 30)
            clr = _clean_cell(local_time(f["cleared_at"], cfg.display_tz) if f["cleared_at"] else "-", 30)
            dur = "-"
            if f["occurred_at"] and f["cleared_at"]:
                try:
                    s = (parse_iso(f["cleared_at"]) - parse_iso(f["occurred_at"])).total_seconds()
                    dur = f"{int(s // 60)} mins" if s >= 60 else f"{int(s)}s"
                except Exception:
                    pass
            dur = _clean_cell(dur, 20)
            if is_multi:
                lines.append(f"| {fid} | {app_name} | {code} | {desc} | {state} | {rep} | {clr} | {dur} |")
            else:
                lines.append(f"| {fid} | {code} | {desc} | {state} | {rep} | {clr} | {dur} |")
    lines.append("")

    # Energy summary
    lines.append("## Energy Usage Summary")
    all_daily = [
        r for r in conn.execute(
            """SELECT mac, ts, total_energy, heat_pump_energy, element_energy, reported_minutes
               FROM energy_usage WHERE view = 'daily' ORDER BY ts DESC"""
        ).fetchall()
        if monitored is None or r["mac"] in monitored
    ]

    tot = sum((r["total_energy"] or 0.0) for r in all_daily)
    hp = sum((r["heat_pump_energy"] or 0.0) for r in all_daily)
    el = sum((r["element_energy"] or 0.0) for r in all_daily)

    lines.append(f"- **Total Energy Recorded**: {tot:.2f} kWh")
    if tot > 0:
        lines.append(f"- **Heat Pump Compressor**: {hp:.2f} kWh ({(hp/tot)*100:.1f}%)")
        lines.append(f"- **Backup Electric Element**: {el:.2f} kWh ({(el/tot)*100:.1f}%)")
    else:
        lines.append(f"- **Heat Pump Compressor**: {hp:.2f} kWh")
        lines.append(f"- **Backup Electric Element**: {el:.2f} kWh")

    # Recent daily energy table
    daily_rows = all_daily[:14]

    if daily_rows:
        lines.append("")
        lines.append("### Recent Daily Breakdown (Last 14 Days)")
        if is_multi:
            lines.append("| Date | Appliance | Total (kWh) | Heat Pump (kWh) | Backup Element (kWh) | Run Time |")
            lines.append("|---|---|---|---|---|---|")
        else:
            lines.append("| Date | Total (kWh) | Heat Pump (kWh) | Backup Element (kWh) | Run Time |")
            lines.append("|---|---|---|---|---|")
        for r in daily_rows:
            d_date = _clean_cell(local_time(r["ts"], cfg.display_tz).split()[0], 20)
            app_name = _clean_cell(app_by_mac.get(r["mac"], r["mac"]), 30)
            d_tot = f"{r['total_energy']:.3f}" if r["total_energy"] is not None else "-"
            d_hp = f"{r['heat_pump_energy']:.3f}" if r["heat_pump_energy"] is not None else "-"
            d_el = f"{r['element_energy']:.3f}" if r["element_energy"] is not None else "-"
            d_mins = f"{r['reported_minutes']} mins" if r["reported_minutes"] is not None else "-"
            if is_multi:
                lines.append(f"| {d_date} | {app_name} | {d_tot} | {d_hp} | {d_el} | {d_mins} |")
            else:
                lines.append(f"| {d_date} | {d_tot} | {d_hp} | {d_el} | {d_mins} |")


    lines.append("")
    lines.append("## Exported CSV Files")
    lines.append("The raw data files are available in this folder for use in Excel, Home Assistant, or scripts:")
    lines.append("- `faults.csv`: Complete fault occurrences and clearing logs")
    lines.append("- `readings.csv`: Historical operating modes, setpoints, and temperatures")
    lines.append("- `energy.csv`: Hourly and daily electricity usage breakdown")
    lines.append("")
    return "\n".join(lines)
