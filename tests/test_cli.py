"""The command-line tools, run in-process against the mock cloud."""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import unittest
from unittest import mock

from bwwatch import cli
from bwwatch.config import ConfigError
from bwwatch.wave import TokenStore

from .helpers import MAC, WaveTestCase
from .mock_wave import REDIRECT


class CliCase(WaveTestCase):
    def run_cmd(self, fn, cfg, **ns):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = fn(cfg, argparse.Namespace(**ns))
        return code, out.getvalue()


class Login(CliCase):
    def login(self, pasted: str):
        cfg = self.cfg()
        with mock.patch("builtins.input", return_value=pasted):
            code, out = self.run_cmd(cli.cmd_login, cfg)
        return cfg, code, out

    def test_login_flow_saves_a_working_refresh_token(self):
        code = self.mock.issue_login_code()
        cfg, status, out = self.login(REDIRECT + "?state=init&code=" + code)
        self.assertEqual(status, 0, out)
        self.assertIn("Signed in", out)
        self.assertIn("Basement", out)
        self.assertTrue(TokenStore(cfg.data_dir / "token.json").load()["refresh_token"])
        self.assertNotIn(code, out.split("Paste the address here:")[-1], "the one-time code is not echoed back")

    def test_the_printed_link_matches_the_real_sign_in_page_parameters(self):
        cfg = self.cfg()
        with mock.patch("builtins.input", return_value=""):
            _, out = self.run_cmd(cli.cmd_login, cfg)
        link = [line.strip() for line in out.splitlines() if line.strip().startswith(self.mock.url)][0]
        query = dict(part.split("=", 1) for part in link.split("?", 1)[1].split("&"))
        self.assertEqual(query["client_id"], "7899415d-1c23-46d8-8a79-4c15ed5f7f22")
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["scope"], "openid+email+offline_access+profile")
        self.assertEqual(query["redirect_uri"], "com.bradfordwhiteapps.bwconnect%3A%2F%2Foauth%2Fredirect")
        self.assertTrue(link.split("?")[0].endswith("/auth/authorize"))

    def test_helpful_errors(self):
        cfg, status, out = self.login("https://consumer.bradfordwhiteapps.com/x/api/CombinedSigninAndSignup/confirmed")
        self.assertEqual(status, 1)
        self.assertIn("intermediate", out)
        cfg, status, out = self.login(REDIRECT + "?code=" + "x" * 40)  # a code the server never issued
        self.assertEqual(status, 1)
        self.assertIn("expire", out)
        self.assertIsNone(TokenStore(cfg.data_dir / "token.json").load())

    def test_no_terminal(self):
        cfg = self.cfg()
        with mock.patch("builtins.input", side_effect=EOFError):
            status, out = self.run_cmd(cli.cmd_login, cfg)
        self.assertEqual(status, 2)
        self.assertIn("interactive terminal", out)


