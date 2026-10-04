"""The guided setup: scripted conversations against the mock cloud, and the settings file it writes."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import unittest
from pathlib import Path
from typing import Dict, List
from unittest import mock

from bwwatch import cli, wizard
from bwwatch.config import Config
from bwwatch.notify import test_message as make_test_message
from bwwatch.wave import TokenStore

from .helpers import WaveTestCase, make_env
from .mock_wave import REDIRECT

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / ".env.example").read_text(encoding="utf-8")
FAULT_REQUEST = "GET /wave/getNotifications?username={account_id}&macAddress={mac}"


# --- scripted terminal --------------------------------------------------------
class Script:
    """Answers each prompt by what it asked, not by its position: ``Script(("substring", "answer", ...))``.

    A rule with several answers gives them in turn, then repeats the last. A prompt no rule matches fails the
    test (and says what was asked) instead of hanging.
    """

    def __init__(self, *rules):
        self.rules = [(substring, list(answers)) for substring, *answers in rules]
        self.asked: List[str] = []

    def answer(self, prompt: str) -> str:
        self.asked.append(prompt)
        for substring, queue in self.rules:
            if substring in prompt:
                value = queue.pop(0) if len(queue) > 1 else queue[0]
                value = value() if callable(value) else value
                if isinstance(value, BaseException):
                    raise value
                return value
        raise AssertionError("the wizard asked something the script does not expect: %r" % prompt)


class ScriptedConsole(wizard.Console):
    def __init__(self, script: Script):
        self.out = io.StringIO()
        super().__init__(stdin=io.StringIO(""), stdout=self.out, color=False)
        self.script = script

    def _read(self, prompt: str, secret: bool = False) -> str:
        self.out.write(prompt + "\n")  # what is typed is NOT echoed: the transcript is only what the wizard printed
        try:
            return self.script.answer(prompt)
        except (KeyboardInterrupt, EOFError):
            raise wizard.Cancelled()

    @property
    def text(self) -> str:
        return self.out.getvalue()


# --- a tiny model of how Docker Compose reads a .env file ---------------------
def read_compose_env(text: str) -> Dict[str, str]:
    """The rules, verified against `docker compose config`: bare values end at ' #' and interpolate ``$``;
    single quotes are literal; double quotes understand only \\\\ \\" and \\$ here."""
    out: Dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            raise AssertionError("line %d is not NAME=value: %r" % (number, line))
        name, raw = match.groups()
        if raw.startswith("'"):
            if not raw.endswith("'") or len(raw) < 2 or "'" in raw[1:-1]:
                raise AssertionError("line %d: bad single quotes: %r" % (number, raw))
            out[name] = raw[1:-1]
        elif raw.startswith('"'):
            if not raw.endswith('"') or len(raw) < 2:
                raise AssertionError("line %d: bad double quotes: %r" % (number, raw))
            body, value, i = raw[1:-1], [], 0
            while i < len(body):
                if body[i] == "\\":
                    if i + 1 >= len(body) or body[i + 1] not in '\\"$':
                        raise AssertionError("line %d: escape this model does not know: %r" % (number, raw))
                    value.append(body[i + 1])
                    i += 2
                elif body[i] == '"':
                    raise AssertionError("line %d: unescaped quote: %r" % (number, raw))
                else:
                    value.append(body[i])
                    i += 1
            out[name] = "".join(value)
        else:
            if "$" in raw or " #" in raw or raw != raw.strip():
                raise AssertionError("line %d: a bare value Compose would change: %r" % (number, raw))
            out[name] = raw
    return out


NASTY = [
    "plain-value_1.2/3:4@5%6+7=8,9",
    "pa$$word",
    "has # hash",
    "it's",
    "it's \"quoted\" and $HOME and \\ back",
    "spaces and, commas",
    "=equals=",
    "https://hooks.example/a?b=c&d=e#frag",
    "päss wörd ✓",
    "${BRACED} $(command) `tick`",
    "back\\slash\\n not a newline",
    "trailing-quote'",
    "'leading-quote",
    '"double-leading',
]


