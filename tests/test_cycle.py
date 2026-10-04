"""What a poll does: baseline, new faults, dedupe, settings changes, flags, failures, retries, atomicity."""
from __future__ import annotations

import sqlite3
import time
import unittest
from unittest import mock

from bwwatch import cycle as cycle_module
from bwwatch.config import Config
from bwwatch.cycle import FetchResult, apply_cycle, fetch
from bwwatch.service import Service
from bwwatch.wave import TokenStore

from .helpers import MAC, WaveTestCase

T0 = 1760000000


def event(i, code=10, msg="Heating element failure", **extra):
    return dict({"id": i, "faultCode": code, "message": msg, "timestamp": T0 + i}, **extra)


class CycleCase(WaveTestCase):
    def setUp(self):
        super().setUp()
        self.svc = self.service()

    def kinds(self):
        return [r["kind"] for r in self.outbox(self.svc)]

    def delivered(self):
        return [m["json"] for m in self.mock.sinks["ntfy"]]

    def titles(self):
        return [m["title"] for m in self.delivered()]

    def count(self, table, where=""):
        return self.svc.conn.execute("SELECT COUNT(*) FROM %s %s" % (table, where)).fetchone()[0]

    def poll(self):
        outcome = self.svc.cycle()
        return outcome


class Baseline(CycleCase):
    def test_first_poll_records_existing_faults_without_alarming(self):
        self.mock.notifications = {"notifications": [event(1), event(2, code=11)]}
        out = self.poll()
        self.assertTrue(out.ok, out.error)
        self.assertEqual(out.new_faults, [])
        self.assertEqual(self.count("faults", "WHERE baseline = 1"), 2)
        self.assertEqual(self.kinds(), ["info"], "just one 'now watching' summary, no fault alerts")
        title, body = self.delivered()[0]["title"], self.delivered()[0]["message"]
        self.assertEqual(title, "Watching Basement")
        self.assertIn("2 existing entries recorded without alerting", body)
        self.assertIn("mode Heat Pump, setpoint 120°F", body)

    def test_with_no_fault_request_the_summary_says_so(self):
        svc = self.service(self.cfg(BW_FAULT_REQUEST=""))
        svc.cycle()
        self.assertIn("BW_FAULT_REQUEST is not set", self.delivered()[-1]["message"])