class Check(CliCase):
    def test_check_shows_what_bwwatch_sees_and_writes_nothing(self):
        cfg = self.cfg(HA_WEBHOOK_URL=self.mock.url + "/ha/api/webhook/xyz")
        self.sign_in(cfg)
        self.mock.notifications = {"notifications": [{"id": 1, "faultCode": 10, "message": "Heating element failure", "timestamp": 1760000000}]}
        status, out = self.run_cmd(cli.cmd_check, cfg)
        self.assertEqual(status, 0, out)
        for text in ("Sign-in:  OK", "cannot change any setting", "Appliances found: 1", "Basement", MAC,
                     "mode Heat Pump, setpoint 120°F", "Fault history:  1 entry", "code 10", "ntfy, homeassistant"):
            self.assertIn(text, out)
        self.assertFalse((cfg.data_dir / "bwwatch.db").exists(), "check does not touch the database")

    def test_check_explains_a_missing_fault_request(self):
        cfg = self.cfg(BW_FAULT_REQUEST="")
        self.sign_in(cfg)
        status, out = self.run_cmd(cli.cmd_check, cfg)
        self.assertEqual(status, 0)
        self.assertIn("NOT CONFIGURED", out)
        self.assertIn("Finding the fault request", out)

    def test_check_reports_an_unrecognised_fault_response(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        self.mock.notifications = {"message": "hello"}
        status, out = self.run_cmd(cli.cmd_check, cfg)
        self.assertIn("NOT recognised", out)
        self.assertIn("BW_FAULT_LIST_PATH", out)

    def test_check_when_not_signed_in(self):
        status, out = self.run_cmd(cli.cmd_check, self.cfg())
        self.assertEqual(status, 1)
        self.assertIn("login", out)

    def test_check_when_the_login_has_expired(self):
        cfg = self.cfg()
        TokenStore(cfg.data_dir / "token.json").save("dead")
        status, out = self.run_cmd(cli.cmd_check, cfg)
        self.assertEqual(status, 1)
        self.assertIn("FAILED", out)
        self.assertIn("login", out)


class Call(CliCase):
    def test_call_prints_the_answer(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        status, out = self.run_cmd(cli.cmd_call, cfg, request="GET /wave/getApplianceStatus?macAddress={mac}", mac=None)
        self.assertEqual(status, 0, out)
        self.assertEqual(json.loads(out[out.index("{"):])["setpointFahrenheit"], 120)

    def test_call_refuses_anything_that_changes_the_heater_before_sending_anything(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        for request in ("GET /wave/changeSetpoint?mac_address={mac}&temperature=130", "GET /wave/changeOpMode?mac_address={mac}&mode=3",
                        "POST /wave/updateThing", "PUT /wave/getNotifications"):
            with self.subTest(request):
                with self.assertRaises(ConfigError):
                    cli.cmd_call(cfg, argparse.Namespace(request=request, mac=None))
        self.assertEqual(self.mock.requests, [])
        self.assertEqual(self.mock.writes, [])


class Probe(CliCase):
    def test_probe_needs_explicit_confirmation(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        status, out = self.run_cmd(cli.cmd_probe, cfg, yes=False)
        self.assertEqual(status, 2)
        self.assertIn("read-only GET", out)
        self.assertIn("bwwatch probe --yes", out)
        self.assertEqual(self.mock.requests, [], "nothing is sent without --yes")

    def test_probe_finds_a_real_endpoint_with_few_requests_and_no_writes(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        self.mock.disabled_routes.add("getNotifications")
        self.mock.extra_routes["getFaultHistory"] = (200, {"faults": []})
        with mock.patch.object(cli, "PROBE_PAUSE_SECONDS", 0):
            status, out = self.run_cmd(cli.cmd_probe, cfg, yes=True)
        self.assertEqual(status, 0, out)
        self.assertRegex(out, r"getFaultHistory\s+HTTP 200\s+<-- different")
        self.assertIn("BW_FAULT_REQUEST=GET /wave/getFaultHistory?username={account_id}&macAddress={mac}", out)
        self.assertRegex(out, r"getAlerts\s+no such endpoint")
        api_calls = self.mock.hits("/wave/")
        self.assertLessEqual(len(api_calls), 1 + 1 + len(cli.PROBE_NAMES))
        self.assertTrue(all(r["method"] == "GET" for r in api_calls))
        self.assertTrue(all(r["path"].rsplit("/", 1)[-1].startswith("get") for r in api_calls))
        self.assertEqual(self.mock.writes, [])

    def test_probe_with_nothing_found_says_so(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        self.mock.disabled_routes.add("getNotifications")
        with mock.patch.object(cli, "PROBE_PAUSE_SECONDS", 0):
            status, out = self.run_cmd(cli.cmd_probe, cfg, yes=True)
        self.assertEqual(status, 1)
        self.assertIn("None of the guesses exist", out)

    def test_probe_stops_immediately_if_rate_limited(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        self.mock.extra_routes["getNotificationList"] = (429, {"message": "slow down"})
        with mock.patch.object(cli, "PROBE_PAUSE_SECONDS", 0):
            status, out = self.run_cmd(cli.cmd_probe, cfg, yes=True)
        self.assertEqual(status, 1)
        self.assertIn("slow down (HTTP 429)", out)
        self.assertLess(len(self.mock.hits("/wave/")), 2 + len(cli.PROBE_NAMES))


class LogViews(CliCase):
    def setUp(self):
        super().setUp()
        self.svc = self.service()
        self.svc.cycle()
        self.mock.notifications = {"notifications": [
            {"id": 1, "faultCode": 10, "message": "Heating element failure", "timestamp": 1760000000},
            {"id": 2, "faultCode": 7, "message": "=HYPERLINK(\"http://evil\")", "timestamp": 1760000500},
        ]}
        self.svc.cycle()
        self.cfg_ = self.svc.cfg

    def test_status(self):
        code, out = self.run_cmd(cli.cmd_status, self.cfg_)
        self.assertEqual(code, 0)
        for text in ("Basement", "mode Heat Pump, setpoint 120°F", "Faults logged:   2", "Polls, last 24h: 2 ok of 2", "ntfy"):
            self.assertIn(text, out)

    def test_faults_table(self):
        code, out = self.run_cmd(cli.cmd_faults, self.cfg_, limit=20, all=False, raw=False)
        self.assertEqual(code, 0)
        self.assertIn("Heating element failure", out)
        self.assertIn("alert sent", out)
        code, out = self.run_cmd(cli.cmd_faults, self.cfg_, limit=20, all=False, raw=True)
        self.assertIn('"faultCode": 10', out)

    def test_export_csv_is_valid_and_formula_safe(self):
        code, out = self.run_cmd(cli.cmd_export, self.cfg_, table="faults", out=None)
        rows = list(csv.reader(io.StringIO(out)))
        header, body = rows[0], rows[1:]
        self.assertIn("code", header)
        self.assertEqual(len(body), 2)
        descriptions = [r[header.index("description")] for r in body]
        self.assertIn("'=HYPERLINK(\"http://evil\")", descriptions, "text starting with = is defused for spreadsheets")
        for table in ("polls", "readings"):
            code, out = self.run_cmd(cli.cmd_export, self.cfg_, table=table, out=None)
            self.assertEqual(code, 0)
            self.assertEqual(len(list(csv.reader(io.StringIO(out)))), 3)

    def test_backup_and_dbcheck(self):
        code, out = self.run_cmd(cli.cmd_backup, self.cfg_)
        self.assertEqual(code, 0)
        self.assertIn("verified backup written", out)
        code, out = self.run_cmd(cli.cmd_dbcheck, self.cfg_)
        self.assertEqual(code, 0)
        for text in ("Integrity:   OK", "wal", "synchronous=FULL", "Backups:     1"):
            self.assertIn(text, out)


class TestNotify(CliCase):
    def test_sends_to_every_channel(self):
        cfg = self.cfg(HA_WEBHOOK_URL=self.mock.url + "/ha/api/webhook/abc")
        code, out = self.run_cmd(cli.cmd_test_notify, cfg, event="info")
        self.assertEqual(code, 0, out)
        self.assertIn("ntfy", out)
        self.assertIn("homeassistant", out)
        self.assertEqual((len(self.mock.sinks["ntfy"]), len(self.mock.sinks["ha"])), (1, 1))

    def test_a_test_fault_exercises_a_home_assistant_automation(self):
        cfg = self.cfg(HA_WEBHOOK_URL=self.mock.url + "/ha/api/webhook/abc", HA_WEBHOOK_EVENTS="fault")
        self.run_cmd(cli.cmd_test_notify, cfg, event="info")
        self.assertEqual(self.mock.sinks["ha"], [], "filtered out, exactly as a real info alert would be")
        code, out = self.run_cmd(cli.cmd_test_notify, cfg, event="fault")
        self.assertEqual(code, 0)
        payload = self.mock.sinks["ha"][0]["json"]
        self.assertEqual((payload["event"], payload["fault"]["code"]), ("fault", "99"))
        self.assertIn("TEST", payload["title"])
        self.assertIn("only a test", payload["fault"]["description"])

    def test_no_channel(self):
        code, out = self.run_cmd(cli.cmd_test_notify, self.cfg(NTFY_TOPIC=""), event="info")
        self.assertEqual(code, 1)
        self.assertIn("No notification channel", out)

    def test_failure_is_reported_and_exit_code_is_nonzero(self):
        self.mock.sink_status["ntfy"] = 500
        code, out = self.run_cmd(cli.cmd_test_notify, self.cfg(), event="info")
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)


class Misc(CliCase):
    def test_healthcheck_exit_codes(self):
        cfg = self.cfg()
        code, out = self.run_cmd(cli.cmd_healthcheck, cfg)
        self.assertEqual(code, 1)
        svc = self.service(cfg)
        svc.cycle()
        code, out = self.run_cmd(cli.cmd_healthcheck, cfg)
        self.assertEqual(code, 0, out)

    def test_parser_knows_every_command(self):
        parser = cli.build_parser()
        for name in cli.COMMANDS:
            self.assertIsNotNone(parser.parse_args([name] + (["x"] if name in ("call",) else (["faults"] if name == "export" else []))))

    def test_csv_safe(self):
        self.assertEqual(cli._csv_safe("=1+1"), "'=1+1")
        self.assertEqual(cli._csv_safe("@x"), "'@x")
        self.assertEqual(cli._csv_safe("-cmd"), "'-cmd")
        self.assertEqual(cli._csv_safe("-5"), "-5")
        self.assertEqual(cli._csv_safe(7), 7)
        self.assertEqual(cli._csv_safe("plain"), "plain")


if __name__ == "__main__":
    unittest.main()