class SettingsFile(unittest.TestCase):
    def test_every_awkward_value_reaches_compose_unchanged(self):
        for value in NASTY:
            rendered = wizard.format_value(value)
            self.assertEqual(read_compose_env("NAME=%s\n" % rendered), {"NAME": value}, "value %r rendered as %r" % (value, rendered))

    def test_simple_values_stay_bare_and_blank_stays_blank(self):
        self.assertEqual(wizard.format_value("America/Chicago"), "America/Chicago")
        self.assertEqual(wizard.format_value("fault,health"), "fault,health")
        self.assertEqual(wizard.format_value(""), "")

    def test_a_line_break_cannot_be_smuggled_into_a_setting(self):
        for bad in ("a\nb", "a\rb", "a\x00b"):
            with self.assertRaises(ValueError):
                wizard.format_value(bad)

    def test_the_whole_file_reads_back_as_chosen(self):
        settings = {
            "NTFY_TOPIC": "bwwatch-0123456789abcdef0123",
            "NTFY_PASSWORD": "pa$$ w0rd # x",          # commented in the template: gets switched on
            "SMTP_HOST": "mail.example",
            "SMTP_TO": "a@example.com,b@example.com",
            "BW_FAULT_REQUEST": FAULT_REQUEST + ' {"mac": "{mac}"}',
            "HA_WEBHOOK_URL": "https://ha.example/api/webhook/abc?x=1&y=2",
            "DISPLAY_TZ": "America/Chicago",
            "BW_POLL_INTERVAL_SECONDS": "1800",
            "NOT_IN_TEMPLATE": "kept at the end",
            "TELEGRAM_CHAT_ID": "",                     # blank means unset
        }
        text = wizard.render_env(TEMPLATE, settings)
        parsed = {k: v for k, v in read_compose_env(text).items() if v != ""}
        self.assertEqual(parsed, {k: v for k, v in settings.items() if v != ""})
        self.assertEqual(text.count("\nNTFY_TOPIC="), 1, "a setting appears once, in its documented place")
        self.assertIn("# ---- added by the setup wizard ----", text)
        self.assertTrue(text.endswith("\n"))
        # the explanations survive, so the file stays self-documenting
        self.assertIn("# ----- 1. WHERE ALERTS GO", text)
        self.assertIn("NO Wave PASSWORD GOES HERE", text)

    def test_the_rendered_template_with_nothing_chosen_equals_the_shipped_defaults(self):
        parsed = read_compose_env(wizard.render_env(TEMPLATE, {}))
        shipped = {k: v for k, v in read_compose_env(TEMPLATE).items()}
        self.assertEqual(parsed, shipped)

    def test_the_generated_file_is_a_working_configuration(self):
        text = wizard.render_env(TEMPLATE, {"NTFY_TOPIC": "bwwatch-0123456789abcdef0123", "DISPLAY_TZ": "Europe/London", "BW_FAULT_REQUEST": FAULT_REQUEST})
        cfg = Config.from_env(read_compose_env(text))
        self.assertEqual(cfg.channel_names, ("ntfy",))
        self.assertEqual(cfg.display_tz, "Europe/London")
        self.assertIsNotNone(cfg.fault_request)

    def test_template_keys_cover_everything_the_code_reads(self):
        keys = set(wizard.template_keys(TEMPLATE))
        for name in ("NTFY_TOPIC", "HA_WEBHOOK_URL", "BW_FAULT_REQUEST", "DISPLAY_TZ", "BW_POLL_INTERVAL_SECONDS", "HEARTBEAT_URL", "LOG_FILE"):
            self.assertIn(name, keys)
        self.assertNotIn("DATA_DIR", keys, "fixed by docker-compose.yml")

    def test_secrets_are_masked_when_shown_for_review(self):
        self.assertEqual(wizard.mask("NTFY_TOPIC", "bwwatch-abc"), "bwwatch-abc", "the topic is shown: you type it into the app")
        self.assertNotIn("hunter2", wizard.mask("SMTP_PASSWORD", "hunter2"))
        self.assertNotIn("123456:ABC", wizard.mask("TELEGRAM_BOT_TOKEN", "123456:ABCdef"))
        shown = wizard.mask("HA_WEBHOOK_URL", "https://ha.example/api/webhook/secret-id?x=1")
        self.assertEqual(shown, "https://ha.example/...")
        self.assertNotIn("secret-id", shown)
        self.assertEqual(wizard.mask("HEARTBEAT_URL", "not a url"), "...")

    def test_generated_topics_are_long_and_different(self):
        topics = {wizard.generate_topic() for _ in range(20)}
        self.assertEqual(len(topics), 20)
        for topic in topics:
            self.assertRegex(topic, r"^bwwatch-[0-9a-f]{20}$")


