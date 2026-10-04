"""Command-line interface: ``bwwatch <command>`` (run it inside the container)."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from . import __version__
from .config import Config, ConfigError, RequestSpec
from .cycle import describe_event, fetch
from .db import DB_NAME, DatabaseTooNew, backup_now, connect, init_schema, integrity_check, list_backups, table_counts
from .errors import AuthError, WaveError
from .faults import extract_events
from .notify import Notifier, test_message
from .privs import AlreadyRunning, prepare_runtime
from .probe import REQUEST_COUNT, run_probe
from .readings import extract_reading, reading_from_row
from .service import Service, healthcheck
from .util import iso, local_time, truncate
from .verify import format_checks, overall_ok, run_checks
from .wave import TokenManager, TokenStore, WaveApi, parse_redirect

log = logging.getLogger("bwwatch")

LOGIN_HELP = """\
Sign in to Bradford White Wave (needed once; your password never reaches this server).

 1. Open this address in a web browser on any computer or phone:

    {url}

 2. Sign in with your normal Wave account.
 3. The browser then ends on an error page. That is expected - it is trying to open the phone
    app. Do not close it yet: open the developer tools (F12), go to the Network tab, reload the
    page if the list is empty, click the request that is marked as failed / status 302, and
    copy the value of its "location" response header. It starts with
        com.bradfordwhiteapps.bwconnect://oauth/redirect?...
 4. Paste that whole address below (pasting just the code=... part also works).

The address holds a one-time code that expires after a few minutes, so do steps 2-4 promptly.
"""

PROBE_NOTICE = """\
`probe` looks for the Wave request that returns your notifications / fault history.

It sends about {count} read-only GET requests to {host}, 3 seconds apart, once. Each one is
a guessed endpoint name starting with 'get' and nothing else; it changes nothing (bwwatch
cannot send anything else). It is optional: the README describes other ways to find the request.

Run it only if you are comfortable with that:   ./bwctl probe --yes
"""


# --- helpers ----------------------------------------------------------------
def setup_logging(level: str) -> None:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stderr,
    )


def add_file_logging(path: Path) -> None:
    """Also keep a small rotating log (about 5 MB at most) in the data folder, so a failure is still
    explainable days later and everything bwwatch writes stays inside its own folder."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(str(path), maxBytes=1_000_000, backupCount=4, encoding="utf-8")
    except OSError as exc:
        log.warning("cannot write the log file %s: %s", path, exc)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ"))
    logging.getLogger().addHandler(handler)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(c) if c is not None else "" for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells)
    return "\n".join(lines)


def _open_existing_db(cfg: Config) -> Optional[sqlite3.Connection]:
    if not (cfg.data_dir / DB_NAME).exists():
        return None
    conn = connect(cfg.data_dir / DB_NAME)
    init_schema(conn)
    return conn


def _make_api(cfg: Config, stop: Optional[threading.Event] = None):
    store = TokenStore(cfg.data_dir / "token.json")
    tokens = TokenManager(cfg, store)
    return store, tokens, WaveApi(cfg, tokens, stop=stop)


def _csv_safe(value: Any) -> Any:
    """Stop spreadsheet programs treating text that starts with = + @ - as a formula."""
    if not isinstance(value, str):
        return value
    if value[:1] in ("=", "+", "@") or re.match(r"-[^\d.]", value):
        return "'" + value
    return value