class NewFaults(CycleCase):
    def test_a_new_fault_alerts_exactly_once(self):
        self.poll()
        self.mock.notifications = {"notifications": [event(5)]}
        out = self.poll()
        self.assertEqual([f.code for f in out.new_faults], ["10"])
        alert = self.delivered()[-1]
        self.assertEqual(alert["title"], "Water heater fault 10 — Basement")
        self.assertEqual(alert["priority"], 4)
        for text in ("Fault code: 10", "Heating element failure", "Appliance: Basement (%s)" % MAC, "Reported:", "Detected:"):
            self.assertIn(text, alert["message"])
        sent = len(self.delivered())
        for _ in range(3):
            self.poll()
        self.assertEqual(len(self.delivered()), sent, "the same fault never alerts again")
        self.assertEqual(self.svc.conn.execute("SELECT seen_count FROM faults WHERE baseline = 0").fetchone()[0], 4)

    def test_restart_does_not_repeat_alerts(self):
        self.poll()
        self.mock.notifications = {"notifications": [event(5)]}
        self.poll()
        sent = len(self.delivered())
        self.svc.shutdown()
        again = self.service(self.svc.cfg)
        again.cycle()
        again.cycle()
        self.assertEqual(len(self.delivered()), sent)

    def test_read_flags_and_request_ids_do_not_cause_duplicates(self):
        self.poll()
        raw = {"faultCode": 10, "message": "Heating element failure", "timestamp": T0, "read": False, "age": "1 minute ago"}
        self.mock.notifications = {"notifications": [dict(raw)]}
        self.poll()
        sent = len(self.delivered())
        self.mock.notifications = {"notifications": [dict(raw, read=True, age="3 hours ago")], "requestId": "abc"}
        self.poll()
        self.assertEqual(len(self.delivered()), sent)

    def test_a_recurrence_of_the_same_code_is_a_new_alert(self):
        self.poll()
        self.mock.notifications = {"notifications": [event(1, code=10)]}
        self.poll()
        self.mock.notifications = {"notifications": [event(1, code=10), event(2, code=10)]}
        self.poll()
        self.assertEqual(sum(1 for t in self.titles() if t.startswith("Water heater fault 10")), 2)

    def test_a_flood_is_capped_and_summarised(self):
        self.poll()
        self.mock.notifications = {"notifications": [event(i) for i in range(1, 9)]}
        out = self.poll()
        self.assertEqual(len(out.new_faults), 8)
        new = self.titles()[1:]  # after the 'Watching' message
        self.assertEqual(len(new), 6)
        self.assertEqual(new[-1], "3 more new fault entries")
        self.assertEqual(self.count("faults", "WHERE baseline = 0"), 8, "all eight are still logged")

    def test_unrecognised_response_still_alerts_when_it_changes(self):
        self.mock.notifications = {"message": "No notifications"}
        self.poll()
        self.poll()
        self.assertEqual(self.titles(), ["Watching Basement"])
        self.mock.notifications = {"message": "Fault 10 detected"}
        self.poll()
        self.assertEqual(self.titles()[-1], "Wave fault data changed — Basement")
        sent = len(self.delivered())
        self.poll()
        self.assertEqual(len(self.delivered()), sent)

    def test_raw_responses_are_kept_when_they_change(self):
        self.poll()
        self.mock.notifications = {"notifications": [event(1)]}
        self.poll()
        self.poll()
        self.assertEqual(self.count("snapshots", "WHERE kind = 'faults'"), 2, "stored on change only")
        self.assertEqual(self.count("snapshots", "WHERE kind = 'status'"), 1, "requestId changes do not count as a change")

    def test_home_assistant_receives_the_fault_with_structured_details(self):
        svc = self.service(self.cfg(HA_WEBHOOK_URL=self.mock.url + "/ha/api/webhook/abc", HA_WEBHOOK_EVENTS="fault"))
        svc.cycle()
        self.assertEqual(self.mock.sinks["ha"], [], "the baseline summary is not a fault event")
        self.mock.notifications = {"notifications": [event(5)]}
        svc.cycle()
        payload = self.mock.sinks["ha"][0]["json"]
        self.assertEqual((payload["event"], payload["fault"]["code"], payload["appliance"]["name"]), ("fault", "10", "Basement"))
        self.assertEqual(payload["fault"]["description"], "Heating element failure")
        self.assertEqual(payload["appliance"]["mac"], MAC)
        self.assertEqual(len(self.mock.sinks["ha"]), 1)