class TerminalInput(unittest.TestCase):
    def console(self, text: str) -> wizard.Console:
        return wizard.Console(stdin=io.StringIO(text), stdout=io.StringIO(), color=False)

    def test_ask_default_validate_and_retry(self):
        console = self.console("\nabc\n12\n")
        self.assertEqual(console.ask("n", default="7"), "7")
        self.assertEqual(console.ask("n", validate=lambda t: None if t.isdigit() else "digits only"), "12")
        self.assertIn("digits only", console.stdout.getvalue())

    def test_ask_refuses_blank_unless_allowed(self):
        console = self.console("\n\nreal\n")
        self.assertEqual(console.ask("x"), "real")
        self.assertIn("Please type something", console.stdout.getvalue())
        self.assertEqual(self.console("\n").ask("x", allow_empty=True), "")

    def test_end_of_input_cancels_instead_of_looping_forever(self):
        with self.assertRaises(wizard.Cancelled):
            self.console("").ask("x")
        with self.assertRaises(wizard.Cancelled):
            self.console("").confirm("x?")

    def test_confirm(self):
        console = self.console("maybe\nY\nno\n\n")
        self.assertTrue(console.confirm("a?", False))
        self.assertFalse(console.confirm("b?", True))
        self.assertTrue(console.confirm("c?", True), "blank takes the default")
        self.assertIn("Please answer y or n", console.stdout.getvalue())

    def test_choose_one_and_many(self):
        options = [("a", "Apple"), ("b", "Banana"), ("c", "Cherry")]
        console = self.console("9\n2\n1, c\n\nb a\n")
        self.assertEqual(console.choose("pick", options), ["b"])
        self.assertEqual(console.choose("pick", options, multi=True), ["a", "c"])
        self.assertEqual(console.choose("pick", options, default=["c"]), ["c"])
        self.assertEqual(console.choose("pick", options, multi=True), ["b", "a"])
        self.assertIn("Please choose one number", console.stdout.getvalue())

    def test_choose_rejects_two_when_one_is_wanted(self):
        console = self.console("1 2\n3\n")
        self.assertEqual(console.choose("pick", [("a", "A"), ("b", "B"), ("c", "C")]), ["c"])

    def test_a_secret_is_not_echoed_when_a_terminal_hides_it(self):
        console = wizard.Console(stdin=io.StringIO(""), stdout=io.StringIO(), color=False)
        console.interactive = True
        with mock.patch("getpass.getpass", return_value="hunter2") as hidden:
            self.assertEqual(console.ask("Password", secret=True), "hunter2")
        hidden.assert_called_once()

    def test_colour_only_on_a_terminal(self):
        out = io.StringIO()
        wizard.Console(stdin=io.StringIO(""), stdout=out).say("hello", "ok")
        self.assertNotIn("\033", out.getvalue())
        out = io.StringIO()
        wizard.Console(stdin=io.StringIO(""), stdout=out, color=True).say("hello", "fail")
        self.assertIn("\033[31;1m", out.getvalue())


# --- the conversation ---------------------------------------------------------
class WizardCase(WaveTestCase):
    def wizard_env(self, **extra: str) -> Dict[str, str]:
        env = make_env(self.mock, self.data, **extra)
        for fresh in ("NTFY_TOPIC", "BW_FAULT_REQUEST"):  # a first install has neither
            env.pop(fresh, None)
        env.update(extra)
        return env

    def run_wizard(self, script: Script, **extra: str):
        console = ScriptedConsole(script)
        with mock.patch("bwwatch.wizard.check_reachable", wraps=wizard.check_reachable):
            status = wizard.Wizard(console, self.wizard_env(**extra), self.data, TEMPLATE).run()
        return status, console

    def happy_script(self, *overrides) -> Script:
        code = self.mock.issue_login_code()
        return Script(
            *overrides,
            ("Press Enter to begin", ""),
            ("Your choice", "1", "1"),
            ("Press Enter once you have subscribed", ""),
            ("Did it arrive", "1"),
            ("Time zone", "America/Chicago"),
            ("In that zone it is now", "y"),
            ("Paste the address here", REDIRECT + "?state=x&code=" + code),
            ("Is that your water heater", "y"),
            ("The request (blank to skip)", FAULT_REQUEST),
            ("Does this look like your notifications", "y"),
            ("Change advanced options", "n"),
            ("Save them", "y"),
        )

    def generated(self) -> Dict[str, str]:
        return read_compose_env((self.data / wizard.GENERATED_ENV).read_text(encoding="utf-8"))

    def expected(self) -> Dict[str, str]:
        return json.loads((self.data / wizard.EXPECTED_JSON).read_text(encoding="utf-8"))["keys"]


