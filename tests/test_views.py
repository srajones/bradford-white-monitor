"""The commands that show what has been logged: fields, changes."""
from __future__ import annotations

import argparse
import contextlib
import io
import time
import unittest

from bwwatch import cli, views
from bwwatch.util import iso

from .helpers import MAC, WaveTestCase


class ViewCase(WaveTestCase):
    def setUp(self):
        super().setUp()
        self.svc = self.service()

    def run_cmd(self, fn, **ns):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(self.svc.cfg, argparse.Namespace(**ns))
        return code, out.getvalue()

    def fields(self, **ns):
        return self.run_cmd(views.cmd_fields, match=ns.get("match"))

    def changes(self, **ns):
        args = dict(hours=24.0, match=None, around=None, minutes=180, limit=200, all=False)
        args.update(ns)
        return self.run_cmd(views.cmd_changes, **args)


class Fields(ViewCase):
    def test_nothing_before_the_first_poll(self):
        code, out = self.fields()
        self.assertEqual(code, 0)
        self.assertIn("No fields logged yet", out)

    def test_every_field_with_its_value_and_change_count(self):
        self.svc.cycle()
        self.mock.status[MAC].update({"compressorState": "off"})
        self.svc.cycle()
        self.mock.status[MAC].update({"compressorState": "on", "setpointFahrenheit": 125})
        self.svc.cycle()
        code, out = self.fields()
        self.assertEqual(code, 0)
        for text in ("FIELD", "VALUE NOW", "CHANGES", "status.compressorState", "status.setpointFahrenheit", "list.friendlyName", "Basement"):
            self.assertIn(text, out)
        row = [l for l in out.splitlines() if l.startswith("status.compressorState")][0]
        self.assertRegex(row, r"status\.compressorState\s+on\s+1\b")
        self.assertRegex([l for l in out.splitlines() if l.startswith("list.friendlyName")][0], r"\s0\s.*never")
        self.assertIn("have changed at least once", out)

    def test_a_vanished_field_is_shown_as_gone(self):
        self.mock.status[MAC]["errorState"] = "none"
        self.svc.cycle()
        del self.mock.status[MAC]["errorState"]
        self.svc.cycle()
        _, out = self.fields()
        self.assertIn("(gone; was none)", out)

    def test_it_can_be_narrowed_with_a_pattern(self):
        self.svc.cycle()
        _, out = self.fields(match="setpoint|MODE")
        names = [l.split()[0] for l in out.splitlines()[2:] if l.startswith(("status", "list"))]
        self.assertTrue(names and all("setpoint" in n.lower() or "mode" in n.lower() for n in names), names)
        _, out = self.fields(match="zzz-no-such")
        self.assertIn("No logged field matches", out)

    def test_a_bad_pattern_is_explained(self):
        self.svc.cycle()
        with self.assertRaises(ValueError) as caught:
            self.fields(match="(")
        self.assertIn("not a valid regular expression", str(caught.exception))


class Changes(ViewCase):
    def test_the_starting_picture_is_not_a_change(self):
        self.svc.cycle()
        _, out = self.changes()
        self.assertIn("Nothing changed", out)
        _, out = self.changes(all=True)
        self.assertIn("first seen:", out, "--all includes it")

    def test_changes_are_listed_oldest_first_with_what_happened(self):
        self.svc.cycle()
        self.mock.status[MAC]["compressorState"] = "off"
        self.svc.cycle()
        self.mock.status[MAC].update({"compressorState": "on", "mode": "Electric"})
        self.svc.cycle()
        del self.mock.status[MAC]["compressorState"]
        self.svc.cycle()
        code, out = self.changes()
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split()[0], "WHEN".split()[0])
        self.assertIn("status.compressorState", out)
        self.assertIn("appeared: off", out)
        self.assertIn("off -> on", out)
        self.assertIn("Heat Pump -> Electric", out)
        self.assertIn("disappeared (was on)", out)
        order = [i for i, l in enumerate(lines) if "status.compressorState" in l]
        self.assertEqual(len(order), 3)
        self.assertLess(out.index("appeared: off"), out.index("off -> on"))
        self.assertLess(out.index("off -> on"), out.index("disappeared"))

    def test_filters_limits_and_time_windows(self):
        self.svc.cycle()
        for n in range(4):
            self.mock.status[MAC]["setpointFahrenheit"] = 121 + n
            self.mock.status[MAC]["note"] = "n%d" % n
            self.svc.cycle()
        _, out = self.changes(match="setpoint")
        self.assertNotIn("status.note", out)
        self.assertEqual(out.count("status.setpointFahrenheit"), 4)
        _, out = self.changes(match="setpoint", limit=2)
        self.assertEqual(out.count("status.setpointFahrenheit"), 2)
        self.assertIn("showing the latest 2 of 4", out)
        _, out = self.changes(hours=0)
        self.assertIn("status.setpointFahrenheit", out)
        time.sleep(1.1)  # stamps have one-second precision
        _, out = self.changes(hours=0.00001)
        self.assertIn("Nothing changed", out, "a window of a split second contains nothing")

    def test_around_a_moment_shows_the_hours_either_side(self):
        self.svc.cycle()
        self.mock.status[MAC]["compressorState"] = "on"
        self.svc.cycle()
        now = iso()
        _, out = self.changes(around=now, minutes=30)
        self.assertIn("compressorState", out)
        _, out = self.changes(around="2020-01-01 00:00", minutes=30)
        self.assertIn("Nothing changed", out)

    def test_nothing_is_written_by_looking(self):
        self.svc.cycle()
        before = self.count_rows()
        self.fields()
        self.changes(all=True)
        self.assertEqual(self.count_rows(), before)

    def count_rows(self):
        return tuple(self.svc.conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in ("observations", "field_state", "polls"))


class Dates(unittest.TestCase):
    def test_times_in_your_zone_become_utc(self):
        self.assertEqual(views.parse_when("2026-10-04 13:15", "America/Chicago"), "2026-10-04T18:15:00Z")
        self.assertEqual(views.parse_when("2026-10-04T13:15", "America/New_York"), "2026-10-04T17:15:00Z")
        self.assertEqual(views.parse_when("2026-10-04T17:15:00Z", "America/New_York"), "2026-10-04T17:15:00Z")
        self.assertEqual(views.parse_when("2026-10-04", "UTC"), "2026-10-04T00:00:00Z")
        self.assertEqual(views.parse_when("2026-10-04 13:15:30", "UTC"), "2026-10-04T13:15:30Z")

    def test_nonsense_is_explained(self):
        with self.assertRaises(ValueError) as caught:
            views.parse_when("yesterday-ish", "UTC")
        self.assertIn("something like", str(caught.exception))


class Parser(unittest.TestCase):
    def test_the_commands_exist_and_parse(self):
        parser = cli.build_parser()
        args = parser.parse_args(["changes", "--around", "2026-10-04 13:15", "--minutes", "60", "--match", "x"])
        self.assertEqual((args.command, args.minutes, args.match), ("changes", 60, "x"))
        self.assertEqual(parser.parse_args(["fields"]).command, "fields")
        self.assertIn("fields", cli.COMMANDS)
        self.assertIn("changes", cli.COMMANDS)


if __name__ == "__main__":
    unittest.main()
