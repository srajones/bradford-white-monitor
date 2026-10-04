from __future__ import annotations

import re
import unittest

from bwwatch.config import FaultOptions
from bwwatch.faults import (
    digest,
    extract_events,
    find_event_list,
    is_clear,
    normalize_event,
    scan_state,
)
from bwwatch.readings import extract_reading, reading_changes
from bwwatch.util import coerce_time, scrub

VOLATILE = frozenset({"requestid", "read", "isread", "age"})
OPTS = FaultOptions(volatile=VOLATILE)


class FindList(unittest.TestCase):
    def test_common_shapes(self):
        item = {"code": 10}
        cases = {
            "bare list": ([item], "$"),
            "notifications key": ({"notifications": [item]}, "notifications"),
            "faultHistory camel": ({"status": "ok", "faultHistory": [item]}, "faultHistory"),
            "nested data": ({"data": {"alerts": [item]}}, "data.alerts"),
            "only one list of dicts": ({"weird": [item], "n": 1}, "weird"),
        }
        for name, (payload, where) in cases.items():
            with self.subTest(name):
                found, got = find_event_list(payload)
                self.assertEqual(found, [item])
                self.assertEqual(got, where)

    def test_empty_list_is_recognised_as_no_faults(self):
        self.assertEqual(find_event_list({"notifications": []})[0], [])
        self.assertEqual(find_event_list([])[0], [])

    def test_unrecognised_payload(self):
        found, why = find_event_list({"message": "No notifications"})
        self.assertIsNone(found)
        self.assertIn("no list", why)

    def test_explicit_path(self):
        payload = {"result": {"rows": [{"a": 1}]}}
        self.assertEqual(find_event_list(payload, "result.rows")[0], [{"a": 1}])
        self.assertIsNone(find_event_list(payload, "result.nope")[0])
        self.assertIsNone(find_event_list(payload, "result")[0])


class Normalize(unittest.TestCase):
    def test_fields_are_picked_up_with_varied_names(self):
        ev = normalize_event({"Fault_Code": 10, "message": "Heating element failure", "createdAt": 1760000000000}, OPTS)
        self.assertEqual(ev.code, "10")
        self.assertEqual(ev.description, "Heating element failure")
        self.assertEqual(ev.occurred_at, "2025-10-09T08:53:20Z")

    def test_nested_fault_object(self):
        ev = normalize_event({"id": "n1", "fault": {"code": "E4", "description": "Dry fire"}}, OPTS)
        self.assertEqual((ev.code, ev.description), ("E4", "Dry fire"))
        self.assertEqual(ev.fingerprint, "id=n1|code=E4")

    def test_fingerprint_ignores_volatile_fields(self):
        first = normalize_event({"title": "Fault", "code": 10, "read": False, "age": "1 min ago"}, OPTS)
        again = normalize_event({"title": "Fault", "code": 10, "read": True, "age": "9 min ago"}, OPTS)
        self.assertEqual(first.fingerprint, again.fingerprint)

    def test_fingerprint_changes_for_a_different_fault(self):
        a = normalize_event({"title": "Fault", "code": 10}, OPTS)
        b = normalize_event({"title": "Fault", "code": 11}, OPTS)
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_code_and_time_identify_a_recurrence(self):
        a = normalize_event({"code": 10, "timestamp": 1760000000}, OPTS)
        b = normalize_event({"code": 10, "timestamp": 1760003600}, OPTS)
        self.assertNotEqual(a.fingerprint, b.fingerprint)  # same code, a new occurrence -> new alert

    def test_user_overrides(self):
        opts = FaultOptions(code_field="weird_code", time_field="when", text_field="blurb", id_fields=("uid",), volatile=VOLATILE)
        ev = normalize_event({"weird_code": "X1", "when": "2026-01-02T03:04:05Z", "blurb": "hello", "uid": 77, "code": "ignored"}, opts)
        self.assertEqual((ev.code, ev.description, ev.occurred_at, ev.fingerprint), ("X1", "hello", "2026-01-02T03:04:05Z", "id:77"))

    def test_a_positional_id_cannot_hide_a_new_fault(self):
        # newest-first list whose "id" is just the row number: the new fault reuses id 0
        before = normalize_event({"id": 0, "faultCode": 10, "timestamp": 1760000000}, OPTS)
        new_fault = normalize_event({"id": 0, "faultCode": 7, "timestamp": 1760003600}, OPTS)
        shifted_old = normalize_event({"id": 1, "faultCode": 10, "timestamp": 1760000000}, OPTS)
        self.assertNotEqual(before.fingerprint, new_fault.fingerprint)
        self.assertNotEqual(before.fingerprint, shifted_old.fingerprint)  # same fault, new row number: a duplicate, never a miss

    def test_relative_times_do_not_make_an_entry_look_new_every_poll(self):
        a = normalize_event({"id": 5, "faultCode": 10, "time": "5 minutes ago"}, OPTS)
        b = normalize_event({"id": 5, "faultCode": 10, "time": "2 hours ago"}, OPTS)
        self.assertEqual(a.fingerprint, b.fingerprint)
        c = normalize_event({"faultCode": 10, "when": "yesterday", "msg": "x"}, OPTS)
        d = normalize_event({"faultCode": 10, "when": "3 days ago", "msg": "x"}, FaultOptions(volatile=VOLATILE | {"when"}))
        self.assertTrue(c.fingerprint.startswith("h:") and d.fingerprint.startswith("h:"))

    def test_zone_less_absolute_times_still_identify_an_entry(self):
        a = normalize_event({"faultCode": 10, "time": "2026-10-04 14:03:00"}, OPTS)
        b = normalize_event({"faultCode": 10, "time": "2026-10-04 15:03:00"}, OPTS)
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(a.fingerprint, "code=10|at=2026-10-04 14:03:00")

    def test_non_dict_entries_do_not_crash(self):
        ev = normalize_event("Fault 10 detected", OPTS)
        self.assertEqual(ev.description, "Fault 10 detected")
        self.assertTrue(ev.fingerprint.startswith("h:"))

    def test_extract_dedupes_and_filters(self):
        payload = {"notifications": [{"id": 1, "msg": "fault 10"}, {"id": 1, "msg": "fault 10"}, {"id": 2, "msg": "software update"}]}
        events, _ = extract_events(payload, FaultOptions(match=re.compile("fault"), volatile=VOLATILE))
        self.assertEqual([e.fingerprint for e in events], ["id=1"])


