"""Keeping every field: splitting answers into fields, and logging only what changed."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from bwwatch import db, observe
from bwwatch.observe import Change, flatten, record, scalar

VOLATILE = frozenset({"requestid", "read"})
T1, T2, T3 = "2026-10-04T10:00:00Z", "2026-10-04T11:00:00Z", "2026-10-04T12:00:00Z"


class Flatten(unittest.TestCase):
    def test_nested_answers_become_dotted_paths(self):
        fields = flatten({"a": {"b": [1, {"c": "x"}]}, "d": True, "e": None, "f": 1.5})
        self.assertEqual(fields, {
            "a.b[0]": ("1", "int"), "a.b[1].c": ("x", "str"), "d": ("true", "bool"), "e": (None, "null"), "f": ("1.5", "float"),
        })

    def test_a_number_and_the_same_text_are_different_values(self):
        self.assertNotEqual(scalar(1), scalar("1"))
        self.assertNotEqual(scalar(True), scalar("true"))
        self.assertEqual(scalar(0.1 + 0.2), ("0.3", "float"), "no binary-float noise in the log")

    def test_fields_that_change_on_every_call_are_left_out_at_any_depth(self):
        fields = flatten({"requestId": "r1", "x": {"RequestId": "r2", "keep": 1}, "list": [{"read": True, "a": 2}]}, VOLATILE)
        self.assertEqual(sorted(fields), ["list[0].a", "x.keep"])

    def test_event_logs_can_be_reduced_to_their_plain_fields(self):
        payload = {"count": 3, "notifications": [{"id": 1}, {"id": 2}], "meta": {"page": 1}}
        self.assertEqual(sorted(flatten(payload, skip_lists=True)), ["count", "meta.page"])

    def test_empty_things_and_odd_roots(self):
        self.assertEqual(flatten({}), {})
        self.assertEqual(flatten({"a": [], "b": {}}), {})
        self.assertEqual(flatten(5), {"$": ("5", "int")})
        self.assertEqual(flatten([1]), {"[0]": ("1", "int")})
        self.assertEqual(flatten("text"), {"$": ("text", "str")})

    def test_limits_keep_a_huge_answer_from_flooding_the_log(self):
        self.assertEqual(len(flatten({"l": list(range(observe.MAX_LIST + 50))})), observe.MAX_LIST)
        deep = cur = {}
        for _ in range(observe.MAX_DEPTH + 5):
            cur["n"] = {}
            cur = cur["n"]
        cur["leaf"] = 1
        self.assertEqual(flatten(deep), {})
        wide = {"k%d" % i: i for i in range(observe.MAX_FIELDS + 100)}
        self.assertEqual(len(flatten(wide)), observe.MAX_FIELDS)
        self.assertEqual(len(flatten({"s": "x" * 5000})["s"][0]), observe.MAX_TEXT)

    def test_a_key_containing_a_dot_is_still_recorded(self):
        self.assertEqual(flatten({"a.b": 1}), {"a.b": ("1", "int")})


class Recording(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bwobs."))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.conn, _ = db.open_database(self.dir)
        self.addCleanup(self.conn.close)

    def rec(self, payload, now=T1, mac="AA", source="status", **kw):
        with db.tx(self.conn):
            return record(self.conn, mac, source, payload, now, VOLATILE, **kw)

    def log(self, **where):
        rows = self.conn.execute("SELECT * FROM observations ORDER BY id").fetchall()
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def test_the_first_look_logs_every_field_as_the_starting_picture(self):  # event "start", not "new"
        changes, first = self.rec({"a": 1, "b": "x"})
        self.assertTrue(first)
        self.assertEqual({(c.path, c.event) for c in changes}, {("a", "start"), ("b", "start")})
        self.assertEqual(len(self.log()), 2)

    def test_a_look_that_changes_nothing_logs_nothing(self):
        self.rec({"a": 1, "b": "x"})
        changes, first = self.rec({"a": 1, "b": "x"}, T2)
        self.assertEqual((changes, first), ([], False))
        self.assertEqual(len(self.log()), 2)
        row = self.conn.execute("SELECT * FROM field_state WHERE path = 'a'").fetchone()
        self.assertEqual((row["first_seen_at"], row["last_changed_at"], row["last_seen_at"], row["changes"]), (T1, T1, T2, 0))

    def test_a_change_is_logged_with_what_it_was_and_what_it_became(self):
        self.rec({"mode": "Heat Pump", "setpoint": 120})
        changes, _ = self.rec({"mode": "Electric", "setpoint": 120}, T2)
        self.assertEqual(changes, [Change("status", "mode", "changed", "Heat Pump", "Electric")])
        (entry,) = self.log(event="changed")
        self.assertEqual((entry["taken_at"], entry["old_value"], entry["new_value"]), (T2, "Heat Pump", "Electric"))
        state = self.conn.execute("SELECT * FROM field_state WHERE path = 'mode'").fetchone()
        self.assertEqual((state["value"], state["changes"], state["last_changed_at"]), ("Electric", 1, T2))

    def test_the_type_counts_as_part_of_the_value(self):
        self.rec({"n": "1"})
        changes, _ = self.rec({"n": 1}, T2)
        self.assertEqual([c.event for c in changes], ["changed"])

    def test_a_field_that_vanishes_and_returns_is_logged_both_ways(self):
        self.rec({"a": 1, "faultCode": 10})
        changes, _ = self.rec({"a": 1}, T2)
        self.assertEqual(changes, [Change("status", "faultCode", "gone", "10", None)])
        self.assertEqual(self.conn.execute("SELECT present FROM field_state WHERE path = 'faultCode'").fetchone()[0], 0)
        self.assertEqual(self.rec({"a": 1}, T3)[0], [], "staying gone is not another event")
        changes, _ = self.rec({"a": 1, "faultCode": 12}, T3)
        self.assertEqual(changes, [Change("status", "faultCode", "back", "10", "12")])
        self.assertEqual(self.conn.execute("SELECT present, value, changes FROM field_state WHERE path = 'faultCode'").fetchone()[:], (1, "12", 2))

    def test_a_brand_new_field_later_on_is_news_not_a_starting_picture(self):
        self.rec({"a": 1})
        changes, first = self.rec({"a": 1, "errorState": "overheat"}, T2)
        self.assertFalse(first)
        self.assertEqual(changes, [Change("status", "errorState", "new", None, "overheat")])

    def test_null_values_and_back_and_forth_changes(self):
        self.rec({"x": None})
        self.assertEqual(self.rec({"x": None}, T2)[0], [])
        self.assertEqual([c.event for c in self.rec({"x": 5}, T2)[0]], ["changed"])
        self.assertEqual([c.event for c in self.rec({"x": None}, T3)[0]], ["changed"])
        self.assertEqual(self.conn.execute("SELECT changes FROM field_state").fetchone()[0], 2)

    def test_volatile_fields_never_appear(self):
        self.rec({"a": 1, "requestId": "one"})
        changes, _ = self.rec({"a": 1, "requestId": "two"}, T2)
        self.assertEqual(changes, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM field_state WHERE path = 'requestId'").fetchone()[0], 0)

    def test_heaters_and_sources_do_not_mix(self):
        self.rec({"a": 1}, mac="AA")
        self.rec({"a": 2}, mac="BB")
        self.rec({"a": 3}, mac="AA", source="list")
        self.assertEqual(self.rec({"a": 1}, T2, mac="AA")[0], [])
        self.assertEqual(self.rec({"a": 2}, T2, mac="BB")[0], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM field_state").fetchone()[0], 3)

    def test_nothing_else_in_the_database_changes(self):
        self.rec({"a": 1})
        for table in ("faults", "readings", "outbox"):
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0], 0)


class Pruning(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bwobs."))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.conn, _ = db.open_database(self.dir)
        self.addCleanup(self.conn.close)

    def test_old_history_is_forgotten_but_current_values_never_are(self):
        with db.tx(self.conn):
            record(self.conn, "AA", "status", {"keep": 1, "old": 2}, "2025-01-01T00:00:00Z")
            record(self.conn, "AA", "status", {"keep": 1}, "2025-01-02T00:00:00Z")       # "old" is gone since then
            record(self.conn, "AA", "status", {"keep": 5}, "2026-10-01T00:00:00Z")      # a recent change
            self.conn.execute("INSERT INTO api_calls(taken_at, kind, endpoint) VALUES('2025-01-01T00:00:00Z', 'api', 'GET /x')")
            self.conn.execute("INSERT INTO api_calls(taken_at, kind, endpoint) VALUES('2026-10-03T00:00:00Z', 'api', 'GET /x')")
            removed = observe.prune(self.conn, 365, "2026-10-04T00:00:00Z")
        self.assertGreater(removed, 0)
        paths = {r["path"] for r in self.conn.execute("SELECT path FROM observations")}
        self.assertEqual(paths, {"keep"}, "only the recent change is left")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT value FROM field_state WHERE path = 'keep'").fetchone()[0], "5")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM field_state WHERE path = 'old'").fetchone()[0], 0,
                         "a field that went away long ago is forgotten")

    def test_zero_means_keep_everything(self):
        with db.tx(self.conn):
            record(self.conn, "AA", "status", {"a": 1}, "2020-01-01T00:00:00Z")
            self.assertEqual(observe.prune(self.conn, 0, "2026-10-04T00:00:00Z"), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
