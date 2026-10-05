"""Guided setup, run inside the container:  ``bwwatch setup``

Asks a few plain questions and tests every answer for real - it sends an actual test alert, performs the
actual sign-in, calls the actual fault request - then writes a ready-to-use settings file that
``install.sh`` puts in place. It never asks for, receives or stores your Wave password (you type that
only into the real sign-in page in your own browser), and everything it creates lives in the data folder.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .config import Config, RequestSpec
from .errors import AuthError, ConfigError, WaveError
from .faults import extract_events
from .notify import Notifier, test_message
from .probe import REQUEST_COUNT, run_probe
from .readings import extract_reading
from .util import atomic_write, atomic_write_json, iso, local_time
from .wave import TokenManager, TokenStore, WaveApi, check_reachable, parse_redirect

GENERATED_ENV = "env.generated"
EXPECTED_JSON = "env.expected.json"
CHANNEL_PREFIXES = ("NTFY_", "TELEGRAM_", "SMTP_", "WEBHOOK_", "HA_")
# Settings the wizard asks about, in the order a broken one is looked for: (what to call it, which names).
SETTING_GROUPS = (
    ("the check interval", ("BW_POLL_INTERVAL_SECONDS",)),
    ("the time zone", ("DISPLAY_TZ",)),
    ("the Notifications request", ("BW_FAULT_REQUEST",)),
    ("the dead-man's-switch address", ("HEARTBEAT_URL",)),
    ("the ntfy settings", ("NTFY_",)),
    ("the Telegram settings", ("TELEGRAM_",)),
    ("the email settings", ("SMTP_",)),
    ("the webhook settings", ("WEBHOOK_",)),
    ("the Home Assistant settings", ("HA_",)),
)
SECRET_WORDS = ("TOKEN", "PASSWORD", "SECRET")
URL_KEYS = ("HA_WEBHOOK_URL", "WEBHOOK_URL", "HEARTBEAT_URL")


class Cancelled(Exception):
    """The person pressed Ctrl-C, or the input ended."""


# --- terminal ---------------------------------------------------------------
class Console:
    """Plain terminal I/O. Tests substitute a scripted version."""

    def __init__(self, stdin: Any = None, stdout: Any = None, color: Optional[bool] = None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.interactive = bool(getattr(self.stdin, "isatty", lambda: False)())
        if color is None:
            color = bool(getattr(self.stdout, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ
        self.color = color

    def _paint(self, text: str, code: str) -> str:
        return "\033[%sm%s\033[0m" % (code, text) if self.color else text

    def say(self, text: str = "", kind: Optional[str] = None) -> None:
        tag = {"ok": self._paint("[ OK ]", "32"), "warn": self._paint("[WARN]", "33"), "fail": self._paint("[FAIL]", "31;1")}.get(kind or "")
        self.stdout.write(("%s %s" % (tag, text) if tag else text) + "\n")
        self.stdout.flush()

    def heading(self, text: str) -> None:
        self.say("")
        self.say(self._paint(text, "1;36"))
        self.say("-" * len(text))

    def _read(self, prompt: str, secret: bool = False) -> str:
        try:
            if secret and self.interactive:
                return getpass.getpass(prompt)
            self.stdout.write(prompt)
            self.stdout.flush()
            line = self.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            raise Cancelled()
        if line == "":
            raise Cancelled()
        return line.rstrip("\r\n")

    def ask(
        self,
        prompt: str,
        default: Optional[str] = None,
        *,
        secret: bool = False,
        validate: Optional[Callable[[str], Optional[str]]] = None,
        allow_empty: bool = False,
    ) -> str:
        while True:
            hint = " [%s]" % default if default not in (None, "") and not secret else ""
            raw = self._read("%s%s: " % (prompt, hint), secret).strip()
            if raw == "" and default is not None:
                raw = str(default)
            if raw == "" and not allow_empty:
                self.say("Please type something (or press Ctrl-C to stop).", "warn")
                continue
            if raw and validate is not None:
                problem = validate(raw)
                if problem:
                    self.say(problem, "warn")
                    continue
            return raw

    def confirm(self, prompt: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            raw = self._read("%s [%s] " % (prompt, hint)).strip().lower()
            if raw == "":
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            self.say("Please answer y or n.", "warn")

    def choose(self, prompt: str, options: Sequence[Tuple[str, str]], default: Optional[Sequence[str]] = None, multi: bool = False) -> List[str]:
        keys = [k for k, _ in options]
        for number, (_, label) in enumerate(options, 1):
            self.say("  %d) %s" % (number, label))
        default_text = ",".join(str(keys.index(d) + 1) for d in (default or []))
        while True:
            raw = self._read("%s%s: " % (prompt, " [%s]" % default_text if default_text else "")).strip()
            if raw == "" and default:
                return list(default)
            picked: List[str] = []
            bad = False
            for token in re.split(r"[,\s]+", raw.lower()):
                if not token:
                    continue
                if token.isdigit() and 1 <= int(token) <= len(keys):
                    picked.append(keys[int(token) - 1])
                elif token in keys:
                    picked.append(token)
                else:
                    bad = True
            picked = list(dict.fromkeys(picked))
            if bad or not picked or (not multi and len(picked) != 1):
                self.say("Please choose %s from the list above." % ("one or more numbers, like 1,2," if multi else "one number"), "warn")
                continue
            return picked

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        self._read("%s " % prompt)


# --- the settings file ------------------------------------------------------
_ASSIGN = re.compile(r"^(#\s?)?([A-Z][A-Z0-9_]+)=(.*)$")
_SAFE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")


def template_keys(template: str) -> List[str]:
    """Setting names the template knows (active lines and one-space-commented ones)."""
    return list(dict.fromkeys(m.group(2) for m in (_ASSIGN.match(line) for line in template.splitlines()) if m))


def format_value(value: str) -> str:
    """A value as it must appear in a Docker Compose ``.env`` file so the service receives it unchanged.

    Verified against Compose: simple values stay bare; anything else goes in single quotes, which are
    fully literal (no ``$`` interpolation, no ``#`` comments); a value containing a single quote falls
    back to double quotes with ``\\``, ``"`` and ``$`` escaped.
    """
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("a setting cannot contain a line break")
    if value == "" or _SAFE.match(value):
        return value
    if "'" not in value:
        return "'" + value + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def render_env(template: str, settings: Mapping[str, str]) -> str:
    """The documented template with ``settings`` filled in (commented lines are switched on)."""
    remaining = {k: v for k, v in settings.items() if v != ""}
    out: List[str] = []
    for line in template.splitlines():
        match = _ASSIGN.match(line)
        if match and match.group(2) in remaining:
            key = match.group(2)
            out.append("%s=%s" % (key, format_value(remaining.pop(key))))
        else:
            out.append(line)
    if remaining:
        out += ["", "# ---- added by the setup wizard ----"]
        out += ["%s=%s" % (k, format_value(v)) for k, v in sorted(remaining.items())]
    return "\n".join(out) + "\n"


def mask(key: str, value: str) -> str:
    if key in URL_KEYS:
        parts = urlsplit(value)
        return "%s://%s/..." % (parts.scheme, parts.netloc) if parts.netloc else "..."
    if any(word in key for word in SECRET_WORDS):
        return "*" * 8
    return value


def generate_topic() -> str:
    return "bwwatch-" + secrets.token_hex(10)


# --- what happens to the file after the wizard (called by install.sh) -------
def print_generated(data_dir: Path, out: Any = None) -> int:
    out = out or sys.stdout
    path = Path(data_dir) / GENERATED_ENV
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 1
    if not text.strip():
        return 1
    out.write(text)
    return 0


def cleanup(data_dir: Path) -> None:
    for name in (GENERATED_ENV, EXPECTED_JSON):
        try:
            (Path(data_dir) / name).unlink()
        except FileNotFoundError:
            pass


def verify_installed(data_dir: Path, environ: Mapping[str, str], say: Callable[[str], None]) -> int:
    """After install.sh has put the new .env in place: does the service see exactly what the wizard chose?"""
    try:
        expected = json.loads((Path(data_dir) / EXPECTED_JSON).read_text(encoding="utf-8"))["keys"]
    except (OSError, ValueError, KeyError):
        say("There is nothing to verify (the wizard's record is missing).")
        return 1
    mismatched = sorted(k for k, v in expected.items() if environ.get(k) != v)
    if mismatched:
        say("These settings did NOT arrive exactly as chosen: %s" % ", ".join(mismatched))
        say("(Values are not shown. Re-run ./install.sh --reconfigure; if it persists, the value probably has an unusual character.)")
        return 1
    try:
        cfg = Config.from_env(environ)
    except ConfigError as exc:
        say("The settings load with an error: %s" % exc)
        return 1
    say("All %d settings reached the service exactly as chosen; alert channels: %s." % (len(expected), ", ".join(cfg.channel_names) or "none"))
    return 0


# --- the wizard -------------------------------------------------------------
SIGNIN_HELP = """\
Open this address in a web browser (on a computer is easiest):

    {url}

Then:
  1. BEFORE signing in, open the browser's developer tools (press F12) and click the "Network" tab.
  2. Sign in with your normal Wave account. (Your password goes only into that page, never here.)
  3. The browser ends on an error page. That is expected - it is trying to open the phone app.
     In the Network tab, click the request marked with status 302 (or shown in red / "blocked"),
     open "Headers", find the response header called "location", and copy its whole value.
     It starts with:  com.bradfordwhiteapps.bwconnect://oauth/redirect?...
  4. Paste it below.

The code inside works once and expires within minutes, so do steps 3 and 4 promptly.
"""


class Wizard:
    def __init__(self, console: Console, env: Mapping[str, str], data_dir: Path, template: str):
        self.c = console
        self.env = dict(env)
        self.data_dir = Path(data_dir)
        self.template = template
        self.keys = set(template_keys(template))
        self.existing: Dict[str, str] = {k: v for k, v in self.env.items() if k in self.keys and v != ""}
        self.values: Dict[str, str] = {}
        self.stop = threading.Event()
        self.heater_ctx: Optional[Dict[str, str]] = None
        self._bundle: Optional[Tuple[Config, TokenManager, WaveApi]] = None

    # --- helpers -------------------------------------------------------------
    def _env_for(self, channel_values: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        """Environment as it would be: existing + chosen settings; channels only if given (to test one at a time)."""
        env = {k: v for k, v in self.env.items() if not k.startswith(CHANNEL_PREFIXES)}
        env.update({k: v for k, v in self.existing.items() if not k.startswith(CHANNEL_PREFIXES)})
        env.update({k: v for k, v in self.values.items() if not k.startswith(CHANNEL_PREFIXES)})
        env.update(channel_values or {})
        env["DATA_DIR"] = str(self.data_dir)
        return env

    def _config(self, channel_values: Optional[Mapping[str, str]] = None) -> Config:
        return Config.from_env(self._env_for(channel_values))

    def _api(self) -> Tuple[Config, TokenManager, WaveApi]:
        """One Wave session for the whole conversation (so a test does not cost a token refresh)."""
        if self._bundle is None:
            cfg = self._config()
            tokens = TokenManager(cfg, TokenStore(self.data_dir / "token.json"))
            self._bundle = (cfg, tokens, WaveApi(cfg, tokens, stop=self.stop))
        return self._bundle

    def _existing_channels(self) -> Dict[str, str]:
        return {k: v for k, v in self.existing.items() if k.startswith(CHANNEL_PREFIXES)}

    # --- the conversation ----------------------------------------------------
    def run(self) -> int:
        try:
            self.welcome()
            if not self.step_connection():
                return 1
            self.step_alerts()
            self.step_timezone()
            if not self.step_signin():
                return 1
            self.step_fault_request()
            return 0 if self.step_review() else 130
        except Cancelled:
            self.c.say("")
            self.c.say("Stopped. Nothing has been installed yet" + (" (a sign-in, if you did one, is kept)." if (self.data_dir / "token.json").exists() else "."))
            self.c.say("Run ./install.sh again whenever you are ready.")
            return 130

    def welcome(self) -> None:
        self.c.say("")
        self.c.say("=" * 64)
        self.c.say(" bwwatch setup  -  about 5 minutes")
        self.c.say("=" * 64)
        self.c.say("bwwatch watches your Bradford White Wave water heater and alerts you when it")
        self.c.say("reports a fault. It only READS from your account: it can never change a setting")
        self.c.say("on the heater.")
        self.c.say("")
        self.c.say("You will need:  your phone (to receive alerts), and a web browser (to sign in to")
        self.c.say("Wave, once). Your Wave password is typed only into the real Wave sign-in page.")
        self.c.say("")
        self._repair_existing_settings()
        self.c.pause("Press Enter to begin")

    @staticmethod
    def _matcher(names: Sequence[str]) -> Callable[[str], bool]:
        return lambda key: key.startswith(tuple(n for n in names if n.endswith("_"))) or key in names

    def _load_error(self, drop: Callable[[str], bool] = lambda key: False) -> Optional[str]:
        """Why the settings (minus those ``drop`` matches) cannot be used, or None if they can."""
        env = {k: v for k, v in self.env.items() if not drop(k)}
        env["DATA_DIR"] = str(self.data_dir)
        try:
            Config.from_env(env)
        except ConfigError as exc:
            return str(exc)
        return None

    def _forget(self, drop: Callable[[str], bool]) -> None:
        self.env = {k: v for k, v in self.env.items() if not drop(k)}
        self.existing = {k: v for k, v in self.existing.items() if not drop(k)}

    def _repair_existing_settings(self) -> None:
        """A hand-edited .env with a mistake in it must not stop the wizard that fixes it."""
        problem = self._load_error()
        if problem is None:
            return
        self.c.say("Your current settings have a problem (%s)." % problem, "warn")
        for label, names in SETTING_GROUPS:
            drop = self._matcher(names)
            if any(drop(k) for k in self.existing) and self._load_error(drop) is None:
                self.c.say("It is in %s: the wizard will ask for those again and keep everything else." % label)
                self.c.say("Your old settings file is kept as a backup.")
                self._forget(drop)
                return
        self.c.say("The wizard will start from a clean slate. Your old settings file is kept as a backup.")
        self._forget(lambda key: key in self.keys)

    # step 1
    def step_connection(self) -> bool:
        self.c.heading("Step 1 of 6: Can this server reach Bradford White?")
        cfg = self._config()
        ok, detail = check_reachable(cfg.authorize_url, cfg.user_agent)
        if ok:
            self.c.say("The Wave sign-in server answered (%s)." % detail, "ok")
            return True
        self.c.say("Could not reach the Wave sign-in server: %s" % detail, "fail")
        self.c.say("Things to check: the server's internet connection and DNS (try:  ping -c1 1.1.1.1 ),")
        self.c.say("its clock (a wrong date breaks HTTPS), and any firewall / proxy rules.")
        return self.c.confirm("Continue anyway?", default=False)

    # step 2
    def step_alerts(self) -> None:
        self.c.heading("Step 2 of 6: How should bwwatch alert you?")
        current = self._existing_channels()
        names = Config.from_env(self._env_for(current)).channel_names if current else ()
        if names:  # (an address override such as NTFY_URL alone is not a channel)
            self.c.say("Alerts are already set up: %s." % ", ".join(names))
            if self.c.confirm("Keep them?", True):
                self.values.update(current)
                if self.c.confirm("Send a test alert now to make sure they still work?", True):
                    cfg = self._config(current)
                    results = Notifier(cfg).send(test_message("fault" if "homeassistant" in cfg.channel_names else "info", local_time(iso(), cfg.display_tz)))
                    for name, error in results.items():
                        self.c.say("%s: %s" % (name, "sent" if error is None else "FAILED - " + error), "ok" if error is None else "fail")
                return
        self.c.say("Pick how you want to be told. You can choose more than one (for example 1,2).")
        options = [
            ("ntfy", "ntfy - free push notifications to your phone (recommended)"),
            ("homeassistant", "Home Assistant (a webhook that triggers an automation)"),
            ("telegram", "Telegram message"),
            ("email", "Email"),
            ("webhook", "Another webhook (Slack, Discord, your own)"),
        ]
        while True:
            picked = self.c.choose("Your choice", options, default=["ntfy"], multi=True)
            for key in picked:
                self.c.say("")
                getattr(self, "_setup_" + key)()
            if any(k.startswith(CHANNEL_PREFIXES) for k in self.values):
                return
            self.c.say("No alert channel is set up. Without one, bwwatch records faults but nobody is told.", "warn")
            if self.c.confirm("Go back and pick a channel?", True):
                continue
            if self.c.ask("Type  yes  to continue with no alerts at all", validate=lambda t: None if t.lower() == "yes" else "Type exactly: yes") :
                return

    def _arrival_loop(self, label: str, vals: Dict[str, str], kind: str, tips: Sequence[str]) -> str:
        """Send a real test alert and ask whether it arrived. Returns 'ok', 'fix' or 'skip'."""
        while True:
            try:
                cfg = self._config(vals)
            except ConfigError as exc:
                self.c.say("Those settings are not valid: %s" % exc, "fail")
                return "fix"
            results = Notifier(cfg).send(test_message(kind, local_time(iso(), cfg.display_tz), cfg.fault_priority))
            error = next(iter(results.values()), "nothing was sent")
            if error is not None:
                self.c.say("The test alert could not be sent: %s" % error, "fail")
                options = [("retry", "Try again"), ("fix", "Change the settings"), ("skip", "Skip this channel for now")]
            else:
                self.c.say("Test alert sent via %s." % label, "ok")
                options = [("yes", "Yes, I got it"), ("retry", "No - send it again"), ("fix", "No - change the settings"),
                           ("keep", "I can't check right now - keep it anyway"), ("skip", "No - skip this channel")]
            choice = self.c.choose("Did it arrive on your device?" if error is None else "What now?", options, default=[options[0][0]])[0]
            if choice in ("yes", "keep"):
                return "ok"
            if choice == "retry":
                continue
            if choice == "fix":
                return "fix"
            return "skip"

    def _finish_channel(self, label: str, vals: Dict[str, str], kind: str, tips: Sequence[str], refine: Callable[[], Dict[str, str]]) -> None:
        while True:
            outcome = self._arrival_loop(label, vals, kind, tips)
            if outcome == "ok":
                self.values.update(vals)
                return
            if outcome == "skip":
                self.c.say("Skipped %s." % label)
                return
            for tip in tips:
                self.c.say("  - " + tip)
            vals = refine()

    def _setup_ntfy(self) -> None:
        self.c.say("ntfy shows push notifications on your phone, for free, with no account.")
        self.c.say("  1. Install the \"ntfy\" app: Android (Play Store or F-Droid) or iPhone (App Store).")
        self.c.say("  2. Open it, tap \"+\" and subscribe to this topic name, typed exactly:")
        base = {}
        if self.existing.get("NTFY_URL"):
            base["NTFY_URL"] = self.existing["NTFY_URL"]
            self.c.say("     (using your ntfy server %s)" % base["NTFY_URL"])
        elif self.c.confirm("Do you run your own ntfy server? (most people don't)", False):
            base["NTFY_URL"] = self.c.ask("Your ntfy server address", validate=lambda t: None if t.startswith(("https://", "http://")) else "It must start with https:// (or http://).")
            token = self.c.ask("Access token, if your server needs one (blank for none)", allow_empty=True, secret=True)
            if token:
                base["NTFY_TOKEN"] = token
        topic = self.existing.get("NTFY_TOPIC") or generate_topic()

        def vals_for(name: str) -> Dict[str, str]:
            return dict(base, NTFY_TOPIC=name)

        self.c.say("")
        self.c.say("        %s" % topic)
        self.c.say("")
        self.c.say("     (This random name is your private channel. Anyone who knows it could read your")
        self.c.say("      alerts, so don't share it.)")
        self.c.say("  Android: allow notifications for ntfy, and turn off battery optimisation for it,")
        self.c.say("  or Android may delay alerts.")
        self.c.pause("Press Enter once you have subscribed in the app")

        def refine() -> Dict[str, str]:
            name = self.c.ask("Topic name (letters, digits, - and _)", default=topic, validate=lambda t: None if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t) else "Use only letters, digits, - and _.")
            self.c.pause("Press Enter once the app is subscribed to  %s" % name)
            return vals_for(name)

        self._finish_channel("ntfy", vals_for(topic), "info", [
            "The topic in the app must match the one above character for character.",
            "Check the app's notification permission and battery settings (Android).",
        ], refine)

    def _setup_homeassistant(self) -> None:
        self.c.say("bwwatch will call a webhook in a Home Assistant automation you create:")
        self.c.say("  1. In Home Assistant, create an automation with a \"Webhook\" trigger.")
        self.c.say("  2. Turn OFF \"Only accessible from the local network\" (this server calls from the internet).")
        self.c.say("  3. Make sure this server can reach Home Assistant: your Home Assistant Cloud address,")
        self.c.say("     Tailscale, or an HTTPS reverse proxy. A VPS cannot see your home network by itself.")
        self.c.say("  The README has a ready-made automation: see the section \"Home Assistant\".")

        def ask_url() -> Dict[str, str]:
            url = self.c.ask("Paste the full webhook address", validate=lambda t: None if t.startswith(("https://", "http://")) else "It must start with https:// (or http://).")
            return {"HA_WEBHOOK_URL": url, "HA_WEBHOOK_EVENTS": "fault,health"}

        self._finish_channel("Home Assistant", ask_url(), "fault", [
            "In Home Assistant open the automation's \"Traces\": did it run? If not, the address or the",
            "\"local network only\" switch is the usual cause (Home Assistant ignores such calls silently).",
        ], ask_url)

    def _setup_telegram(self) -> None:
        self.c.say("Telegram: create a bot with @BotFather (it gives you a token), then send your bot any")
        self.c.say("message so it can reply to you. Your chat id is shown by @userinfobot.")

        def ask_both() -> Dict[str, str]:
            token = self.c.ask("Bot token", secret=True, validate=lambda t: None if re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", t) else "A bot token looks like 123456789:AAH... (from @BotFather).")
            chat = self.c.ask("Your chat id (a number)", validate=lambda t: None if re.fullmatch(r"-?\d+", t) else "A chat id is a number.")
            vals = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat}
            if self.existing.get("TELEGRAM_API_BASE"):  # an address you set by hand is respected
                vals["TELEGRAM_API_BASE"] = self.existing["TELEGRAM_API_BASE"]
            return vals

        self._finish_channel("Telegram", ask_both(), "info", [
            "Send your bot a message first - bots cannot start a conversation.",
            "Check the token and chat id.",
        ], ask_both)

    def _setup_email(self) -> None:
        self.c.say("Email (SMTP). For Gmail use smtp.gmail.com and an \"app password\" (not your normal password).")

        def ask_all() -> Dict[str, str]:
            host = self.c.ask("Mail server", default=self.existing.get("SMTP_HOST") or "smtp.gmail.com")
            security = self.c.choose("Connection security", [("starttls", "STARTTLS (port 587, the usual one)"), ("ssl", "SSL/TLS (port 465)"), ("none", "None (port 25, not recommended)")], default=["starttls"])[0]
            port = self.c.ask("Port", default={"starttls": "587", "ssl": "465", "none": "25"}[security], validate=lambda t: None if t.isdigit() and 0 < int(t) < 65536 else "A port is a number.")
            user = self.c.ask("Username (usually your email address; blank for none)", allow_empty=True)
            vals = {"SMTP_HOST": host, "SMTP_PORT": port, "SMTP_SECURITY": security}
            if user:
                vals["SMTP_USER"] = user
                vals["SMTP_PASSWORD"] = self.c.ask("Password (typing is hidden)", secret=True)
            vals["SMTP_TO"] = self.c.ask("Send alerts to (comma separated)", default=user or None, validate=lambda t: None if "@" in t else "That does not look like an email address.")
            return vals

        self._finish_channel("email", ask_all(), "info", [
            "Check the spam folder.",
            "Gmail needs an \"app password\" and 2-step verification turned on.",
        ], ask_all)

    def _setup_webhook(self) -> None:
        self.c.say("Any service that accepts a JSON POST: Slack/Discord/Mattermost incoming webhooks, or your own.")

        def ask_all() -> Dict[str, str]:
            url = self.c.ask("Webhook address", validate=lambda t: None if t.startswith(("https://", "http://")) else "It must start with https:// (or http://).")
            fmt = self.c.choose("Message format", [("json", "Plain JSON (your own service)"), ("slack", "Slack"), ("discord", "Discord")], default=["json"])[0]
            return {"WEBHOOK_URL": url, "WEBHOOK_FORMAT": fmt}

        self._finish_channel("the webhook", ask_all(), "info", ["Check the address, and that the service accepts a JSON POST."], ask_all)

    # step 3
    def step_timezone(self) -> None:
        self.c.heading("Step 3 of 6: Your time zone")
        self.c.say("Times in alerts are shown in your local time. Examples: America/New_York, America/Chicago,")
        self.c.say("America/Denver, America/Los_Angeles, Europe/London, Australia/Sydney, UTC.")

        def valid(name: str) -> Optional[str]:
            try:
                ZoneInfo(name)
            except Exception:  # noqa: BLE001 - unknown name or missing tz database
                return "I don't know that time zone. Use a name like America/Chicago (letters, with a slash)."
            return None

        default = self.existing.get("DISPLAY_TZ") or "UTC"
        while True:
            name = self.c.ask("Time zone", default=default, validate=valid)
            now = datetime.now(ZoneInfo(name)).strftime("%H:%M %Z on %A")
            if self.c.confirm("In that zone it is now %s. Is that right?" % now, True):
                self.values["DISPLAY_TZ"] = name
                return
            default = name

    # step 4
    def step_signin(self) -> bool:
        self.c.heading("Step 4 of 6: Sign in to Wave (once)")
        cfg, tokens, api = self._api()
        if tokens.has_credentials():
            self.c.say("This server is already signed in to Wave.")
            if self.c.confirm("Keep that sign-in?", True):
                try:
                    if self._show_heaters(api, cfg):
                        return True
                except AuthError:
                    self.c.say("That sign-in no longer works, so you need to sign in again.", "warn")
                except WaveError as exc:
                    self.c.say("Could not read your heater: %s" % exc, "fail")
        failures = 0
        while True:
            link = tokens.authorization_url(secrets.token_urlsafe(9), secrets.token_urlsafe(9))
            self.c.say(SIGNIN_HELP.format(url=link))
            while True:
                pasted = self.c.ask("Paste the address here (or type  new  for a fresh link)")
                if pasted.lower() == "new":
                    break
                try:
                    code, _params = parse_redirect(pasted)
                    tokens.exchange_code(code)
                except WaveError as exc:
                    failures += 1
                    self.c.say("That did not work: %s" % exc, "fail")
                    self.c.say("Codes work once and expire within minutes. Type  new  to get a fresh link.")
                    if failures >= 6 and not self.c.confirm("That is %d failures. Keep trying?" % failures, True):
                        return False
                    continue
                self.c.say("Signed in.", "ok")
                try:
                    if self._show_heaters(api, cfg):
                        return True
                except WaveError as exc:
                    self.c.say("Signed in, but reading your heater failed: %s" % exc, "fail")
                    return self.c.confirm("Continue anyway?", False)
                self.c.say("Removing that sign-in; let's try the right account.")
                try:
                    (self.data_dir / "token.json").unlink()
                except FileNotFoundError:
                    pass
                break

    def _show_heaters(self, api: WaveApi, cfg: Config) -> bool:
        items = api.list_appliances()
        if not items:
            self.c.say("Signed in, but this Wave account has no water heaters.", "warn")
            return False
        self.c.say("Water heaters on this account:")
        for item in items:
            mac = str(item.get("macAddress") or "")
            ctx = {"mac": mac, "serial": str(item.get("serialNumber") or ""), "name": str(item.get("friendlyName") or mac)}
            if self.heater_ctx is None:
                self.heater_ctx = ctx
            line = "  - %s   (serial %s, %s)" % (ctx["name"], ctx["serial"] or "?", item.get("applianceType") or "?")
            if cfg.status_request is not None:
                try:
                    reading = extract_reading(api.call(cfg.status_request, ctx))
                    line += "\n      right now: %s" % (reading.summary() if reading else "no mode or setpoint in the status")
                except WaveError as exc:
                    line += "\n      (could not read its status: %s)" % exc
            self.c.say(line)
        return self.c.confirm("Is that your water heater?", True)

    # step 5
    def step_fault_request(self) -> None:
        self.c.heading("Step 5 of 6: The Notifications list (where \"Fault 10\" appears)")
        current = self.existing.get("BW_FAULT_REQUEST")
        if current:
            self.c.say("A Notifications request is already set:  %s" % current)
            if self.c.confirm("Keep it?", True):
                self.values["BW_FAULT_REQUEST"] = current
                return
        self.c.say("bwwatch can already see your heater's mode and setpoint. To also read the app's")
        self.c.say("Notifications list it needs the name of one request that Bradford White has not")
        self.c.say("published. You can add it now or later (README: \"Finding the fault request\").")
        options = [
            ("have", "I have it (or want to try a guess)"),
            ("probe", "Try to find it for me (about %d read-only guesses, once)" % REQUEST_COUNT),
            ("skip", "Skip for now - I'll add it later"),
        ]
        choice = self.c.choose("Your choice", options, default=["skip"])[0]
        if choice == "have":
            self._enter_request()
        elif choice == "probe":
            self._probe_for_request()
        else:
            self.c.say("Skipped. Alerts for faults in the Notifications list will start once it is set.", "warn")
            self.c.say("Until then you are alerted to settings changes and fault-like status fields.")

    def _test_request(self, text: str) -> Optional[bool]:
        """Run a candidate request for real. True = looks right, False = rejected/failed, None = unsure."""
        try:
            spec = RequestSpec.parse(text, label="that request")
        except ConfigError as exc:
            self.c.say(str(exc), "fail")
            return False
        cfg, _tokens, api = self._api()
        ctx = self.heater_ctx or {}
        try:
            payload = api.call(spec, ctx)
        except WaveError as exc:
            self.c.say("The request failed: %s" % exc, "fail")
            return False
        events, where = extract_events(payload, cfg.fault_options)
        if events is None:
            keys = ", ".join(sorted(map(str, payload))) if isinstance(payload, dict) else type(payload).__name__
            self.c.say("It answered, but I could not find a list of entries in it (%s). Top level: %s" % (where, keys), "warn")
            return None
        self.c.say("It answered with %d entr%s (list found at: %s)." % (len(events), "y" if len(events) == 1 else "ies", where), "ok")
        for event in events[:3]:
            bits = ["code %s" % event.code if event.code else None, event.description, local_time(event.occurred_at, cfg.display_tz) if event.occurred_at else None,
                    {"cleared": "cleared", "active": "ACTIVE NOW"}.get(event.state or "")]
            self.c.say("    - " + " | ".join(b for b in bits if b))
        return True

    def _enter_request(self) -> None:
        self.c.say("Format:  METHOD /path?query [json body]   with {account_id} {mac} {serial} {name} as placeholders.")
        self.c.say("Example: GET /wave/getNotifications?username={account_id}&macAddress={mac}")
        while True:
            text = self.c.ask("The request (blank to skip)", allow_empty=True)
            if not text:
                self.c.say("Skipped.", "warn")
                return
            result = self._test_request(text)
            if result is False:
                if not self.c.confirm("Try another?", True):
                    return
                continue
            if self.c.confirm("Does this look like your notifications?" if result else "Use it anyway?", bool(result)):
                self.values["BW_FAULT_REQUEST"] = text
                return

    def _probe_for_request(self) -> None:
        self.c.say("This sends about %d read-only GET requests, 3 seconds apart, to Bradford White's API --" % REQUEST_COUNT)
        self.c.say("guessed names such as getNotifications. It changes nothing and runs once.")
        if not self.c.confirm("Go ahead?", True):
            return
        _cfg, _tokens, api = self._api()
        try:
            found = run_probe(api, self.c.say, self.stop)
        except WaveError as exc:
            self.c.say("The search stopped: %s" % exc, "fail")
            return
        if not found:
            self.c.say("None of the guesses exist. See the README for other ways to find it.", "warn")
            return
        for name in found:
            text = "GET /wave/%s?username={account_id}&macAddress={mac}" % name
            self.c.say("")
            self.c.say("Trying  %s" % text)
            result = self._test_request(text)
            if result is not False and self.c.confirm("Is this your notifications list?", bool(result)):
                self.values["BW_FAULT_REQUEST"] = text
                return
        self.c.say("None of them was it. You can add the request later (README).", "warn")

    # step 6
    def step_review(self) -> bool:
        self.c.heading("Step 6 of 6: Review and save")
        if self.c.confirm("Change advanced options? (check interval, dead-man's switch - most people skip this)", False):
            interval = self.c.ask(
                "Check every how many seconds? (3600 = hourly, minimum 300)",
                default=self.existing.get("BW_POLL_INTERVAL_SECONDS") or "3600",
                validate=lambda t: None if t.isdigit() and 300 <= int(t) <= 86400 else "Enter a whole number from 300 to 86400.",
            )
            self.values["BW_POLL_INTERVAL_SECONDS"] = interval
            beat = self.c.ask("Dead-man's-switch address (e.g. from healthchecks.io; blank for none)", allow_empty=True,
                              validate=lambda t: None if t.startswith(("https://", "http://")) else "It must start with https://")
            if beat:
                self.values["HEARTBEAT_URL"] = beat
        final = self.final_settings()
        self.c.say("")
        self.c.say("These settings will be saved:")
        for key in sorted(final):
            self.c.say("  %-26s %s" % (key, mask(key, final[key])))
        if "BW_FAULT_REQUEST" not in final:
            self.c.say("  (no Notifications request yet - see step 5)")
        try:
            self._config({k: v for k, v in final.items() if k.startswith(CHANNEL_PREFIXES)})
        except ConfigError as exc:
            self.c.say("These settings do not load: %s" % exc, "fail")
            return False
        if not self.c.confirm("Save them?", True):
            self.c.say("Nothing was saved.")
            return False
        self.write_outputs(final)
        self.c.say("")
        self.c.say("Saved. Next the installer puts these settings in place, starts bwwatch and checks that", "ok")
        self.c.say("everything works. Keep this window open.")
        return True

    def final_settings(self) -> Dict[str, str]:
        """Everything that goes into the new .env: kept existing settings, overlaid with this run's choices."""
        final = {k: v for k, v in self.existing.items() if not k.startswith(CHANNEL_PREFIXES)}
        final.update(self.values)
        return {k: v for k, v in final.items() if v != ""}

    def write_outputs(self, final: Mapping[str, str]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.data_dir / GENERATED_ENV, render_env(self.template, final).encode("utf-8"), 0o600)
        atomic_write_json(self.data_dir / EXPECTED_JSON, {"keys": dict(final), "written_at": iso()}, 0o600)