class Digest(unittest.TestCase):
    def test_volatile_keys_do_not_change_the_digest(self):
        self.assertEqual(digest({"mode": "A", "requestId": "1"}, VOLATILE), digest({"requestId": "2", "mode": "A"}, VOLATILE))
        self.assertNotEqual(digest({"mode": "A"}, VOLATILE), digest({"mode": "B"}, VOLATILE))


class ScanState(unittest.TestCase):
    IGNORE = re.compile(r"(?i)enabled|support|setting")

    def test_finds_active_fault_fields(self):
        doc = {"status": {"faultCode": 10, "mode": "Hybrid", "nested": {"errorMessage": "Sensor open"}}}
        found = dict(scan_state(doc, self.IGNORE))
        self.assertEqual(found["status.faultCode"], "10")
        self.assertEqual(found["status.nested.errorMessage"], "Sensor open")

    def test_clear_values_and_config_flags_are_not_faults(self):
        doc = {"faultCode": 0, "errors": [], "alert": "none", "error": None, "alarm": "No Alarm", "warning": False,
               "alertsEnabled": True, "errorReportingSupported": True, "fault": {"active": False, "code": 0}}
        self.assertEqual(scan_state(doc, self.IGNORE), [])

    def test_lists_and_dicts_under_fault_keys(self):
        found = scan_state({"faults": [{"code": 10}]}, self.IGNORE)
        self.assertEqual(found, [("faults", '[{"code":10}]')])

    def test_is_clear(self):
        for value in (None, False, 0, 0.0, "", "OK", "none", "No Faults", [], {}, [0, None], {"a": ""}):
            self.assertTrue(is_clear(value), value)
        for value in (True, 1, "E10", "Fault", [1], {"a": "x"}, -1):
            self.assertFalse(is_clear(value), value)


class Readings(unittest.TestCase):
    def test_status_payload(self):
        r = extract_reading({"mode": "Heat Pump", "heatModeValue": 3, "setpointFahrenheit": 120, "requestId": "x", "tankTempF": 118.5})
        self.assertEqual((r.mode, r.mode_value, r.setpoint_f, r.temps), ("Heat Pump", 3, 120.0, {"tankTempF": 118.5}))
        self.assertIn("Heat Pump", r.summary())

    def test_mode_name_from_value(self):
        self.assertEqual(extract_reading({"heatModeValue": 4}).mode, "Electric")

    def test_no_data(self):
        self.assertIsNone(extract_reading({"macAddress": "x"}))
        self.assertIsNone(extract_reading("nope"))

    def test_changes_only_report_settings_not_temperature_drift(self):
        a = extract_reading({"mode": "Heat Pump", "heatModeValue": 3, "setpointFahrenheit": 120, "tankTempF": 100})
        b = extract_reading({"mode": "Electric", "heatModeValue": 4, "setpointFahrenheit": 125, "tankTempF": 130})
        self.assertEqual(reading_changes(a, b), ["mode Heat Pump → Electric", "setpoint 120 → 125°F"])
        c = extract_reading({"mode": "Heat Pump", "heatModeValue": 3, "setpointFahrenheit": 120, "tankTempF": 140})
        self.assertEqual(reading_changes(a, c), [])


class Util(unittest.TestCase):
    def test_coerce_time(self):
        self.assertEqual(coerce_time(1760000000), "2025-10-09T08:53:20Z")
        self.assertEqual(coerce_time(1760000000000), "2025-10-09T08:53:20Z")
        self.assertEqual(coerce_time("1760000000"), "2025-10-09T08:53:20Z")
        self.assertEqual(coerce_time("2026-01-02T03:04:05Z"), "2026-01-02T03:04:05Z")
        self.assertEqual(coerce_time("2026-01-02T03:04:05-06:00"), "2026-01-02T09:04:05Z")
        self.assertEqual(coerce_time("2026-01-02 03:04:05"), "2026-01-02 03:04:05")  # no zone: left alone, not guessed
        self.assertIsNone(coerce_time(None))
        self.assertEqual(coerce_time("yesterday"), "yesterday")

    def test_scrub_removes_token_like_material(self):
        jwe = "eyJraWQiOiJjcGltY29yZV8wOTI1MjAxNSIsInZlciI6IjEuMCJ9.abcdefghijk.lmnopqrstuv.wxyz1234567.ABCDEFGH"
        text = scrub("failed code=%s Bearer abc.def.ghi password=hunter2 bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1 \"refresh_token\":\"zzz\"" % jwe)
        for secret in (jwe, "abc.def.ghi", "hunter2", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1", "zzz"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
