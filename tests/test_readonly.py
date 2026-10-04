"""The water heater must never be changed by this program. These tests are the proof."""
from __future__ import annotations

import ast
import inspect
import random
import re
import unittest
from pathlib import Path

from bwwatch import wave
from bwwatch.config import Config, ConfigError, RequestSpec
from bwwatch.errors import ReadOnlyViolation
from bwwatch.readonly import MUTATING_WORDS, READ_NOUNS, READ_VERBS, check_read_only
from bwwatch.wave import WaveApi

from .helpers import MAC, WaveTestCase

PACKAGE = Path(wave.__file__).parent


class GuardTable(unittest.TestCase):
    ALLOWED = [
        ("GET", "/wave/getApplianceList?username={account_id}"),
        ("GET", "/wave/getApplianceStatus?macAddress={mac}"),
        ("POST", "/wave/getEnergyUsage"),
        ("GET", "/wave/getNotifications?username={account_id}"),
        ("GET", "/wave/getPushNotificationHistory?macAddress={mac}"),
        ("GET", "/wave/applianceNotifications?macAddress={mac}"),
        ("GET", "/wave/faultHistory"),
        ("GET", "/wave/listAlerts"),
        ("GET", "https://gw.prdapi.bradfordwhiteapps.com/wave/getFaults?macAddress={mac}"),
        ("GET", "/wave/getSetpointHistory?macAddress={mac}"),  # 'setpoint' is a noun, not the word 'set'
        ("GET", "/wave/getClearedFaults"),  # 'cleared' is not the action 'clear'
    ]
    BLOCKED = [
        ("GET", "/wave/changeSetpoint?mac_address={mac}&temperature=120"),
        ("GET", "/wave/changeOpMode?mac_address={mac}&mode=3"),
        ("GET", "/wave/ChangeSetpoint"),
        ("GET", "/wave/change_setpoint"),
        ("GET", "/wave/CHANGEOPMODE"),
        ("POST", "/wave/changeSetpoint"),
        ("PUT", "/wave/getApplianceStatus"),
        ("DELETE", "/wave/getNotifications"),
        ("PATCH", "/wave/getNotifications"),
        ("HEAD", "/wave/getNotifications"),
        ("GET", "/wave/getAndClearNotifications"),
        ("GET", "/wave/notificationsClear"),
        ("GET", "/wave/ackNotification"),
        ("GET", "/wave/markNotificationsRead"),
        ("GET", "/wave/getOrCreateThing"),
        ("GET", "/wave/resetFaults"),
        ("GET", "/wave/setMode"),
        ("GET", "/wave/updateAppliance"),
        ("GET", "/wave/change/getStatus"),  # action word in an earlier path segment
        ("GET", "/wave/getApplianceStatus?macAddress={mac}&temperature=130"),
        ("GET", "/wave/getApplianceStatus?mode=3"),
        ("GET", "/wave/getApplianceStatus?setpoint=130"),
        ("POST", "/wave/applianceNotifications"),  # POST only for verb-style reads
        ("GET", "/wave/{name}"),  # placeholder in the path
        ("GET", "/wave/foo"),  # not recognisably a read
        ("GET", "/"),
        ("GET", "/wave/../changeSetpoint"),
        ("GET", "ftp://gw.example/wave/getX"),
    ]

    def test_allowed(self):
        for method, target in self.ALLOWED:
            with self.subTest(method=method, target=target):
                check_read_only(method, target)

    def test_blocked(self):
        for method, target in self.BLOCKED:
            with self.subTest(method=method, target=target):
                with self.assertRaises(ReadOnlyViolation):
                    check_read_only(method, target)

    def test_write_style_body_keys_are_blocked(self):
        for body in ({"temperature": 130}, {"mode": 3}, {"Setpoint": 1}, {"mac_address": "x", "heatMode": 2}):
            with self.subTest(body=body):
                with self.assertRaises(ReadOnlyViolation):
                    check_read_only("POST", "/wave/getEnergyUsage", body)
        check_read_only("POST", "/wave/getEnergyUsage", {"mac_address": "x", "view_type": "weekly"})

    def test_fuzz_any_action_word_anywhere_is_blocked(self):
        rng = random.Random(20261004)
        words = sorted(MUTATING_WORDS)
        for _ in range(3000):
            lead = rng.choice(sorted(READ_VERBS) + sorted(READ_NOUNS))
            action = rng.choice(words)
            pieces = [lead, action, rng.choice(["Appliance", "Notifications", "Faults", "Mode", "Thing"])]
            rng.shuffle(pieces)
            style = rng.choice(["camel", "snake", "kebab", "upper_snake"])
            if style == "camel":
                name = "".join(p.capitalize() for p in pieces)
                name = name[0].lower() + name[1:]
            elif style == "snake":
                name = "_".join(pieces)
            elif style == "kebab":
                name = "-".join(pieces)
            else:
                name = "_".join(pieces).upper()
            prefix = rng.choice(["/wave/", "/wave/v2/", "/"])
            with self.subTest(name=name):
                with self.assertRaises(ReadOnlyViolation):
                    check_read_only("GET", prefix + name)


