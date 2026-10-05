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

    def test_the_wave_apps_own_key_names(self):
        """The Wave app (v1.1.3371) reads `error_history` / `error_code` / `error_string` / `timestamp`."""
        payload = {
            "status_code": 200,
            "message": "Success",
            "error_history": [
                {"error_code": 10, "error_string": "Superheat Fault", "timestamp": "2026-10-04T18:15:00Z"},
                {"error_code": 3, "error_string": "Upper Thermistor Fault", "timestamp": "2026-09-30T02:00:00Z"},
            ],
        }
        events, where = extract_events(payload, OPTS)
        self.assertEqual(where, "error_history")
        self.assertEqual([e.code for e in events], ["10", "3"])
        self.assertEqual(events[0].description, "Superheat Fault")
        self.assertEqual(events[0].occurred_at, "2026-10-04T18:15:00Z")
        self.assertEqual(extract_events({"error_history": [], "message": "Success"}, OPTS)[0], [], "an empty history is 'no faults'")

    def test_active_errors_marks_the_current_fault_active(self):
        """`active_errors` is the app's name for the fault that is happening now (libapp.so)."""
        payload = {
            "error_history": [
                {"error_code": 10, "error_string": "(Cleared) Superheat Fault", "timestamp": "2026-10-04T18:15:00Z"},
                {"error_code": 3, "error_string": "Upper Thermistor Fault", "timestamp": "2026-10-04T19:00:00Z"},
            ],
            "active_errors": [
                {"error_code": 3, "error_string": "Upper Thermistor Fault", "timestamp": "2026-10-04T19:00:00Z"},
            ],
        }
        events, where = extract_events(payload, OPTS)
        self.assertEqual(where, "error_history")
        by_code = {e.code: e.state for e in events}
        self.assertEqual(by_code, {"10": "cleared", "3": "active"})

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
        self.assertEqual([e.fingerprint for e in events], ["id=1|code=10"], "the code is read from the wording too")


