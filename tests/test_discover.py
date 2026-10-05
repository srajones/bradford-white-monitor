"""Learning /wave/getApplianceErrors, and polling faster while a fault entry is active."""
from __future__ import annotations

import argparse

from bwwatch import cli, views
from bwwatch.discover import META_KEY
from bwwatch.util import iso

from .helpers import WaveTestCase


class Learn(WaveTestCase):
    def test_the_first_form_that_returns_json_is_remembered_and_read(self):
        self.mock.extra_routes["getApplianceErrors"] = (200, {
            "error_history": [{
                "error_code": 10, "error_string": "(Cleared) Superheat Fault", "timestamp": "2026-10-04T18:15:00Z",
            }],
            "active_errors": [],
        })
        svc = self.service(self.cfg(BW_FAULT_REQUEST=""))
        out = svc.cycle()
        self.assertTrue(out.ok, out.error)
        learned = svc.conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()[0]
        self.assertEqual(learned, "GET /wave/getApplianceErrors?macAddress={mac}")
        row = svc.conn.execute("SELECT code, state, baseline FROM faults").fetchone()
        self.assertEqual((row["code"], row["state"], row["baseline"]), ("10", "cleared", 1))
        titles = [r["title"] for r in self.outbox(svc)]
        self.assertIn("Notifications list found", titles)
        svc.cycle()
        # remembered: no second search, just the normal per-heater read
        self.assertEqual(len(self.mock.api_hits("getApplianceErrors")), 3)  # learn + poll, then one more poll

    def test_a_miss_is_not_retried_on_the_next_poll(self):
        svc = self.service(self.cfg(BW_FAULT_REQUEST=""))
        svc.cycle()
        self.assertEqual(len(self.mock.api_hits("getApplianceErrors")), 3)
        body = svc.conn.execute("SELECT body FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn("BW_FAULT_REQUEST is not set", body)
        svc.cycle()
        self.assertEqual(len(self.mock.api_hits("getApplianceErrors")), 3)

    def test_an_explicit_request_is_not_replaced(self):
        self.mock.extra_routes["getApplianceErrors"] = (200, {"error_history": []})
        svc = self.service()
        svc.cycle()
        self.assertEqual(self.mock.api_hits("getApplianceErrors"), [])
        self.assertIsNone(svc.conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone())

    def test_discover_reset_forgets_it(self):
        self.mock.extra_routes["getApplianceErrors"] = (200, {"error_history": []})
        svc = self.service(self.cfg(BW_FAULT_REQUEST=""))
        svc.cycle()
        status, out = self._run(cli.cmd_discover, svc.cfg, reset=True)
        self.assertEqual(status, 0, out)
        self.assertIn("Forgotten", out)
        self.assertIsNone(svc.conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone())

    def test_calls_lists_the_request_log(self):
        svc = self.service()
        svc.cycle()
        status, out = self._run(views.cmd_calls, svc.cfg, hours=24)
        self.assertEqual(status, 0, out)
        self.assertIn("GET /wave/getApplianceList", out)
        self.assertIn("GET /wave/getApplianceStatus", out)

    def _run(self, fn, cfg, **ns):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(cfg, argparse.Namespace(**ns))
        return code, out.getvalue()


class FasterPoll(WaveTestCase):
    def test_an_active_fault_shortens_the_interval_only_inside_the_window(self):
        svc = self.service()
        self.assertEqual(svc.next_delay(), 3600)

        def plant(when: str) -> None:
            svc.conn.execute("DELETE FROM faults")
            svc.conn.execute(
                """INSERT INTO faults(mac, kind, source, fingerprint, code, description, occurred_at,
                     first_seen_at, last_seen_at, state, baseline, raw)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("AA:BB:CC:DD:EE:FF", "event", "faults", "fp", "10", "Superheat", when, when, when, "active", 0, "{}"),
            )

        plant(iso())
        self.assertEqual(svc.next_delay(), 600)
        plant("2020-01-01T00:00:00Z")
        self.assertEqual(svc.next_delay(), 3600)