class NoOverride(unittest.TestCase):
    def test_config_refuses_write_requests(self):
        for var in ("BW_FAULT_REQUEST", "BW_STATUS_REQUEST", "BW_LIST_REQUEST"):
            with self.subTest(var=var):
                with self.assertRaises(ConfigError):
                    Config.from_env({var: "GET /wave/changeSetpoint?mac_address={mac}&temperature=120"})

    def test_there_is_no_flag_that_allows_writes(self):
        env = {
            "BW_FAULT_REQUEST": "GET /wave/changeOpMode?mac_address={mac}&mode=3",
            "BW_ALLOW_WRITE_REQUESTS": "1",
            "ALLOW_WRITES": "true",
            "BW_READ_ONLY": "0",
        }
        with self.assertRaises(ConfigError):
            Config.from_env(env)

    def test_the_old_override_name_is_gone_from_the_code(self):
        for path in PACKAGE.glob("*.py"):
            self.assertNotIn("ALLOW_WRITE", path.read_text(encoding="utf-8"), path.name)


class SourceStructure(unittest.TestCase):
    """Properties of the code itself, so a future edit cannot quietly add a way to write."""

    def test_write_endpoint_names_appear_only_in_the_guard(self):
        pattern = re.compile(r"change[_ ]?(setpoint|opmode)", re.I)
        offenders = [p.name for p in PACKAGE.glob("*.py") if p.name != "readonly.py" and pattern.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_only_wave_module_opens_http_connections(self):
        for path in PACKAGE.glob("*.py"):
            if path.name == "wave.py":
                continue
            text = path.read_text(encoding="utf-8")
            for forbidden in ("urllib.request", "http.client", "import requests", "import httpx", "import socket"):
                self.assertNotIn(forbidden, text, "%s uses %s" % (path.name, forbidden))

    def test_http_request_has_exactly_two_callers_in_wave_module(self):
        tree = ast.parse((PACKAGE / "wave.py").read_text(encoding="utf-8"))
        callers = set()

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "http_request":
                    callers.add(self.stack[-1] if self.stack else "<module>")
                self.generic_visit(node)

        Visitor().visit(tree)
        # _post_token talks to the sign-in server (OAuth); _exchange is the only way to the Wave API.
        self.assertEqual(callers, {"_post_token", "_exchange"})

    def test_every_route_to_the_api_runs_the_guard_first(self):
        for func in (WaveApi.call, WaveApi.raw):
            source = inspect.getsource(func)
            guard = source.index("check_read_only(")
            for later in ("self.tokens.bearer(", "self._send(", "self._exchange("):
                if later in source:
                    self.assertLess(guard, source.index(later), "%s: guard must precede %s" % (func.__name__, later))

    def test_exchange_only_sends_get_or_post(self):
        source = inspect.getsource(WaveApi._exchange)
        self.assertIn('("GET", "POST")', source)

    def test_requestspec_parse_runs_the_guard(self):
        self.assertIn("check_read_only(", inspect.getsource(RequestSpec.parse))


class RuntimeNeverWrites(WaveTestCase):
    def test_forged_spec_is_refused_before_any_network_traffic(self):
        svc = self.service()
        forged = RequestSpec("GET", "/wave/changeSetpoint?mac_address=%s&temperature=130" % MAC)  # bypasses parse()
        with self.assertRaises(ReadOnlyViolation):
            svc.api.call(forged)
        with self.assertRaises(ReadOnlyViolation):
            svc.api.raw(forged)
        self.assertEqual(self.mock.requests, [], "nothing at all may be sent, not even a sign-in")
        self.assertEqual(self.mock.writes, [])

    def test_forged_put_is_refused(self):
        svc = self.service()
        with self.assertRaises(ReadOnlyViolation):
            svc.api.call(RequestSpec("PUT", "/wave/getApplianceStatus?macAddress=x"))
        self.assertEqual(self.mock.requests, [])

    def test_normal_operation_only_touches_read_endpoints(self):
        svc = self.service()
        self.mock.notifications = {"notifications": [{"id": 1, "faultCode": 10, "timestamp": 1760000000}]}
        for _ in range(3):
            svc.cycle()
        self.mock.status[MAC]["heatModeValue"] = 4
        self.mock.status[MAC]["mode"] = "Electric"
        svc.cycle()
        self.assertEqual(self.mock.writes, [])
        allowed = {"/auth/token", "/wave/getApplianceList", "/wave/getApplianceStatus", "/wave/getNotifications", "/ntfy"}
        self.assertLessEqual({r["path"] for r in self.mock.requests}, allowed)
        self.assertTrue(all(r["method"] in ("GET", "POST") for r in self.mock.requests))
        # POSTs may only be the sign-in refresh and our own alerts
        posts = {r["path"] for r in self.mock.requests if r["method"] == "POST"}
        self.assertLessEqual(posts, {"/auth/token", "/ntfy"})


if __name__ == "__main__":
    unittest.main()