# --- run --------------------------------------------------------------------
def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    stop = threading.Event()

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("received signal %d; finishing up", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    service = Service(cfg, stop=stop)
    try:
        return service.run()
    except AlreadyRunning as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 3
    except DatabaseTooNew as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 4
    except Exception:  # noqa: BLE001
        log.exception("bwwatch stopped because of an unexpected error")
        return 1


# --- login ------------------------------------------------------------------
def cmd_login(cfg: Config, args: argparse.Namespace) -> int:
    _store, tokens, api = _make_api(cfg)
    state, nonce = secrets.token_urlsafe(9), secrets.token_urlsafe(9)
    print(LOGIN_HELP.format(url=tokens.authorization_url(state, nonce)))
    try:
        pasted = input("Paste the address here: ")
    except EOFError:
        print("error: login needs an interactive terminal. Run:  docker compose run --rm bwwatch login", file=sys.stderr)
        return 2
    try:
        code, params = parse_redirect(pasted)
    except AuthError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if params.get("state") and params["state"] != state:
        print("note: that address came from a different sign-in attempt than the link above; fine if it is recent.")
    try:
        tokens.exchange_code(code)
    except WaveError as exc:
        print("error: %s\nCodes work once and expire in a few minutes: open the link again and repeat steps 2-4." % exc, file=sys.stderr)
        return 1
    try:
        appliances = api.list_appliances()
    except WaveError as exc:
        print("Signed in, but reading the appliance list failed: %s" % exc, file=sys.stderr)
        return 1
    print("\nSigned in. The refresh token is saved in %s (private to this container user)." % (cfg.data_dir / "token.json"))
    print("Appliances on this account: %d" % len(appliances))
    for item in appliances:
        print("  - %s  mac=%s  serial=%s  type=%s" % (item.get("friendlyName"), item.get("macAddress"), item.get("serialNumber"), item.get("applianceType")))
    print("\nNext:  docker compose run --rm bwwatch check     (then)     docker compose up -d")
    return 0


# --- check ------------------------------------------------------------------
def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    """Sign in, read everything once and show what bwwatch understands. Writes nothing to the database."""
    store, tokens, api = _make_api(cfg)
    problems = 0
    if not tokens.has_credentials():
        print("Sign-in:  NOT SIGNED IN. Run:  docker compose run --rm bwwatch login")
        return 1
    try:
        fetched = fetch(cfg, api)
    except AuthError as exc:
        print("Sign-in:  FAILED - %s\n          Run:  docker compose run --rm bwwatch login" % exc)
        return 1
    except WaveError as exc:
        print("FAILED - %s" % exc)
        return 1
    account = tokens.account_id or "?"
    print("Sign-in:  OK (account %s)" % (account[:8] + "..." if len(account) > 8 else account))
    print("Read-only: this tool only sends read requests; it cannot change any setting on the heater.")
    print("Appliances found: %d" % len(fetched.appliances))
    for a in fetched.appliances:
        print("\n  %s   mac=%s  serial=%s  type=%s" % (a.name, a.mac, a.serial or "-", a.model or "-"))
        if a.status_fetched:
            reading = extract_reading(a.status)
            print("    Settings:       %s" % (reading.summary() if reading else "no mode/setpoint fields in the status response"))
            if isinstance(a.status, dict):
                print("    Status fields:  %s" % ", ".join(sorted(map(str, a.status))))
        if a.faults_fetched:
            problems += _print_fault_check(cfg, a.faults, "    ")
        elif cfg.fault_request is not None and cfg.fault_request.per_appliance:
            print("    Fault history:  request failed (see errors below)")
    if fetched.account_faults_fetched:
        print("\n  Account-level fault history:")
        problems += _print_fault_check(cfg, fetched.account_faults, "    ")
    if cfg.fault_request is None:
        print("\nFault history:  NOT CONFIGURED. Without it bwwatch can only see settings changes and fault-like")
        print("                fields in the status data. See README 'Finding the fault request'.")
    errors = fetched.all_errors()
    for err in errors:
        print("\nERROR: %s" % err)
    print("\nAlert channels: %s" % (", ".join(cfg.channel_names) or "NONE - set NTFY_TOPIC (or another channel) in .env"))
    for warning in cfg.warnings:
        print("Warning: %s" % warning)
    return 1 if errors or problems else 0


def _print_fault_check(cfg: Config, payload: Any, indent: str) -> int:
    events, where = extract_events(payload, cfg.fault_options)
    if events is None:
        keys = ", ".join(sorted(map(str, payload))) if isinstance(payload, dict) else type(payload).__name__
        print("%sFault history:  response format NOT recognised (%s). Top-level: %s" % (indent, where, keys))
        print("%s                bwwatch will still alert when this response changes; set BW_FAULT_LIST_PATH to point at the list." % indent)
        return 0
    print("%sFault history:  %d entr%s (list found at: %s)" % (indent, len(events), "y" if len(events) == 1 else "ies", where))
    for event in events[:3]:
        print("%s                - %s" % (indent, describe_event(event, cfg.display_tz)))
    return 0


# --- call / probe -----------------------------------------------------------
def _resolve_ctx(api: WaveApi, spec: RequestSpec, mac: Optional[str]) -> Dict[str, str]:
    if not (spec.placeholders() - {"account_id"}):
        return {}
    if mac:
        return {"mac": mac, "serial": "", "name": ""}
    first = api.list_appliances()
    if not first:
        raise WaveError("the account has no appliances to fill {mac} from")
    item = first[0]
    ctx = {
        "mac": str(item.get("macAddress") or ""),
        "serial": str(item.get("serialNumber") or ""),
        "name": str(item.get("friendlyName") or ""),
    }
    print("(using your first appliance: %s)" % (ctx["name"] or ctx["mac"]), file=sys.stderr)
    return ctx


def cmd_call(cfg: Config, args: argparse.Namespace) -> int:
    """Make one READ-ONLY request and print the answer (handy for trying a request before putting it in .env)."""
    spec = RequestSpec.parse(args.request, label="request")  # refuses anything that could change the heater
    _store, _tokens, api = _make_api(cfg)
    try:
        ctx = _resolve_ctx(api, spec, args.mac)
        payload = api.call(spec, ctx)
    except WaveError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_probe(cfg: Config, args: argparse.Namespace) -> int:
    host = cfg.api_base.split("://", 1)[-1]
    if not args.yes:
        print(PROBE_NOTICE.format(count=REQUEST_COUNT, host=host))
        return 2
    _store, _tokens, api = _make_api(cfg)
    try:
        found = run_probe(api, print)
    except WaveError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if not found:
        print("\nNone of the guesses exist. See README 'Finding the fault request' for the other ways.")
        return 1
    print("\nCandidates. Try one and read the answer, for example:")
    print("  ./bwctl call \"GET /wave/%s?username={account_id}&macAddress={mac}\"" % found[0])
    print("If the answer is your notification / fault list, put that line in .env as:")
    print("  BW_FAULT_REQUEST=GET /wave/%s?username={account_id}&macAddress={mac}" % found[0])
    return 0


# --- viewing the log --------------------------------------------------------
def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    ok, message = healthcheck(cfg.data_dir)
    print("bwwatch %s   data: %s" % (__version__, cfg.data_dir))
    print("Service:         %s" % (("running - " + message) if ok else ("NOT running / unhealthy - " + message)))
    print("Poll interval:   every %d minutes" % (cfg.interval // 60))
    print("Alert channels:  %s" % (", ".join(cfg.channel_names) or "NONE"))
    print("Fault history:   %s" % (cfg.fault_request.describe() if cfg.fault_request else "NOT configured (see README)"))
    conn = _open_existing_db(cfg)
    if conn is None:
        print("Database:        none yet")
        return 0
    try:
        poll = conn.execute("SELECT * FROM polls ORDER BY id DESC LIMIT 1").fetchone()
        if poll:
            print("Last poll:       %s - %s" % (local_time(poll["finished_at"], cfg.display_tz), "ok" if poll["ok"] else "FAILED: %s" % poll["error"]))
        since = iso(datetime.now(timezone.utc) - timedelta(hours=24))
        day = conn.execute("SELECT COALESCE(SUM(ok), 0), COUNT(*) FROM polls WHERE started_at >= ?", (since,)).fetchone()
        print("Polls, last 24h: %d ok of %d" % (day[0], day[1]))
        print("\nHeaters:")
        for row in conn.execute("SELECT * FROM appliances ORDER BY name"):
            reading = conn.execute("SELECT * FROM readings WHERE mac = ? ORDER BY id DESC LIMIT 1", (row["mac"],)).fetchone()
            line = "  %s (%s)" % (row["name"], row["mac"])
            if reading:
                line += "  %s   [as of %s]" % (reading_from_row(reading).summary(), local_time(reading["taken_at"], cfg.display_tz))
            print(line)
        opened = conn.execute("SELECT * FROM faults WHERE kind = 'state' AND cleared_at IS NULL").fetchall()
        print("\nFault-like fields active now: %s" % (", ".join("%s=%s" % (r["description"], r["code"]) for r in opened) or "none"))
        total, new = conn.execute("SELECT COUNT(*), COALESCE(SUM(1 - baseline), 0) FROM faults").fetchone()
        print("Faults logged:   %d (%d seen since monitoring began, %d pre-existing)" % (total, new, total - new))
        latest = conn.execute("SELECT * FROM faults WHERE baseline = 0 ORDER BY id DESC LIMIT 3").fetchall()
        for row in latest:
            print("  - %s  code %s  %s" % (local_time(row["first_seen_at"], cfg.display_tz), row["code"] or "-", truncate(row["description"] or "", 70)))
        pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
        print("Alerts waiting to be delivered: %d" % pending)
        backups = list_backups(cfg.data_dir / "backups")
        print("Backups:         %d (newest: %s)" % (len(backups), backups[0].name if backups else "none yet"))
    finally:
        conn.close()
    return 0


def cmd_faults(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_existing_db(cfg)
    if conn is None:
        print("No database yet - has the service run?")
        return 1
    try:
        limit = -1 if args.all else args.limit
        rows = conn.execute(
            """SELECT f.*, a.name AS appliance,
                      (SELECT o.status FROM outbox o WHERE o.fault_id = f.id ORDER BY o.id DESC LIMIT 1) AS alert
               FROM faults f LEFT JOIN appliances a ON a.mac = f.mac
               ORDER BY f.first_seen_at DESC, f.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        if not rows:
            print("No faults logged yet.")
            return 0
        table = []
        for r in rows:
            flags = []
            if r["baseline"]:
                flags.append("pre-existing")
            if r["kind"] == "state":
                flags.append("cleared " + local_time(r["cleared_at"], cfg.display_tz) if r["cleared_at"] else "ACTIVE")
            if r["alert"] and not r["baseline"]:
                flags.append("alert " + r["alert"])
            table.append(
                (r["id"], local_time(r["first_seen_at"], cfg.display_tz), r["code"] or "-", r["appliance"] or r["mac"],
                 truncate(r["description"] or "", 60), ", ".join(flags))
            )
        print(_table(("ID", "FIRST SEEN", "CODE", "APPLIANCE", "DETAIL", "NOTES"), table))
        if args.raw:
            print()
            for r in rows:
                print("#%d raw: %s" % (r["id"], r["raw"]))
    finally:
        conn.close()
    return 0


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_existing_db(cfg)
    if conn is None:
        print("No database yet.", file=sys.stderr)
        return 1
    queries = {
        "faults": "SELECT f.id, f.first_seen_at, f.last_seen_at, f.cleared_at, a.name AS appliance, f.mac, f.kind, f.code, "
                  "f.description, f.occurred_at, f.seen_count, f.baseline, f.source FROM faults f "
                  "LEFT JOIN appliances a ON a.mac = f.mac ORDER BY f.id",
        "polls": "SELECT * FROM polls ORDER BY id",
        "readings": "SELECT r.taken_at, a.name AS appliance, r.mac, r.mode, r.mode_value, r.setpoint_f, r.temps "
                    "FROM readings r LEFT JOIN appliances a ON a.mac = r.mac ORDER BY r.id",
    }
    try:
        cursor = conn.execute(queries[args.table])
        out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
        try:
            writer = csv.writer(out)
            writer.writerow([d[0] for d in cursor.description])
            for row in cursor:
                writer.writerow([_csv_safe(v) for v in tuple(row)])
        finally:
            if args.out:
                out.close()
    finally:
        conn.close()
    if args.out:
        print("wrote %s" % args.out)
    return 0


# --- housekeeping -----------------------------------------------------------
def cmd_backup(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_existing_db(cfg)
    if conn is None:
        print("No database yet.", file=sys.stderr)
        return 1
    try:
        path = backup_now(conn, cfg.data_dir / "backups", cfg.backup_keep)
    finally:
        conn.close()
    print("verified backup written: %s" % path)
    return 0


def cmd_dbcheck(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_existing_db(cfg)
    if conn is None:
        print("No database yet.")
        return 1
    try:
        problems = integrity_check(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}.get(conn.execute("PRAGMA synchronous").fetchone()[0], "?")
        print("Integrity:   %s" % ("OK" if not problems else "PROBLEMS: " + "; ".join(problems[:5])))
        print("Journal:     %s, synchronous=%s" % (mode, sync))
        for name, count in table_counts(conn):
            print("  %-11s %d rows" % (name, count))
        backups = list_backups(cfg.data_dir / "backups")
        print("Backups:     %d" % len(backups))
        for b in backups[:5]:
            print("  %s  (%d KB)" % (b.name, b.stat().st_size // 1024))
        return 0 if not problems else 1
    finally:
        conn.close()


def cmd_test_notify(cfg: Config, args: argparse.Namespace) -> int:
    notifier = Notifier(cfg)
    if not notifier.channels:
        print("No notification channel is configured. Set NTFY_TOPIC (or Telegram / email / webhook / HA_WEBHOOK_URL) in .env.")
        return 1
    kind = args.event
    results = notifier.send(test_message(kind, local_time(iso(), cfg.display_tz), cfg.fault_priority))
    if not results:
        print("No channel is set to receive '%s' alerts (see the *_EVENTS settings)." % kind)
        return 1
    for name, error in results.items():
        print("  %-14s %s" % (name, "sent" if error is None else "FAILED: " + error))
    return 0 if all(error is None for error in results.values()) else 1


def cmd_verify(cfg: Config, args: argparse.Namespace) -> int:
    """Is the installation actually working? (Used by the installer; safe to run any time.)"""
    def progress(text: str) -> None:
        print("  ... " + text, file=sys.stderr, flush=True)

    checks = run_checks(cfg, wait=args.wait, since=args.since, progress=progress if args.wait else None)
    print(format_checks(checks))
    return 0 if overall_ok(checks) else 1


def cmd_setup(args: argparse.Namespace) -> int:
    """The guided setup wizard (and the small helper actions install.sh uses around it)."""
    from . import wizard  # only this command needs it

    data_dir = Path(os.environ.get("DATA_DIR") or "/data")
    prepare_runtime(data_dir)
    if args.print_env:
        return wizard.print_generated(data_dir)
    if args.cleanup:
        wizard.cleanup(data_dir)
        return 0
    if args.verify:
        return wizard.verify_installed(data_dir, os.environ, print)
    template_path = Path(__file__).resolve().parent.parent / ".env.example"
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError:
        print("error: the settings template (.env.example) is missing from the image", file=sys.stderr)
        return 1
    return wizard.Wizard(wizard.Console(), os.environ, data_dir, template).run()


def cmd_healthcheck(cfg: Config, args: argparse.Namespace) -> int:
    ok, message = healthcheck(cfg.data_dir)
    print(message)
    return 0 if ok else 1


# --- entry point ------------------------------------------------------------
COMMANDS: Dict[str, Callable[[Config, argparse.Namespace], int]] = {
    "run": cmd_run,
    "login": cmd_login,
    "check": cmd_check,
    "call": cmd_call,
    "probe": cmd_probe,
    "status": cmd_status,
    "faults": cmd_faults,
    "export": cmd_export,
    "backup": cmd_backup,
    "dbcheck": cmd_dbcheck,
    "test-notify": cmd_test_notify,
    "verify": cmd_verify,
    "healthcheck": cmd_healthcheck,
}
# `setup` and `version` take no loaded configuration, so they are handled before COMMANDS.
ALL_COMMANDS = tuple(COMMANDS) + ("setup", "version")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bwwatch",
        description="Bradford White Wave fault watcher. READ-ONLY: it never changes a setting on the water heater.",
    )
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING (default: INFO for `run`, WARNING otherwise)")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("run", help="run the watcher (the default)")
    sub.add_parser("login", help="sign in once through your browser and save the refresh token")
    sub.add_parser("check", help="read everything once and show what bwwatch understands (writes nothing)")
    p = sub.add_parser("call", help="make one read-only request and print the answer")
    p.add_argument("request", help="e.g. 'GET /wave/getApplianceStatus?macAddress={mac}'")
    p.add_argument("--mac", help="appliance to use for {mac} (default: your first appliance)")
    p = sub.add_parser("probe", help="look for the notifications / fault-history endpoint (optional)")
    p.add_argument("--yes", action="store_true", help="confirm sending the read-only guesses")
    sub.add_parser("status", help="summarise what has been recorded")
    p = sub.add_parser("faults", help="list logged faults")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all", action="store_true", help="show every fault")
    p.add_argument("--raw", action="store_true", help="also print each fault's stored raw data")
    p = sub.add_parser("export", help="write a table as CSV")
    p.add_argument("table", choices=("faults", "polls", "readings"))
    p.add_argument("--out", help="file to write (default: standard output)")
    sub.add_parser("backup", help="write a verified backup of the database now")
    sub.add_parser("dbcheck", help="verify the database and list backups")
    p = sub.add_parser("test-notify", help="send a test alert to every configured channel")
    p.add_argument("--event", default="info", choices=("info", "fault", "health", "recovered", "cleared", "setting"),
                   help="kind of alert to send (use 'fault' to test a Home Assistant automation)")
    p = sub.add_parser("verify", help="check the installation really works: polling, alerts, backups (used by the installer)")
    p.add_argument("--wait", type=float, default=0.0, help="give a just-started service this many seconds to finish its first poll")
    p.add_argument("--since", type=float, default=None, metavar="EPOCH",
                   help="only count the service and polls that started after this time (seconds since 1970; the installer uses it)")
    p = sub.add_parser("setup", help="the guided setup wizard (install.sh runs it for you)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--print-env", action="store_true", help="print the settings file the wizard produced (used by install.sh)")
    group.add_argument("--verify", action="store_true", help="check the installed .env reached the service exactly as chosen")
    group.add_argument("--cleanup", action="store_true", help="remove the wizard's temporary output files")
    sub.add_parser("healthcheck", help="exit 0 if the service is alive (used by Docker)")
    sub.add_parser("version", help="print the version")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    if command == "version":
        print(__version__)
        return 0
    level = args.log_level or os.environ.get("LOG_LEVEL") or ("INFO" if command == "run" else "WARNING")
    setup_logging(level)
    try:
        if command == "setup":  # must work even when the current settings are broken - it repairs them
            return cmd_setup(args)
        cfg = Config.from_env(os.environ)
        prepare_runtime(cfg.data_dir)
        if command == "run":
            raw = os.environ.get("LOG_FILE")
            if raw != "":
                add_file_logging(Path(raw) if raw else cfg.data_dir / "logs" / "bwwatch.log")
        return COMMANDS[command](cfg, args)
    except ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