class Cleared(unittest.TestCase):
    """The Wave app lists a fault as "Fault 10 / (Cleared) Superheat Fault"; these are the ways the data may say so."""

    def one(self, item, **opts):
        events, _ = extract_events([item], FaultOptions(volatile=VOLATILE, **opts))
        self.assertEqual(len(events), 1)
        return events[0]

    def test_the_apps_own_wording_is_understood(self):
        ev = self.one({"title": "Fault 10", "message": "(Cleared) Superheat Fault", "timestamp": "2026-10-04T17:15:00Z"})
        self.assertEqual((ev.code, ev.description, ev.state), ("10", "Superheat Fault", "cleared"))
        self.assertEqual(ev.occurred_at, "2026-10-04T17:15:00Z")
        self.assertEqual(ev.fingerprint, "code=10|at=2026-10-04T17:15:00Z")
        self.assertIn("(Cleared) Superheat Fault", ev.raw, "the raw entry is kept exactly as it came")

    def test_the_marker_in_other_forms(self):
        for text in ("(Cleared) Superheat Fault", "[Resolved] Superheat Fault", "Cleared - Superheat Fault",
                     "CLEARED: Superheat Fault", "(cleared)Superheat Fault"):
            with self.subTest(text):
                ev = self.one({"faultCode": 10, "description": text})
                self.assertEqual((ev.state, ev.description), ("cleared", "Superheat Fault"))

    def test_status_words_flags_and_end_times(self):
        cleared = [
            {"status": "CLEARED"}, {"state": "Resolved"}, {"alertStatus": "recovered"}, {"isCleared": True},
            {"cleared": "yes"}, {"active": False}, {"isActive": "false"}, {"clearedAt": "2026-10-04T17:45:00Z"},
            {"resolvedTime": 1760000000}, {"endTime": "2026-10-04T17:45:00Z"},
        ]
        active = [{"status": "Active"}, {"state": "OPEN"}, {"isActive": True}, {"cleared": False}, {"resolved": 0}, {"status": "unresolved"}]
        unknown = [{}, {"status": "unread"}, {"read": True}, {"endTime": None}, {"endTime": ""}, {"clearedAt": 0}, {"status": "ok"}]
        for extra in cleared:
            with self.subTest(cleared=extra):
                self.assertEqual(self.one(dict({"faultCode": 10, "description": "x"}, **extra)).state, "cleared")
        for extra in active:
            with self.subTest(active=extra):
                self.assertEqual(self.one(dict({"faultCode": 10, "description": "x"}, **extra)).state, "active")
        for extra in unknown:
            with self.subTest(unknown=extra):
                self.assertIsNone(self.one(dict({"faultCode": 10, "description": "x"}, **extra)).state, "no explicit evidence: no guess")

    def test_cleared_wins_over_a_stale_active_hint(self):
        ev = self.one({"faultCode": 10, "description": "(Cleared) Superheat", "status": "Active"})
        self.assertEqual(ev.state, "cleared")

    def test_the_time_it_cleared_is_kept_when_given(self):
        ev = self.one({"faultCode": 10, "description": "x", "time": "2026-10-04T17:15:00Z", "clearedAt": "2026-10-04T17:45:00Z"})
        self.assertEqual((ev.state, ev.cleared_at), ("cleared", "2026-10-04T17:45:00Z"))
        ev = self.one({"faultCode": 10, "description": "x", "clearedAt": 1760000000})
        self.assertEqual(ev.cleared_at, coerce_time(1760000000))
        self.assertIsNone(self.one({"faultCode": 10, "description": "(Cleared) x"}).cleared_at, "a marker alone gives no time")

    def test_an_entry_that_says_nothing_has_no_state(self):
        ev = self.one({"faultCode": 10, "description": "Superheat Fault", "timestamp": 1760000000})
        self.assertIsNone(ev.state)

    def test_the_code_is_read_from_the_wording_when_there_is_no_code_field(self):
        for text, code in (("Fault 10", "10"), ("fault #7", "7"), ("Fault Code 12", "12"), ("Error: 44", "44"), ("ALARM 3", "3")):
            with self.subTest(text):
                self.assertEqual(self.one({"title": text, "timestamp": 1760000000}).code, code)
        self.assertIsNone(self.one({"title": "Vacation mode ends tomorrow", "timestamp": 1760000000}).code)
        self.assertEqual(self.one({"title": "Fault 10", "faultCode": 77}).code, "77", "an explicit field wins")
        self.assertIsNone(self.one({"title": "Fault 10"}, code_field="nosuchfield").code, "a chosen field is used alone")

    def test_fault_n_alone_is_not_repeated_in_the_description(self):
        self.assertEqual(self.one({"title": "Fault 10", "message": "Superheat Fault"}).description, "Superheat Fault")
        self.assertEqual(self.one({"title": "Fault 10"}).description, "Fault 10", "kept when it is all there is")

    def test_the_identity_does_not_depend_on_the_state(self):
        active = self.one({"title": "Fault 10", "message": "Superheat Fault", "timestamp": 1760000000})
        cleared = self.one({"title": "Fault 10", "message": "(Cleared) Superheat Fault", "timestamp": 1760000000})
        self.assertEqual(active.fingerprint, cleared.fingerprint)
        self.assertNotEqual(active.state, cleared.state)

    def test_unique_looking_ids_identify_an_entry_on_their_own(self):
        uuid = "9f1c2a3e-1111-2222-3333-444455556666"
        a = self.one({"id": uuid, "faultCode": 10, "timestamp": 1760000000})
        b = self.one({"id": uuid, "faultCode": 10, "timestamp": 1760009999, "status": "Cleared"})
        self.assertEqual(a.fingerprint, b.fingerprint, "same entry even if the time was rewritten")
        self.assertEqual(a.fingerprint, "id:" + uuid)
        self.assertEqual(self.one({"id": 1234567, "faultCode": 10, "timestamp": 1}).fingerprint, "id:1234567")
        self.assertEqual(self.one({"id": "a1b2c3d4e5f60718", "faultCode": 10}).fingerprint, "id:a1b2c3d4e5f60718")

    def test_short_or_derived_ids_are_never_trusted_alone(self):
        for ident in (1, 7, 12345, "3", "fault-10", "abc", "notification"):
            with self.subTest(ident):
                fp = self.one({"id": ident, "faultCode": 10, "timestamp": 1760000000}).fingerprint
                self.assertIn("code=10", fp)
                self.assertIn("at=", fp)

    def test_looks_unique(self):
        from bwwatch.faults import looks_unique

        for good in ("9f1c2a3e-1111-2222-3333-444455556666", "123456", "1760000000123", "a1b2c3d4e5f60718", "Nx7Kq2Lm9Pz4Rt8Vw1"):
            self.assertTrue(looks_unique(good), good)
        for bad in (None, "", 0, 5, "12345", "fault-10", "abcdefghijklmnop", "short1", "1234567890 abcdef"):
            self.assertFalse(looks_unique(bad), bad)


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
