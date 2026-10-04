"""`bwwatch verify` - the installer's end-to-end check - and the small helpers that came with the installer."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bwwatch import cli
from bwwatch.service import Service
from bwwatch.verify import FAIL, OK, WARN, Check, format_checks, overall_ok, run_checks
from bwwatch.wave import check_reachable

from .helpers import WaveTestCase


def by_name(checks):
    out = {}
    for c in checks:
        out.setdefault(c.name, []).append(c)
    return out


class Verify(WaveTestCase):
    def poll_once(self, cfg=None):
        svc = self.service(cfg or self.cfg())
        svc.startup(None)
        outcome = svc.cycle()
        svc.maintenance()
        svc.write_status()
        return svc, outcome

    def test_a_healthy_installation_passes_everything(self):
        cfg = self.cfg()
        self.poll_once(cfg)
        checks = run_checks(cfg)
        self.assertTrue(overall_ok(checks), format_checks(checks))
        self.assertEqual({c.status for c in checks}, {OK}, format_checks(checks))
        names = by_name(checks)
        self.assertIn("Basement", names["Water heater"][0].detail)
        self.assertIn("Heat Pump", names["Water heater"][0].detail)
        self.assertIn("ntfy", names["Alert channels"][0].detail)
        self.assertIn("journal mode wal", names["Database"][0].detail)
        self.assertIn("1 verified backup", names["Backups"][0].detail)
        self.assertNotIn("(ok (", format_checks(checks), "no doubled parentheses")

    def test_nothing_started_fails_with_the_next_step(self):
        checks = run_checks(self.cfg())
        self.assertFalse(overall_ok(checks))
        names = by_name(checks)
        self.assertEqual(names["Service"][0].status, FAIL)
        self.assertIn("./bwctl start", names["Service"][0].fix)
        self.assertEqual(names["First poll"][0].status, FAIL)
        self.assertEqual(names["Database"][0].status, FAIL)

    def test_a_rejected_sign_in_points_at_login(self):
        cfg = self.cfg()
        svc, _ = self.poll_once(cfg)
        self.mock.revoke_all_tokens()
        svc.cycle()
        svc.write_status()
        checks = run_checks(cfg)
        wave = by_name(checks)["Wave cloud"][0]
        self.assertEqual(wave.status, FAIL)
        self.assertIn("./bwctl login", wave.fix)
        self.assertFalse(overall_ok(checks))

    def test_a_refusal_by_the_server_explains_vps_addresses(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        self.mock.api_queue.append((403, {"message": "Forbidden"}, {}))
        svc = self.service(cfg)
        svc.startup(None)
        svc.cycle()
        svc.write_status()
        wave = by_name(run_checks(cfg))["Wave cloud"][0]
        self.assertEqual(wave.status, FAIL)
        self.assertIn("VPS", wave.fix)

    def test_missing_channels_are_a_failure_missing_fault_request_only_a_warning(self):
        cfg = self.cfg(NTFY_TOPIC="", BW_FAULT_REQUEST="")
        self.poll_once(cfg)
        checks = run_checks(cfg)
        names = by_name(checks)
        self.assertEqual(names["Alert channels"][0].status, FAIL)
        self.assertEqual(names["Fault history"][0].status, WARN)
        self.assertIn("Finding the fault request", names["Fault history"][0].fix)
        self.assertFalse(overall_ok(checks))

    def test_undeliverable_alerts_are_a_warning_with_the_reason(self):
        self.mock.sink_status["ntfy"] = 500
        cfg = self.cfg()
        self.poll_once(cfg)
        alerts = by_name(run_checks(cfg))["Alerts delivered"][0]
        self.assertEqual(alerts.status, WARN)
        self.assertIn("waiting", alerts.detail)
        self.assertIn("500", alerts.detail)
        self.assertIn("./bwctl test-notify", alerts.fix)

    def test_a_damaged_database_is_reported(self):
        cfg = self.cfg()
        self.poll_once(cfg)
        with mock.patch("bwwatch.verify.integrity_check", return_value=["*** in database main ***", "Page 2: bad"]):
            database = by_name(run_checks(cfg))["Database"][0]
        self.assertEqual(database.status, FAIL)
        self.assertIn("integrity check reported problems", database.detail)

    def test_the_dead_mans_switch_is_listed_only_when_configured(self):
        cfg = self.cfg()
        self.poll_once(cfg)
        self.assertNotIn("Dead-man's switch", by_name(run_checks(cfg)))
        cfg = self.cfg(HEARTBEAT_URL=self.mock.url + "/heartbeat/abc")
        self.assertEqual(by_name(run_checks(cfg))["Dead-man's switch"][0].status, OK)

    def test_it_writes_nothing_and_changes_nothing(self):
        cfg = self.cfg()
        self.poll_once(cfg)
        before = sorted((p.name, p.stat().st_size) for p in cfg.data_dir.iterdir() if p.suffix not in (".db-wal", ".db-shm", ".json"))
        polls = self.mock.requests[:]
        run_checks(cfg)
        run_checks(cfg)
        after = sorted((p.name, p.stat().st_size) for p in cfg.data_dir.iterdir() if p.suffix not in (".db-wal", ".db-shm", ".json"))
        self.assertEqual(before, after)
        self.assertEqual(self.mock.requests, polls, "verify never contacts Bradford White or any alert service")


class SinceRestart(WaveTestCase):
    """Right after the installer starts the service, leftovers from an earlier run must not pass for it."""

    def test_an_old_poll_and_an_old_service_do_not_count(self):
        cfg = self.cfg()
        svc, _ = Verify.poll_once(self, cfg)
        svc.shutdown()
        checks = run_checks(cfg, since=time.time() + 1)
        names = by_name(checks)
        self.assertEqual(names["Service"][0].status, FAIL)
        self.assertIn("has not started", names["Service"][0].detail)
        self.assertEqual(names["First poll"][0].status, FAIL)
        self.assertNotIn("Wave cloud", names, "an earlier poll says nothing about the new service")
        self.assertFalse(overall_ok(checks))

    def test_a_new_service_with_a_new_poll_passes(self):
        cfg = self.cfg()
        since = time.time() - 1
        Verify.poll_once(self, cfg)
        checks = run_checks(cfg, since=since)
        self.assertTrue(overall_ok(checks), format_checks(checks))
        self.assertIn("Wave cloud", by_name(checks))

    def test_a_poll_held_back_by_the_safeguard_is_a_warning_not_a_failure(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        since = time.time() - 1
        svc = self.service(cfg)
        svc.startup(None)
        svc.next_poll_epoch = time.time() + 200
        svc.write_status()
        checks = run_checks(cfg, since=since)
        names = by_name(checks)
        self.assertEqual(names["First poll"][0].status, WARN)
        self.assertIn("safeguard", names["First poll"][0].fix)
        self.assertEqual(names["Water heater"][0].status, WARN)
        self.assertTrue(overall_ok(checks), format_checks(checks))

    def test_a_stalled_start_without_a_hold_is_a_failure(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        since = time.time() - 1
        svc = self.service(cfg)
        svc.startup(None)
        svc.write_status()  # started, but no poll and nothing held back
        checks = run_checks(cfg, since=since)
        self.assertEqual(by_name(checks)["First poll"][0].status, FAIL)

    def test_the_startup_alert_is_reported_so_the_installer_knows_whether_to_ask(self):
        cfg = self.cfg()
        since = time.time() - 1
        Verify.poll_once(self, cfg)
        alert = by_name(run_checks(cfg, since=since))["Startup alert"][0]
        self.assertEqual(alert.status, OK)
        self.assertIn("was delivered at", alert.detail)
        self.assertNotIn("Startup alert", by_name(run_checks(cfg)), "only asked about for a fresh start")

    def test_an_undelivered_startup_alert_is_a_warning_with_the_reason(self):
        self.mock.sink_status["ntfy"] = 500
        cfg = self.cfg()
        since = time.time() - 1
        Verify.poll_once(self, cfg)
        alert = by_name(run_checks(cfg, since=since))["Startup alert"][0]
        self.assertEqual(alert.status, WARN)
        self.assertIn("waiting to be delivered", alert.detail)
        self.assertIn("500", alert.detail)
        self.assertIn("./bwctl test-notify", alert.fix)

    def test_channels_that_only_want_faults_get_no_startup_alert_and_that_is_fine(self):
        cfg = self.cfg(NTFY_EVENTS="fault,health")
        since = time.time() - 1
        Verify.poll_once(self, cfg)
        alert = by_name(run_checks(cfg, since=since))["Startup alert"][0]
        self.assertEqual(alert.status, OK)
        self.assertIn("faults and problems only", alert.detail)

    def test_a_quick_restart_sends_no_startup_alert_and_that_is_reported_as_such(self):
        cfg = self.cfg()
        first, _ = Verify.poll_once(self, cfg)
        first.shutdown()
        time.sleep(1.1)  # stamps have one-second precision: put the restart clearly after the first start's alert
        since = time.time()
        svc = Service(cfg)
        svc.tokens.retry_after = 0
        svc.open()
        self.addCleanup(svc.shutdown)
        svc.startup(None)  # the previous start was moments ago: the once-an-hour notice is not repeated
        svc.cycle()
        svc.write_status()
        alert = by_name(run_checks(cfg, since=since))["Startup alert"][0]
        self.assertEqual(alert.status, OK)
        self.assertIn("none was due", alert.detail)

    def test_the_service_publishes_when_it_will_poll_next(self):
        cfg = self.cfg()
        svc, _ = Verify.poll_once(self, cfg)
        svc.next_poll_epoch = 1234.0
        svc.write_status()
        self.assertEqual(json.loads((cfg.data_dir / "status.json").read_text())["next_poll_epoch"], 1234.0)

    def test_progress_is_reported_while_waiting(self):
        cfg = self.cfg()
        said = []
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        run_checks(cfg, wait=35, sleep=sleep, clock=lambda: now[0], progress=said.append, since=1.0)
        self.assertGreaterEqual(len(said), 2)
        self.assertTrue(all("waiting" in line for line in said), said)
        self.assertEqual(run_checks(cfg, wait=0, progress=said.append)[0].name, "Service")


class Waiting(unittest.TestCase):
    """The `wait` option, with a fake clock so nothing really sleeps."""

    class Clock:
        def __init__(self):
            self.now = 0.0
            self.sleeps = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds
            if self.on_sleep:
                self.on_sleep(self)

        on_sleep = None

        def __call__(self):
            return self.now

    def run_with(self, snapshots, wait, since=None, now=0.0):
        clock = self.Clock()
        feed = iter(snapshots)
        last = {}

        def next_snapshot(_cfg, _since=None):
            try:
                last.update(next(feed))
            except StopIteration:
                pass
            return dict(last)

        cfg = mock.Mock(display_tz="UTC", channel_names=("ntfy",), fault_request=None, heartbeat_url="")
        with mock.patch("bwwatch.verify._snapshot", side_effect=next_snapshot), mock.patch("bwwatch.verify._evaluate", side_effect=lambda c, s, since=None, now=0: s):
            result = __import__("bwwatch.verify", fromlist=["run_checks"]).run_checks(
                cfg, wait=wait, sleep=clock.sleep, clock=clock, since=since, now=lambda: now)
        return result, clock

    def test_without_a_wait_it_looks_once(self):
        result, clock = self.run_with([{"polls": 0}], wait=0)
        self.assertEqual(clock.sleeps, [])

    def test_it_stops_as_soon_as_the_first_poll_and_its_alerts_are_done(self):
        snaps = [{"polls": 0}, {"polls": 0}, {"polls": 1, "outbox": {"pending": 2}}, {"polls": 1, "outbox": {"pending": 0, "sent": 2}}]
        result, clock = self.run_with(snaps, wait=150)
        self.assertEqual(result["outbox"], {"pending": 0, "sent": 2})
        self.assertLessEqual(clock.now, 8)

    def test_it_does_not_wait_forever_for_alerts_that_cannot_be_delivered(self):
        snaps = [{"polls": 1, "outbox": {"pending": 2}}]
        result, clock = self.run_with(snaps, wait=150)
        self.assertEqual(result["outbox"], {"pending": 2})
        self.assertGreaterEqual(clock.now, 15)
        self.assertLess(clock.now, 30, "gives the alerts 15 seconds, not the whole wait")

    def test_it_does_not_wait_for_a_poll_the_safeguard_will_not_allow_in_time(self):
        snaps = [{"polls": 3, "fresh_polls": 0, "fresh_service": True, "status": {"next_poll_epoch": 1000.0 + 250}}]
        result, clock = self.run_with(snaps, wait=150, since=900.0, now=1000.0)
        self.assertEqual(clock.sleeps, [], "no point waiting 150 s for a poll that is 250 s away")

    def test_it_does_wait_when_the_held_back_poll_is_due_within_the_wait(self):
        snaps = [{"polls": 3, "fresh_polls": 0, "fresh_service": True, "status": {"next_poll_epoch": 1000.0 + 20}},
                 {"fresh_polls": 0}, {"fresh_polls": 0}, {"fresh_polls": 0}, {"fresh_polls": 0}, {"fresh_polls": 0},
                 {"fresh_polls": 0}, {"fresh_polls": 0}, {"fresh_polls": 0}, {"fresh_polls": 0}, {"fresh_polls": 0},
                 {"fresh_polls": 1, "outbox": {"pending": 0}}]
        result, clock = self.run_with(snaps, wait=150, since=900.0, now=1000.0)
        self.assertGreater(len(clock.sleeps), 5)
        self.assertEqual(result["fresh_polls"], 1)

    def test_it_gives_up_at_the_deadline_when_no_poll_ever_happens(self):
        result, clock = self.run_with([{"polls": 0}], wait=20)
        self.assertGreaterEqual(clock.now, 20)
        self.assertLess(clock.now, 24)


class Formatting(unittest.TestCase):
    def test_layout_and_fixes(self):
        text = format_checks([
            Check(OK, "Service", "running"),
            Check(FAIL, "Wave cloud", "last poll FAILED", "Sign in again:  ./bwctl login"),
            Check(WARN, "Backups", "none yet"),
        ])
        lines = text.splitlines()
        self.assertEqual(len(lines), 4, "a fix line only for something that needs fixing")
        self.assertTrue(lines[0].lstrip().startswith("[ OK ] Service"))
        self.assertIn("[FAIL] Wave cloud", lines[1])
        self.assertIn("-> Sign in again:  ./bwctl login", lines[2])
        self.assertIn("[WARN] Backups", lines[3])

    def test_overall_ok_only_fails_on_a_failure(self):
        self.assertTrue(overall_ok([Check(OK, "a", ""), Check(WARN, "b", "")]))
        self.assertFalse(overall_ok([Check(OK, "a", ""), Check(FAIL, "b", "")]))


class VerifyCommand(WaveTestCase):
    def test_exit_status_follows_the_result(self):
        cfg = self.cfg()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_verify(cfg, argparse.Namespace(wait=0.0, since=None)), 1)
        self.assertIn("[FAIL]", out.getvalue())
        svc = self.service(cfg)
        svc.startup(None)
        svc.cycle()
        svc.maintenance()
        svc.write_status()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_verify(cfg, argparse.Namespace(wait=0.0, since=None)), 0)
        self.assertNotIn("[FAIL]", out.getvalue())


class Reachability(WaveTestCase):
    def test_any_http_answer_means_reachable(self):
        for path in ("/auth/authorize", "/nothing-here"):
            ok, detail = check_reachable(self.mock.url + path)
            self.assertTrue(ok, detail)
            self.assertIn("HTTP 404", detail)

    def test_nothing_listening_is_not_reachable_and_says_why(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]  # a port nothing is listening on
        ok, detail = check_reachable("http://127.0.0.1:%d/x" % port, timeout=2)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_no_token_and_no_secrets_are_sent(self):
        check_reachable(self.mock.url + "/auth/authorize?client_id=x&state=secret", user_agent="bwwatch-test")
        request = self.mock.requests[-1]
        self.assertEqual(request["method"], "GET")
        self.assertNotIn("Authorization", request["headers"])
        self.assertEqual(request["headers"]["User-Agent"], "bwwatch-test")


class FileLogging(unittest.TestCase):
    def test_a_small_rotating_log_inside_the_given_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "bwwatch.log"
            root = logging.getLogger()
            before = list(root.handlers)
            try:
                cli.add_file_logging(path)
                logging.getLogger("bwwatch.test").warning("hello from the test")
                for handler in root.handlers:
                    handler.flush()
                self.assertIn("hello from the test", path.read_text(encoding="utf-8"))
                added = [h for h in root.handlers if h not in before]
                self.assertEqual(len(added), 1)
                self.assertEqual((added[0].maxBytes, added[0].backupCount), (1_000_000, 4))
            finally:
                for handler in list(root.handlers):
                    if handler not in before:
                        root.removeHandler(handler)
                        handler.close()

    def test_an_unwritable_location_does_not_stop_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            root = logging.getLogger()
            before = list(root.handlers)
            cli.add_file_logging(blocker / "sub" / "x.log")  # cannot be created: must not raise
            self.assertEqual(list(root.handlers), before)


if __name__ == "__main__":
    unittest.main()