class Settings(CycleCase):
    def test_mode_and_setpoint_changes_are_reported_and_logged(self):
        self.poll()
        self.mock.status[MAC].update(heatModeValue=4, mode="Electric", setpointFahrenheit=130)
        self.poll()
        alert = self.delivered()[-1]
        self.assertEqual(alert["title"], "Water heater setting changed — Basement")
        self.assertIn("mode Heat Pump → Electric", alert["message"])
        self.assertIn("setpoint 120 → 130°F", alert["message"])
        self.assertEqual(self.count("readings"), 2)
        sent = len(self.delivered())
        self.poll()
        self.assertEqual(len(self.delivered()), sent, "no repeat while it stays the same")

    def test_temperature_fields_are_recorded_but_do_not_alert(self):
        self.mock.status[MAC]["tankTempF"] = 111.5
        self.poll()
        sent = len(self.delivered())
        self.mock.status[MAC]["tankTempF"] = 119
        self.poll()
        self.assertEqual(len(self.delivered()), sent)
        temps = self.svc.conn.execute("SELECT temps FROM readings ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn("119", temps)

    def test_setting_alerts_can_be_turned_off_but_are_still_logged(self):
        svc = self.service(self.cfg(NOTIFY_STATUS_CHANGES="false"))
        svc.cycle()
        self.mock.status[MAC].update(heatModeValue=5, mode="Vacation")
        svc.cycle()
        self.assertNotIn("setting", [r["kind"] for r in self.outbox(svc)])
        self.assertEqual(svc.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 2)


class StatusFlags(CycleCase):
    def test_flag_lifecycle(self):
        self.poll()
        self.mock.status[MAC]["faultCode"] = 10
        out = self.poll()
        self.assertEqual([f.kind for f in out.new_faults], ["state"])
        self.assertEqual(self.delivered()[-1]["title"], "Water heater fault 10 — Basement")
        self.assertIn("heuristic", self.delivered()[-1]["message"])
        sent = len(self.delivered())
        self.poll()
        self.assertEqual(len(self.delivered()), sent, "an active flag alerts once, not every poll")
        self.mock.status[MAC]["faultCode"] = 0
        out = self.poll()
        self.assertEqual(out.cleared, 1)
        self.assertEqual(self.delivered()[-1]["title"], "Fault flag cleared — Basement")
        self.mock.status[MAC]["faultCode"] = 10
        self.poll()
        self.assertEqual(self.delivered()[-1]["title"], "Water heater fault 10 — Basement", "a recurrence alerts again")
        self.assertEqual(self.count("faults", "WHERE kind = 'state'"), 2)
        self.assertEqual(self.count("faults", "WHERE kind = 'state' AND cleared_at IS NOT NULL"), 1)

    def test_a_flag_already_active_at_start_is_baseline_not_an_alert(self):
        self.mock.status[MAC]["faultCode"] = 7
        out = self.poll()
        self.assertEqual(out.new_faults, [])
        self.assertIn("Fault-like status fields already active at start: status.faultCode = 7", self.delivered()[0]["message"])

    def test_missing_status_data_does_not_look_like_everything_cleared(self):
        self.poll()
        self.mock.status[MAC]["faultCode"] = 10
        self.poll()
        saved = self.mock.status.pop(MAC)  # the status request now fails
        out = self.poll()
        self.assertFalse(out.ok)
        self.assertEqual(out.cleared, 0)
        self.assertEqual(self.count("faults", "WHERE kind = 'state' AND cleared_at IS NULL"), 1)
        self.assertNotIn("cleared", self.kinds())
        self.mock.status[MAC] = saved
        self.poll()
        self.assertNotIn("cleared", self.kinds())

    def test_config_flags_that_mention_alerts_are_ignored(self):
        self.mock.status[MAC].update(alertsEnabled=True, errorReportingSupported=True, faults=[], alarm="none")
        self.poll()
        self.mock.status[MAC]["alertsEnabled"] = False
        out = self.poll()
        self.assertEqual(out.new_faults, [])


class Failures(CycleCase):
    def test_health_alert_after_repeated_failures_then_recovery(self):
        self.mock.token_override = (500, {"error": "server_error"}, {})
        for _ in range(2):
            self.assertFalse(self.poll().ok)
        self.assertNotIn("health", self.kinds(), "two failures are not yet worth waking anyone")
        self.poll()
        self.assertEqual(self.kinds().count("health"), 1)
        health = self.delivered()[-1]
        self.assertEqual(health["title"], "Wave monitoring is failing")
        self.assertEqual(health["priority"], 4)
        for _ in range(3):
            self.poll()
        self.assertEqual(self.kinds().count("health"), 1, "no spam while it stays down")
        self.mock.token_override = None
        self.assertTrue(self.poll().ok)
        self.assertEqual(self.kinds().count("recovered"), 1)
        self.assertEqual(self.delivered()[-1]["title"], "Wave monitoring is working again")
        self.poll()
        self.assertEqual(self.kinds().count("recovered"), 1)
        self.assertEqual(self.count("polls", "WHERE ok = 0"), 6, "2 + 1 + 3 failed polls, every one of them logged")

    def test_sign_in_problems_alert_immediately_and_stop_hammering(self):
        self.mock.revoke_all_tokens()
        out = self.poll()
        self.assertFalse(out.ok)
        alert = self.delivered()[-1]
        self.assertIn("faults are NOT being monitored", alert["title"])
        self.assertEqual(alert["priority"], 5)
        self.assertIn("docker compose run --rm bwwatch login", alert["message"])
        token_calls = len(self.mock.hits("/auth/token"))
        for _ in range(3):
            self.poll()
        self.assertEqual(len(self.mock.hits("/auth/token")), token_calls, "the sign-in server is left alone until a new login")
        self.assertEqual(self.kinds().count("health"), 1)
        time.sleep(0.02)
        TokenStore(self.svc.cfg.data_dir / "token.json").save(self.mock.seed_refresh_token("fresh-login"))
        self.assertTrue(self.poll().ok, "a new login is picked up on the next poll")
        self.assertEqual(self.kinds().count("recovered"), 1)

    def test_reminder_every_twelve_hours_while_still_failing(self):
        conn, cfg = self.svc.conn, self.svc.cfg

        def fail(hours):
            stamp = "2026-10-04T%02d:00:00Z" % hours if hours < 24 else "2026-10-05T%02d:00:00Z" % (hours - 24)
            apply_cycle(conn, cfg, FetchResult(started_at=stamp, errors=["boom"]), now=stamp)

        for hour in (0, 1, 2):
            fail(hour)
        self.assertEqual(self.kinds().count("health"), 1)
        for hour in (3, 8, 13):
            fail(hour)
        self.assertEqual(self.kinds().count("health"), 1, "less than 12 h since the alert")
        fail(14)
        self.assertEqual(self.kinds().count("health"), 2, "a reminder after 12 h")

    def test_partial_failure_still_records_what_worked(self):
        self.mock.status.pop(MAC)  # status fails, fault history works
        self.mock.notifications = {"notifications": [event(1)]}
        out = self.poll()
        self.assertFalse(out.ok)
        self.assertIn("status", out.error)
        self.assertEqual(self.count("faults"), 1)


class Delivery(CycleCase):
    def test_undeliverable_alerts_wait_and_are_sent_once_later(self):
        self.mock.sink_status["ntfy"] = 500
        self.poll()
        self.assertEqual(self.svc.pending, 1)
        row = self.outbox(self.svc)[0]
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.assertIn("HTTP 500", row["last_error"])
        self.mock.sink_status.clear()
        self.svc.deliver()
        self.assertEqual(self.svc.pending, 0)
        self.assertEqual(len(self.delivered()), 1)
        self.svc.deliver()
        self.assertEqual(len(self.delivered()), 1)

    def test_without_any_channel_alerts_are_dropped_but_faults_are_logged(self):
        svc = self.service(self.cfg(NTFY_TOPIC=""))
        self.mock.notifications = {"notifications": [event(1)]}
        svc.cycle()
        self.mock.notifications = {"notifications": [event(1), event(2)]}
        svc.cycle()
        self.assertEqual(svc.conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 2)
        statuses = {r["status"] for r in self.outbox(svc)}
        self.assertEqual(statuses, {"dropped"})

    def test_old_alerts_expire(self):
        self.mock.sink_status["ntfy"] = 500
        self.poll()
        self.svc.conn.execute("UPDATE outbox SET created_at = '2020-01-01T00:00:00Z'")
        self.mock.sink_status.clear()
        self.svc.deliver()
        self.assertEqual(self.outbox(self.svc)[0]["status"], "dropped")
        self.assertEqual(self.delivered(), [])


class Atomicity(CycleCase):
    def test_a_database_error_midway_leaves_nothing_behind(self):
        fetched = fetch(self.svc.cfg, self.svc.api)

        def break_midway(conn, cfg, a, now, outcome, notes):
            conn.execute("INSERT INTO faults(mac, kind, source, fingerprint, first_seen_at, last_seen_at, raw) "
                         "VALUES('m', 'state', 's', 'f', 't', 't', '{}')")
            raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(cycle_module, "_record_state", break_midway):
            with self.assertRaises(sqlite3.OperationalError):
                apply_cycle(self.svc.conn, self.svc.cfg, fetched)
        for table in ("polls", "faults", "outbox", "snapshots", "readings", "appliances"):
            self.assertEqual(self.count(table), 0, table)
        self.assertFalse(self.svc.conn.in_transaction)
        self.assertTrue(self.poll().ok, "and the next poll simply works")

    def test_a_parsing_bug_cannot_lose_the_raw_data_or_the_rest_of_the_poll(self):
        self.mock.notifications = {"notifications": [event(1)]}
        with mock.patch.object(cycle_module, "extract_events", side_effect=ValueError("surprise format")):
            out = self.poll()
        self.assertFalse(out.ok)
        self.assertIn("internal error while reading the fault history", out.error)
        self.assertEqual(self.count("snapshots", "WHERE kind = 'faults'"), 1, "the raw response was kept")
        self.assertEqual(self.count("readings"), 1, "the rest of the poll was still recorded")
        self.assertEqual(self.count("polls", "WHERE ok = 0"), 1)
        self.assertTrue(self.poll().ok, "and with the bug gone, the same data is processed normally")

    def test_a_crash_in_the_recording_step_is_reported_not_fatal(self):
        import bwwatch.service as service_module

        real = service_module.apply_cycle
        calls = []

        def flaky(conn, cfg, fetched, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("bug while recording")
            return real(conn, cfg, fetched, **kw)

        with mock.patch.object(service_module, "apply_cycle", flaky):
            out = self.poll()
        self.assertFalse(out.ok)
        self.assertIn("internal error while recording", out.error)
        self.assertEqual(self.count("polls", "WHERE ok = 0"), 1, "the failure was recorded so health alerting still works")


if __name__ == "__main__":
    unittest.main()