class HappyPath(WizardCase):
    def test_a_first_install_end_to_end(self):
        script = self.happy_script()
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)

        settings = self.generated()
        self.assertRegex(settings["NTFY_TOPIC"], r"^bwwatch-[0-9a-f]{20}$")
        self.assertEqual(settings["DISPLAY_TZ"], "America/Chicago")
        self.assertEqual(settings["BW_FAULT_REQUEST"], FAULT_REQUEST)
        expected = self.expected()
        self.assertEqual({k: settings.get(k) for k in expected}, expected, "the record install.sh verifies against")
        self.assertTrue(set(expected) >= {"NTFY_TOPIC", "DISPLAY_TZ", "BW_FAULT_REQUEST"})

        # what it wrote is a complete, valid configuration for the service
        cfg = Config.from_env(dict(settings, DATA_DIR=str(self.data)))
        self.assertEqual(cfg.channel_names, ("ntfy",))
        self.assertIsNotNone(cfg.fault_request)

        # a real test alert was sent, to the topic that was shown, and a real sign-in was made
        sent = self.mock.sinks["ntfy"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["json"]["topic"], settings["NTFY_TOPIC"])
        self.assertIn("TEST", (sent[0]["json"]["title"] + sent[0]["json"]["message"]).upper())
        self.assertIn(settings["NTFY_TOPIC"], console.text)
        self.assertTrue(TokenStore(self.data / "token.json").load()["refresh_token"])
        self.assertIn("Basement", console.text)
        self.assertIn("Signed in.", console.text)

    def test_the_files_it_leaves_are_private(self):
        status, _ = self.run_wizard(self.happy_script())
        self.assertEqual(status, 0)
        for name in (wizard.GENERATED_ENV, wizard.EXPECTED_JSON, "token.json"):
            mode = stat.S_IMODE(os.stat(self.data / name).st_mode)
            self.assertEqual(mode & 0o077, 0, "%s must not be readable by other users (mode %o)" % (name, mode))

    def test_it_never_asks_for_the_wave_password_or_username(self):
        script = self.happy_script()
        self.run_wizard(script)
        asked = " ".join(script.asked).lower()
        self.assertNotIn("password", asked)
        self.assertNotIn("username", asked)
        self.assertNotIn("email", asked)

    def test_nothing_is_ever_sent_to_a_change_endpoint(self):
        self.run_wizard(self.happy_script())
        self.assertEqual(self.mock.writes, [])
        names = {r["path"].rsplit("/", 1)[-1] for r in self.mock.requests if r["path"].startswith("/wave/")}
        self.assertEqual(names, {"getApplianceList", "getApplianceStatus", "getNotifications"})

    def test_the_pasted_one_time_code_is_never_printed_back(self):
        _, console = self.run_wizard(self.happy_script())
        self.assertNotIn("one-time-login-code", console.text)
        self.assertNotIn("refresh-token", console.text)

    def test_it_writes_nothing_outside_the_data_folder(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        before = {p for p in self.tmp.rglob("*") if p.is_file()}
        self.run_wizard(self.happy_script())
        created = {p for p in self.tmp.rglob("*") if p.is_file()} - before
        for path in created:
            self.assertIn(self.data, path.parents, "%s is outside the data folder" % path)

    def test_the_outputs_are_removed_by_cleanup_and_only_those(self):
        self.run_wizard(self.happy_script())
        (self.data / "bwwatch.db").write_text("keep")
        wizard.cleanup(self.data)
        self.assertFalse((self.data / wizard.GENERATED_ENV).exists())
        self.assertFalse((self.data / wizard.EXPECTED_JSON).exists())
        self.assertTrue((self.data / "token.json").exists())
        self.assertTrue((self.data / "bwwatch.db").exists())
        wizard.cleanup(self.data)  # and doing it again is fine


class RetriesAndErrors(WizardCase):
    def test_a_bad_paste_an_expired_code_and_a_fresh_link(self):
        good = self.mock.issue_login_code("fresh-code-" + "z" * 24)
        script = self.happy_script(
            ("Paste the address here",
             "not an address at all",
             REDIRECT + "?code=" + "e" * 40,  # a code the server never issued / has expired
             "new",
             REDIRECT + "?code=" + good),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertEqual(console.text.count("That did not work"), 2)
        self.assertEqual(console.text.count("Open this address in a web browser"), 2, "'new' printed a fresh link")
        self.assertIn("Signed in.", console.text)

    def test_the_wrong_account_is_removed_and_redone(self):
        first = self.mock.issue_login_code("first-code-" + "a" * 24)
        second = self.mock.issue_login_code("second-code-" + "b" * 24)
        script = self.happy_script(
            ("Is that your water heater", "n", "y"),
            ("Paste the address here", REDIRECT + "?code=" + first, REDIRECT + "?code=" + second),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertIn("Removing that sign-in", console.text)
        self.assertEqual(console.text.count("Signed in."), 2)

    def test_a_failed_test_alert_offers_retry_and_then_works(self):
        self.mock.sink_status["ntfy"] = 500
        calls = {"n": 0}

        def repair():
            calls["n"] += 1
            self.mock.sink_status["ntfy"] = 200
            return "1"  # "Try again"

        script = self.happy_script(("What now?", repair))
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertEqual(calls["n"], 1)
        self.assertIn("The test alert could not be sent", console.text)
        self.assertEqual(len(self.mock.sinks["ntfy"]), 1)
        self.assertIn("NTFY_TOPIC", self.generated())

    def test_alert_not_arriving_lets_you_change_the_topic(self):
        script = self.happy_script(
            ("Did it arrive", "3", "1"),                 # "No - change the settings", then "Yes"
            ("Topic name", "my-own-topic_42"),
            ("Press Enter once the app is subscribed", ""),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertEqual(self.generated()["NTFY_TOPIC"], "my-own-topic_42")
        self.assertEqual(len(self.mock.sinks["ntfy"]), 2, "one test per topic")
        self.assertIn("must match the one above character for character", console.text)

    def test_at_least_one_channel_is_enforced(self):
        script = self.happy_script(
            ("Did it arrive", "5", "1"),                 # skip the first channel...
            ("Go back and pick a channel", "y"),         # ...refuse to proceed without one...
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertIn("No alert channel is set up", console.text)
        self.assertIn("NTFY_TOPIC", self.generated())

    def test_going_without_alerts_needs_a_deliberate_yes(self):
        script = self.happy_script(
            ("Did it arrive", "5"),
            ("Go back and pick a channel", "n"),
            ("Type  yes  to continue", "maybe", "yes"),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertNotIn("NTFY_TOPIC", self.expected())
        self.assertIn("Type exactly: yes", console.text)

    def test_timezone_must_exist_and_be_confirmed(self):
        script = self.happy_script(("Time zone", "Mars/Olympus", "Europe/Paris"), ("In that zone it is now", "n", "y"))
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertIn("I don't know that time zone", console.text)
        self.assertEqual(self.generated()["DISPLAY_TZ"], "Europe/Paris")

    def test_a_rejected_fault_request_is_explained_and_can_be_replaced(self):
        script = self.happy_script(
            ("The request (blank to skip)", "GET /wave/changeSetpoint?macAddress={mac}", "GET /wave/getNothingHere", FAULT_REQUEST),
            ("Try another", "y"),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertEqual(self.generated()["BW_FAULT_REQUEST"], FAULT_REQUEST)
        self.assertEqual(self.mock.writes, [], "the change endpoint was refused before anything was sent")
        self.assertEqual(self.mock.api_hits("changeSetpoint"), [])
        self.assertIn("The request failed", console.text)

    def test_an_unreadable_answer_can_still_be_kept_deliberately(self):
        self.mock.notifications = {"weird": "shape"}
        script = self.happy_script(("Use it anyway", "y"))  # the question differs when the shape is unknown
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertIn("could not find a list of entries", console.text)
        self.assertEqual(self.generated()["BW_FAULT_REQUEST"], FAULT_REQUEST)

    def test_skipping_the_fault_request_is_allowed_and_said_plainly(self):
        script = self.happy_script(("Your choice", "1", "3"))
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertNotIn("BW_FAULT_REQUEST", self.expected())
        self.assertIn("no Notifications request yet", console.text)

    def test_the_search_for_the_request_finds_it_and_tests_it(self):
        script = self.happy_script(
            ("Your choice", "1", "2"),
            ("Go ahead?", "y"),
            ("Is this your notifications list", "y"),
        )
        with mock.patch("bwwatch.probe.PROBE_PAUSE_SECONDS", 0):
            status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        self.assertEqual(self.generated()["BW_FAULT_REQUEST"], "GET /wave/getNotifications?username={account_id}&macAddress={mac}")
        self.assertEqual(self.mock.writes, [])

    def test_declining_the_search_sends_nothing_extra(self):
        script = self.happy_script(("Your choice", "1", "2"), ("Go ahead?", "n"))
        status, _ = self.run_wizard(script)
        self.assertEqual(status, 0)
        self.assertEqual(len(self.mock.api_hits("getNotifications")), 0)
        self.assertNotIn("BW_FAULT_REQUEST", self.expected())

    def test_an_unreachable_wave_server_is_reported_with_advice(self):
        script = self.happy_script(("Continue anyway", "n"))
        with mock.patch("bwwatch.wizard.check_reachable", return_value=(False, "name resolution failed")):
            console = ScriptedConsole(script)
            status = wizard.Wizard(console, self.wizard_env(), self.data, TEMPLATE).run()
        self.assertEqual(status, 1)
        self.assertIn("Could not reach the Wave sign-in server: name resolution failed", console.text)
        self.assertIn("its clock", console.text)
        self.assertFalse((self.data / wizard.GENERATED_ENV).exists())

    def test_declining_the_final_save_writes_nothing(self):
        script = self.happy_script(("Save them", "n"))
        status, console = self.run_wizard(script)
        self.assertEqual(status, 130)
        self.assertIn("Nothing was saved", console.text)
        self.assertFalse((self.data / wizard.GENERATED_ENV).exists())
        self.assertFalse((self.data / wizard.EXPECTED_JSON).exists())

    def test_advanced_options_are_validated_and_saved(self):
        script = self.happy_script(
            ("Change advanced options", "y"),
            ("Check every how many seconds", "10", "abc", "1800"),
            ("Dead-man's-switch address", "nope", "https://hc.example/ping/abc"),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        settings = self.generated()
        self.assertEqual(settings["BW_POLL_INTERVAL_SECONDS"], "1800")
        self.assertEqual(settings["HEARTBEAT_URL"], "https://hc.example/ping/abc")
        self.assertNotIn("hc.example/ping/abc", console.text.split("These settings will be saved:")[1], "the secret address is masked in the review")


class Cancelling(WizardCase):
    def test_ctrl_c_at_any_prompt_stops_cleanly_with_130_and_writes_nothing(self):
        # find out how many prompts a happy run has, then cancel at each one in turn
        probe = self.happy_script()
        self.run_wizard(probe)
        total = len(probe.asked)
        self.assertGreater(total, 10)
        for stop_at in range(total):
            with self.subTest(prompt=stop_at):
                for leftover in self.data.iterdir():
                    leftover.unlink()
                seen = {"n": 0}
                script = self.happy_script()
                real = script.answer

                def answer(prompt, real=real, seen=seen, stop_at=stop_at):
                    if seen["n"] == stop_at:
                        raise KeyboardInterrupt()
                    seen["n"] += 1
                    return real(prompt)

                script.answer = answer
                status, console = self.run_wizard(script)
                self.assertEqual(status, 130, "stopped at prompt %d\n%s" % (stop_at, console.text[-400:]))
                self.assertIn("Nothing has been installed yet", console.text)
                self.assertFalse((self.data / wizard.GENERATED_ENV).exists())
                self.assertFalse((self.data / wizard.EXPECTED_JSON).exists())

    def test_closed_input_is_a_cancel_not_a_crash(self):
        console = wizard.Console(stdin=io.StringIO(""), stdout=io.StringIO(), color=False)
        status = wizard.Wizard(console, self.wizard_env(), self.data, TEMPLATE).run()
        self.assertEqual(status, 130)


class Rerunning(WizardCase):
    def existing_install(self) -> Dict[str, str]:
        return self.wizard_env(
            NTFY_TOPIC="bwwatch-aaaaaaaaaaaaaaaaaaaa",
            DISPLAY_TZ="America/Denver",
            BW_FAULT_REQUEST=FAULT_REQUEST,
            HEARTBEAT_URL=self.mock.url + "/heartbeat/abc",
            BW_POLL_INTERVAL_SECONDS="1800",
        )

    def test_reconfiguring_keeps_what_you_keep_and_tests_it(self):
        self.sign_in(self.cfg())
        script = Script(
            ("Press Enter to begin", ""),
            ("Keep them?", "y"),
            ("Send a test alert now", "y"),
            ("Time zone", ""),                           # keeps America/Denver
            ("In that zone it is now", "y"),
            ("Keep that sign-in?", "y"),
            ("Is that your water heater", "y"),
            ("Keep it?", "y"),
            ("Change advanced options", "n"),
            ("Save them", "y"),
        )
        console = ScriptedConsole(script)
        status = wizard.Wizard(console, self.existing_install(), self.data, TEMPLATE).run()
        self.assertEqual(status, 0, console.text)
        settings = self.generated()
        self.assertEqual(settings["NTFY_TOPIC"], "bwwatch-aaaaaaaaaaaaaaaaaaaa", "the topic your phone is subscribed to is not changed")
        self.assertEqual(settings["DISPLAY_TZ"], "America/Denver")
        self.assertEqual(settings["BW_FAULT_REQUEST"], FAULT_REQUEST)
        self.assertEqual(settings["BW_POLL_INTERVAL_SECONDS"], "1800", "unrelated settings are carried over")
        self.assertEqual(settings["HEARTBEAT_URL"], self.mock.url + "/heartbeat/abc")
        self.assertEqual(len(self.mock.sinks["ntfy"]), 1)
        self.assertNotIn("Paste the address", console.text, "no second sign-in")

    def test_an_expired_sign_in_is_noticed_and_redone(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        self.mock.revoke_all_tokens()
        good = self.mock.issue_login_code("again-" + "q" * 28)
        script = Script(
            ("Press Enter to begin", ""),
            ("Keep them?", "y"),
            ("Send a test alert now", "n"),
            ("Time zone", ""),
            ("In that zone it is now", "y"),
            ("Keep that sign-in?", "y"),
            ("Paste the address here", REDIRECT + "?code=" + good),
            ("Is that your water heater", "y"),
            ("Keep it?", "y"),
            ("Change advanced options", "n"),
            ("Save them", "y"),
        )
        console = ScriptedConsole(script)
        status = wizard.Wizard(console, self.existing_install(), self.data, TEMPLATE).run()
        self.assertEqual(status, 0, console.text)
        self.assertIn("no longer works", console.text)
        self.assertIn("Signed in.", console.text)

    def test_a_broken_setting_is_asked_again_and_the_rest_is_kept(self):
        env = self.wizard_env(NTFY_TOPIC="bwwatch-bbbbbbbbbbbbbbbbbbbb", BW_POLL_INTERVAL_SECONDS="5")  # below the minimum
        script = self.happy_script(("Keep them?", "y"), ("Send a test alert now", "n"))
        console = ScriptedConsole(script)
        status = wizard.Wizard(console, env, self.data, TEMPLATE).run()
        self.assertEqual(status, 0, console.text)
        self.assertIn("Your current settings have a problem", console.text)
        self.assertIn("It is in the check interval", console.text)
        settings = self.generated()
        self.assertEqual(settings["NTFY_TOPIC"], "bwwatch-bbbbbbbbbbbbbbbbbbbb", "the working part was not thrown away")
        self.assertEqual(settings["BW_POLL_INTERVAL_SECONDS"], "3600", "the broken value is gone")

    def test_a_broken_channel_is_found_by_name(self):
        env = self.wizard_env(SMTP_HOST="mail.example")  # email needs SMTP_TO as well
        console = ScriptedConsole(Script(("Press Enter to begin", KeyboardInterrupt())))
        wiz = wizard.Wizard(console, env, self.data, TEMPLATE)
        with self.assertRaises(wizard.Cancelled):
            wiz.welcome()
        self.assertIn("It is in the email settings", console.text)
        self.assertNotIn("SMTP_HOST", wiz.existing)
        self.assertIn("NTFY_URL", wiz.existing, "unrelated settings stay")

    def test_two_mistakes_at_once_fall_back_to_a_clean_slate(self):
        env = self.wizard_env(BW_POLL_INTERVAL_SECONDS="5", HEARTBEAT_URL="ftp://nope")
        console = ScriptedConsole(Script(("Press Enter to begin", KeyboardInterrupt())))
        wiz = wizard.Wizard(console, env, self.data, TEMPLATE)
        with self.assertRaises(wizard.Cancelled):
            wiz.welcome()
        self.assertIn("clean slate", console.text)
        self.assertEqual(wiz.existing, {})
        self.assertIsNone(wiz._load_error(), "what remains is usable")


class OtherChannels(WizardCase):
    def test_home_assistant_is_tested_with_a_fault_shaped_alert(self):
        script = self.happy_script(
            ("Your choice", "2", "1"),
            ("Paste the full webhook address", self.mock.url + "/ha/api/webhook/secret-webhook-id"),
            ("Did it arrive", "1"),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        sent = self.mock.sinks["ha"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["json"]["event"], "fault")
        self.assertEqual(sent[0]["json"]["fault"]["code"], "99")
        self.assertIn("TEST", sent[0]["json"]["title"])
        settings = self.generated()
        self.assertEqual(settings["HA_WEBHOOK_EVENTS"], "fault,health")
        self.assertNotIn("secret-webhook-id", console.text, "the webhook id is never printed")
        self.assertNotIn("NTFY_TOPIC", self.expected())
        self.assertEqual(settings["NTFY_TOPIC"], "", "ntfy was not chosen")

    def test_telegram_validates_and_hides_the_token(self):
        token = "123456789:" + "A" * 30
        script = self.happy_script(
            ("Your choice", "3", "1"),
            ("Bot token", "bad", token),
            ("Your chat id", "abc", "424242"),
            ("Did it arrive", "1"),
        )
        status, console = self.run_wizard(script, TELEGRAM_API_BASE=self.mock.url + "/telegram")
        self.assertEqual(status, 0, console.text)
        self.assertEqual(self.mock.sinks["telegram"][0]["token"], token)
        self.assertEqual(self.generated()["TELEGRAM_BOT_TOKEN"], token)
        self.assertNotIn(token, console.text, "the token is never echoed")

    def test_email_is_tested_with_a_real_message(self):
        from .mock_wave import FakeSMTP

        smtp = FakeSMTP().start()
        self.addCleanup(smtp.stop)
        script = self.happy_script(
            ("Your choice", "4", "1"),
            ("Mail server", "127.0.0.1"),
            ("Connection security", "3"),
            ("Port", "x", str(smtp.port)),
            ("Username", ""),
            ("Send alerts to", "nonsense", "me@example.com"),
            ("Did it arrive", "1"),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        settings = self.generated()
        self.assertEqual((settings["SMTP_HOST"], settings["SMTP_SECURITY"], settings["SMTP_TO"]), ("127.0.0.1", "none", "me@example.com"))
        self.assertNotIn("SMTP_USER", settings)
        self.assertEqual(len(smtp.messages), 1)
        self.assertIn("A port is a number", console.text)
        self.assertIn("does not look like an email address", console.text)

    def test_a_generic_webhook_and_several_channels_at_once(self):
        script = self.happy_script(
            ("Your choice", "1 5", "1"),
            ("Webhook address", self.mock.url + "/webhook"),
            ("Message format", "1"),
            ("Did it arrive", "1"),
        )
        status, console = self.run_wizard(script)
        self.assertEqual(status, 0, console.text)
        settings = self.generated()
        self.assertIn("NTFY_TOPIC", settings)
        self.assertEqual(settings["WEBHOOK_FORMAT"], "json")
        self.assertEqual(len(self.mock.sinks["ntfy"]), 1)
        self.assertEqual(len(self.mock.sinks["webhook"]), 1)
        cfg = Config.from_env(dict(settings, DATA_DIR=str(self.data)))
        self.assertEqual(sorted(cfg.channel_names), ["ntfy", "webhook"])


class AfterTheWizard(WaveTestCase):
    """What install.sh asks of the container once the new settings file is in place."""

    def record(self, keys: Dict[str, str]) -> None:
        (self.data / wizard.EXPECTED_JSON).write_text(json.dumps({"keys": keys}), encoding="utf-8")

    def test_verify_installed_accepts_exactly_what_was_chosen(self):
        env = make_env(self.mock, self.data)
        chosen = {k: env[k] for k in ("NTFY_TOPIC", "NTFY_URL", "BW_FAULT_REQUEST")}
        self.record(chosen)
        lines = []
        self.assertEqual(wizard.verify_installed(self.data, env, lines.append), 0)
        self.assertIn("All 3 settings reached the service exactly as chosen", lines[0])
        self.assertIn("ntfy", lines[0])

    def test_verify_installed_names_the_setting_that_was_mangled_but_not_its_value(self):
        env = make_env(self.mock, self.data)
        self.record({"NTFY_TOPIC": env["NTFY_TOPIC"], "HA_WEBHOOK_URL": "https://ha.example/api/webhook/very-secret"})
        lines = []
        self.assertEqual(wizard.verify_installed(self.data, env, lines.append), 1)
        shown = "\n".join(lines)
        self.assertIn("HA_WEBHOOK_URL", shown)
        self.assertNotIn("very-secret", shown)
        self.assertNotIn(env["NTFY_TOPIC"], shown)

    def test_verify_installed_catches_a_dollar_sign_eaten_by_the_env_file(self):
        env = make_env(self.mock, self.data, SMTP_PASSWORD="pa$word")
        self.record({"SMTP_PASSWORD": "pa$$word"})
        self.assertEqual(wizard.verify_installed(self.data, env, lambda _: None), 1)

    def test_verify_installed_without_a_record(self):
        lines = []
        self.assertEqual(wizard.verify_installed(self.data, make_env(self.mock, self.data), lines.append), 1)
        self.assertIn("nothing to verify", lines[0])

    def test_verify_installed_reports_a_configuration_error(self):
        env = make_env(self.mock, self.data, BW_POLL_INTERVAL_SECONDS="5")
        self.record({"BW_POLL_INTERVAL_SECONDS": "5"})
        lines = []
        self.assertEqual(wizard.verify_installed(self.data, env, lines.append), 1)
        self.assertIn("load with an error", lines[0])

    def test_print_generated(self):
        out = io.StringIO()
        self.assertEqual(wizard.print_generated(self.data, out), 1, "nothing written yet")
        (self.data / wizard.GENERATED_ENV).write_text("A=1\n", encoding="utf-8")
        self.assertEqual(wizard.print_generated(self.data, out), 0)
        self.assertEqual(out.getvalue(), "A=1\n")
        (self.data / wizard.GENERATED_ENV).write_text("\n", encoding="utf-8")
        self.assertEqual(wizard.print_generated(self.data, io.StringIO()), 1, "an empty file is not a result")


class SetupCommand(WaveTestCase):
    """`bwwatch setup` as the container runs it."""

    def run_setup(self, *argv, env=None, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        environ = make_env(self.mock, self.data)
        environ.update(env or {})
        with mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("bwwatch.cli.prepare_runtime"):
            code = cli.main(["setup", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_print_env_and_cleanup_and_verify(self):
        code, out, _ = self.run_setup("--print-env")
        self.assertEqual(code, 1)
        (self.data / wizard.GENERATED_ENV).write_text("NTFY_TOPIC=abc\n", encoding="utf-8")
        code, out, _ = self.run_setup("--print-env")
        self.assertEqual((code, out), (0, "NTFY_TOPIC=abc\n"))
        code, out, _ = self.run_setup("--verify")
        self.assertEqual(code, 1)
        self.assertIn("nothing to verify", out)
        code, _, _ = self.run_setup("--cleanup")
        self.assertEqual(code, 0)
        self.assertFalse((self.data / wizard.GENERATED_ENV).exists())

    def test_setup_runs_even_when_the_current_settings_are_broken(self):
        # a broken .env must not stop the wizard that repairs it
        code, out, err = self.run_setup(env={"BW_POLL_INTERVAL_SECONDS": "1"}, stdin="")
        self.assertEqual(code, 130, out + err)  # reached the conversation, then the input ended
        self.assertNotIn("configuration error", err)
        self.assertIn("bwwatch setup", out)

    def test_setup_is_listed_in_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("setup", out.getvalue())
        self.assertIn("verify", out.getvalue())


class TestMessages(unittest.TestCase):
    def test_every_kind_is_clearly_labelled_as_a_test(self):
        for kind in wizard_test_kinds():
            message = make_test_message(kind, "10:00 UTC")
            self.assertIn("TEST", message.title.upper() + message.body.upper(), kind)

    def test_the_fault_test_carries_structured_data_for_home_assistant(self):
        message = make_test_message("fault", "10:00 UTC", 4)
        self.assertEqual(message.kind, "fault")
        self.assertEqual(message.data["fault"]["code"], "99")
        self.assertIn("appliance", message.data)
        self.assertEqual(message.priority, 4)


def wizard_test_kinds():
    from bwwatch.notify import TEST_TAGS

    return list(TEST_TAGS)


if __name__ == "__main__":
    unittest.main()
