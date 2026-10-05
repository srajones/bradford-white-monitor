"""What a poll does: baseline, new faults, dedupe, settings changes, flags, failures, retries, atomicity."""
from __future__ import annotations

import re
import sqlite3
import time
import unittest
from unittest import mock

from bwwatch import cycle as cycle_module
from bwwatch.cycle import FetchResult, apply_cycle, fetch
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


UUID = "9f1c2a3e-1111-2222-3333-444455556666"


def wave_entry(minute, state="", code=10, text="Superheat Fault", **extra):
    """An entry the way the Wave app words it: title 'Fault 10', '(Cleared) ' in front of the text once it has cleared."""
    marker = "(Cleared) " if state == "cleared" else ""
    entry = {"title": "Fault %s" % code, "message": marker + text, "timestamp": T0 + minute * 60}
    if state == "active":
        entry["status"] = "Active"
    entry.update(extra)
    return entry


class ClearedFaults(CycleCase):
    """Faults come and go by themselves; the Notifications list remembers them as '(Cleared)'."""

    def faults(self, **where):
        rows = self.svc.conn.execute("SELECT * FROM faults WHERE kind = 'event' ORDER BY id").fetchall()
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def feed(self, *entries):
        self.mock.notifications = {"notifications": list(entries)}
        return self.poll()

    def test_the_entry_in_the_screenshot_is_logged_but_does_not_raise_an_alarm_when_it_is_already_history(self):
        out = self.feed(wave_entry(0, "cleared"))
        self.assertEqual(out.new_faults, [])
        (row,) = self.faults()
        self.assertEqual((row["code"], row["description"], row["state"], row["baseline"]), ("10", "Superheat Fault", "cleared", 1))
        self.assertEqual(self.kinds(), ["info"])
        self.assertIn("code 10", self.delivered()[0]["message"])
        self.assertIn("[cleared]", self.delivered()[0]["message"])

    def test_a_fault_that_came_and_went_between_two_checks_is_still_reported(self):
        self.feed()
        out = self.feed(wave_entry(0, "cleared"))
        self.assertEqual(len(out.new_faults), 1)
        alert = self.delivered()[-1]
        self.assertEqual(alert["title"], "Water heater fault 10 (cleared) — Basement")
        self.assertIn("Status: cleared", alert["message"])
        self.assertIn("already cleared", alert["message"])
        self.assertEqual(alert["priority"], 4, "still a fault worth knowing about")
        (row,) = self.faults()
        self.assertEqual((row["state"], row["baseline"]), ("cleared", 0))
        self.assertIsNone(row["cleared_seen_at"], "we never saw it active, so we did not see it clear")
        sent = len(self.delivered())
        self.feed(wave_entry(0, "cleared"))
        self.assertEqual(len(self.delivered()), sent, "and only once")

    def test_active_then_cleared_alerts_twice_with_the_duration(self):
        self.feed()
        self.feed(wave_entry(0, "active"))
        self.assertEqual(self.delivered()[-1]["title"], "Water heater fault 10 — Basement")
        self.assertIn("Status: active", self.delivered()[-1]["message"])
        out = self.feed(wave_entry(0, "cleared", clearedAt=T0 + 42 * 60))
        self.assertEqual(out.new_faults, [])
        self.assertEqual(out.cleared, 1)
        alert = self.delivered()[-1]
        self.assertEqual(alert["title"], "Water heater fault 10 cleared — Basement")
        self.assertIn("It lasted about 42 minutes.", alert["message"])
        for text in ("Began:", "Cleared:", "Noticed:", "Superheat Fault"):
            self.assertIn(text, alert["message"])
        self.assertEqual(self.kinds()[-1], "cleared")
        (row,) = self.faults()
        self.assertEqual(row["state"], "cleared")
        self.assertTrue(row["cleared_at"].startswith("20"), "the time the entry gave")
        self.assertTrue(row["cleared_seen_at"], "and the check on which we saw it clear")
        sent = len(self.delivered())
        self.feed(wave_entry(0, "cleared", clearedAt=T0 + 42 * 60))
        self.assertEqual(len(self.delivered()), sent)

    def test_the_log_says_how_each_cleared_fault_was_learned_about(self):
        import argparse
        import contextlib
        import io

        from bwwatch import cli

        self.feed()
        self.feed(wave_entry(0, "cleared"))                    # already gone when first seen
        self.feed(wave_entry(0, "cleared"), wave_entry(200, "active", code=12, text="Sensor"))
        self.feed(wave_entry(0, "cleared"), wave_entry(200, "cleared", code=12, text="Sensor"))  # seen active, then cleared
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_faults(self.svc.cfg, argparse.Namespace(limit=20, all=False, raw=False))
        lines = {re.split(r"\s{2,}", l.strip())[2]: l for l in out.getvalue().splitlines()[2:]}
        self.assertIn("already cleared when first seen", lines["10"])
        self.assertIn("cleared by 20", lines["12"])

    def test_without_a_time_it_says_it_cleared_between_two_checks(self):
        self.feed()
        self.feed(wave_entry(0, "active"))
        self.feed(wave_entry(0, "cleared"))
        message = self.delivered()[-1]["message"]
        self.assertIn("Cleared: some time between", message)
        self.assertNotIn("It lasted", message)

    def test_clear_alerts_can_be_turned_off_but_the_clear_is_still_logged(self):
        self.svc = self.service(self.cfg(NOTIFY_CLEARED="false"))
        self.feed()
        self.feed(wave_entry(0, "active"))
        sent = len(self.delivered())
        self.feed(wave_entry(0, "cleared"))
        self.assertEqual(len(self.delivered()), sent)
        self.assertEqual(self.faults()[0]["state"], "cleared")

    def test_a_fault_that_is_active_right_now_is_news_even_on_the_very_first_poll(self):
        out = self.feed(wave_entry(0, "cleared", code=11), wave_entry(5, "active"))
        self.assertEqual([f.code for f in out.new_faults], ["10"], "the old cleared one is history, the active one is not")
        titles = self.titles()
        self.assertIn("Water heater fault 10 — Basement", titles)
        self.assertEqual(self.faults(code="11")[0]["baseline"], 1)
        self.assertEqual(self.faults(code="10")[0]["baseline"], 0)

    def test_an_active_fault_found_at_the_start_is_reported_as_cleared_later(self):
        self.feed(wave_entry(0, "active"))
        self.feed(wave_entry(0, "cleared", clearedAt=T0 + 600))
        self.assertEqual(self.delivered()[-1]["title"], "Water heater fault 10 cleared — Basement")

    def test_a_fault_that_comes_back_alerts_again(self):
        self.feed()
        self.feed(wave_entry(0, "active"))
        self.feed(wave_entry(0, "cleared"))
        out = self.feed(wave_entry(0, "active"))
        self.assertEqual(len(out.new_faults), 1)
        alert = self.delivered()[-1]
        self.assertEqual(alert["title"], "Water heater fault 10 (active again) — Basement")
        self.assertIn("ACTIVE again", alert["message"])
        self.assertEqual(self.faults()[0]["state"], "active")
        self.assertEqual(self.count("faults"), 1, "the same row, not a second one")

    def test_entries_that_never_say_anything_behave_exactly_as_before(self):
        self.feed()
        self.feed(event(7))
        self.assertEqual(self.delivered()[-1]["title"], "Water heater fault 10 — Basement")
        self.assertIsNone(self.faults()[0]["state"])
        sent = len(self.delivered())
        self.feed(event(7))
        self.assertEqual(len(self.delivered()), sent)

    def test_a_rewritten_entry_is_the_same_fault_not_two(self):
        # no unique id, and Wave gives the entry a new time when it clears: its identity changes, the fault does not
        self.feed()
        self.feed(wave_entry(0, "active"))
        faults_before = len(self.delivered())
        out = self.feed(wave_entry(50, "cleared"))
        self.assertEqual(out.new_faults, [], "no second fault alert")
        self.assertEqual(self.count("faults", "WHERE kind = 'event'"), 1, "one row, re-keyed")
        self.assertEqual(self.faults()[0]["state"], "cleared")
        self.assertEqual(self.delivered()[-1]["title"], "Water heater fault 10 cleared — Basement")
        self.assertEqual(len(self.delivered()), faults_before + 1)
        sent = len(self.delivered())
        self.feed(wave_entry(50, "cleared"))
        self.assertEqual(len(self.delivered()), sent, "and the re-keyed row is recognised afterwards")

    def test_an_active_entry_that_is_still_listed_is_never_merged_with_another(self):
        self.feed()
        self.feed(wave_entry(0, "active"))
        out = self.feed(wave_entry(0, "active"), wave_entry(60, "cleared"))
        self.assertEqual(len(out.new_faults), 1, "a second occurrence that already cleared")
        states = sorted((r["occurred_at"], r["state"]) for r in self.faults())
        self.assertEqual([s for _, s in states], ["active", "cleared"])
        self.assertEqual(self.delivered()[-1]["title"], "Water heater fault 10 (cleared) — Basement")

    def test_a_different_code_is_never_merged(self):
        self.feed()
        self.feed(wave_entry(0, "active", code=10))
        out = self.feed(wave_entry(50, "cleared", code=11, text="Other"))
        self.assertEqual([f.code for f in out.new_faults], ["11"])
        self.assertEqual(self.faults(code="10")[0]["state"], "active")

    def test_a_unique_id_survives_the_rewrite_without_any_merging(self):
        self.feed()
        self.feed(wave_entry(0, "active", id=UUID))
        out = self.feed(wave_entry(55, "cleared", id=UUID, clearedAt=T0 + 55 * 60))
        self.assertEqual(out.new_faults, [])
        self.assertEqual(self.count("faults", "WHERE kind = 'event'"), 1)
        self.assertEqual(self.delivered()[-1]["title"], "Water heater fault 10 cleared — Basement")

    def test_a_recurrence_after_it_cleared_is_a_new_fault(self):
        self.feed()
        self.feed(wave_entry(0, "cleared"))
        out = self.feed(wave_entry(0, "cleared"), wave_entry(300, "active"))
        self.assertEqual(len(out.new_faults), 1)
        self.assertEqual(self.count("faults", "WHERE kind = 'event'"), 2)

    def test_the_log_commands_show_what_happened(self):
        import argparse
        import contextlib
        import io

        from bwwatch import cli

        self.feed()
        self.feed(wave_entry(0, "active"))
        self.feed(wave_entry(0, "cleared", clearedAt=T0 + 42 * 60))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_faults(self.svc.cfg, argparse.Namespace(limit=20, all=False, raw=False))
            cli.cmd_status(self.svc.cfg, argparse.Namespace())
        text = out.getvalue()
        for expected in ("OCCURRED", "STATE", "cleared", "Superheat Fault", "Active fault entries: none"):
            self.assertIn(expected, text)
        self.assertIn("cleared 20", text, "the time the entry itself gave for clearing")
        self.feed(wave_entry(0, "cleared", clearedAt=T0 + 42 * 60), wave_entry(500, "active", code=12, text="Sensor"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_status(self.svc.cfg, argparse.Namespace())
        self.assertIn("Active fault entries: code 12 Sensor", out.getvalue())
        csv_out = io.StringIO()
        with contextlib.redirect_stdout(csv_out):
            cli.cmd_export(self.svc.cfg, argparse.Namespace(table="faults", out=None))
        header = csv_out.getvalue().splitlines()[0].split(",")
        for column in ("state", "cleared_at", "cleared_seen_at", "occurred_at"):
            self.assertIn(column, header)


class FieldLog(CycleCase):
    """Everything the cloud reports is kept, field by field, changes only."""

    def obs(self, **where):
        rows = self.svc.conn.execute("SELECT * FROM observations ORDER BY id").fetchall()
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def test_the_first_poll_logs_every_field_of_the_list_and_the_status(self):
        self.poll()
        paths = {(r["source"], r["path"]) for r in self.obs()}
        for expected in (("list", "friendlyName"), ("list", "applianceType"), ("status", "setpointFahrenheit"),
                         ("status", "mode"), ("status", "heatModeValue")):
            self.assertIn(expected, paths)
        self.assertNotIn(("status", "requestId"), paths, "a field that changes on every call is not news")
        self.assertTrue(all(r["event"] == "start" for r in self.obs()), "the starting values are not 'changes'")
        self.assertIn("Logging every field of the status answer", self.delivered()[0]["message"])

    def test_a_poll_where_nothing_changed_adds_nothing(self):
        self.poll()
        before = self.count("observations")
        out = self.poll()
        self.assertEqual(self.count("observations"), before)
        self.assertEqual(out.field_changes, 0)

    def test_a_change_in_any_field_is_logged_with_old_and_new(self):
        self.poll()
        self.mock.status[MAC].update({"compressorState": "off", "setpointFahrenheit": 125})
        out = self.poll()
        by_path = {r["path"]: r for r in self.obs(source="status") if r["event"] != "new" or r["path"] == "compressorState"}
        self.assertEqual((by_path["setpointFahrenheit"]["old_value"], by_path["setpointFahrenheit"]["new_value"]), ("120", "125"))
        self.assertEqual(by_path["compressorState"]["event"], "new")
        self.assertEqual(out.field_changes, 2)
        self.mock.status[MAC]["compressorState"] = "on"
        self.poll()
        self.assertEqual([(r["old_value"], r["new_value"]) for r in self.obs(path="compressorState", event="changed")], [("off", "on")])

    def test_a_field_that_disappears_and_returns_is_logged(self):
        self.mock.status[MAC]["errorState"] = "none"
        self.poll()
        del self.mock.status[MAC]["errorState"]
        self.poll()
        self.assertEqual([r["event"] for r in self.obs(path="errorState")], ["start", "gone"])
        self.mock.status[MAC]["errorState"] = "overheat"
        self.poll()
        self.assertEqual([r["event"] for r in self.obs(path="errorState")], ["start", "gone", "back"])

    def test_the_fault_history_contributes_only_its_plain_fields(self):
        self.mock.notifications = {"count": 1, "notifications": [event(1)]}
        self.poll()
        paths = {r["path"] for r in self.obs(source="faults")}
        self.assertEqual(paths, {"count"}, "its entries are in the faults table; indexes into a growing list would only churn")

    def test_it_can_be_switched_off(self):
        self.svc = self.service(self.cfg(BW_LOG_FIELDS="false"))
        self.poll()
        self.assertEqual(self.count("observations"), 0)
        self.assertEqual(self.count("field_state"), 0)
        self.assertGreater(self.count("snapshots"), 0, "the raw answers are still kept")

    def test_watched_fields_alert_only_when_they_change_after_the_start(self):
        self.svc = self.service(self.cfg(BW_WATCH_FIELDS=r"status\.(compressor|error)"))
        self.mock.status[MAC]["compressorState"] = "off"
        self.poll()
        self.assertEqual(self.kinds(), ["info"], "the starting values are not news")
        def field_alerts():
            return [m for m in self.delivered() if m["title"] == "Wave field changed — Basement"]

        self.mock.status[MAC].update({"compressorState": "on", "setpointFahrenheit": 130})
        self.poll()
        (alert,) = field_alerts()
        self.assertIn("status.compressorState: off -> on", alert["message"])
        self.assertNotIn("setpointFahrenheit", alert["message"], "only the fields you asked about")
        self.mock.status[MAC]["errorState"] = "overheat"
        self.poll()
        self.assertIn("status.errorState: (not there) -> overheat", field_alerts()[-1]["message"])
        self.assertIn("[new]", field_alerts()[-1]["message"])
        sent = len(field_alerts())
        self.poll()
        self.assertEqual(len(field_alerts()), sent, "and only once")

    def test_watching_nothing_is_the_default(self):
        self.poll()
        self.mock.status[MAC].update({"compressorState": "on", "faultish": "x"})
        self.poll()
        self.assertNotIn("setting", self.kinds())

    def test_a_bug_in_field_logging_cannot_lose_the_rest_of_the_poll(self):
        self.poll()
        with mock.patch("bwwatch.observe.record", side_effect=ValueError("surprise")):
            out = self.poll()
        self.assertFalse(out.ok)
        self.assertIn("logging every field", out.error)
        self.assertEqual(self.count("readings"), 2, "the settings were still recorded")
        self.assertEqual(self.count("polls"), 2)


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
