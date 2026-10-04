"""The service around the poll: pacing, restarts, shutdown, locking, health, maintenance, recovery."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import bwwatch.service as service_module
from bwwatch import db
from bwwatch.config import MIN_POLL_SECONDS, ConfigError
from bwwatch.privs import AlreadyRunning, acquire_lock
from bwwatch.service import Service, healthcheck

from .helpers import WaveTestCase


class Pacing(WaveTestCase):
    def test_default_is_hourly_and_five_minutes_is_the_floor(self):
        self.assertEqual(self.cfg().interval, 3600)
        self.assertEqual(MIN_POLL_SECONDS, 300)
        with self.assertRaises(ConfigError):
            self.cfg(BW_POLL_INTERVAL_SECONDS="299")
        self.assertEqual(self.cfg(BW_POLL_INTERVAL_SECONDS="300").interval, 300)

    def test_next_delay_stretches_after_repeated_failures(self):
        svc = self.service(self.cfg(BW_POLL_INTERVAL_SECONDS="300"))
        expected = {0: 300, 1: 300, 2: 300, 3: 600, 4: 1200, 5: 1800, 6: 1800, 40: 1800}
        for failures, delay in expected.items():
            svc.failures = failures
            self.assertEqual(svc.next_delay(), delay, "failures=%d" % failures)

    def test_an_hourly_schedule_is_never_made_slower_than_hourly_by_backoff(self):
        svc = self.service()
        for failures in (0, 3, 10, 100):
            svc.failures = failures
            self.assertEqual(svc.next_delay(), 3600)

    def test_a_rate_limit_request_is_honoured(self):
        svc = self.service(self.cfg(BW_POLL_INTERVAL_SECONDS="300"))
        svc.api.retry_after = 5400
        self.assertEqual(svc.next_delay(), 5400)
        svc.api.retry_after = 0
        svc.tokens.retry_after = 2000
        self.assertEqual(svc.next_delay(), 2000)

    def test_rate_limit_seen_during_a_poll_delays_the_next_one(self):
        svc = self.service(self.cfg(BW_POLL_INTERVAL_SECONDS="300"))
        self.mock.api_queue.append((429, {"message": "slow"}, {"Retry-After": "1200"}))
        out = svc.cycle()
        self.assertFalse(out.ok)
        self.assertEqual(svc.next_delay(), 1200)
        self.assertEqual(len(self.mock.api_hits("getApplianceList")), 1, "not retried within the poll")

    def test_a_restart_cannot_cause_rapid_repeat_polls(self):
        svc = self.service()
        svc.cycle()
        svc.shutdown()
        again = Service(svc.cfg)
        again.open()
        self.addCleanup(again.shutdown)
        wait = again.seconds_until_poll_allowed()
        self.assertGreater(wait, MIN_POLL_SECONDS - 30)
        self.assertLessEqual(wait, MIN_POLL_SECONDS)
        again.conn.execute("UPDATE meta SET value = ? WHERE key = 'poll.started_epoch'", (str(int(time.time()) - 301),))
        self.assertEqual(again.seconds_until_poll_allowed(), 0.0)

    def test_a_first_ever_start_polls_immediately(self):
        svc = self.service()
        self.assertEqual(svc.seconds_until_poll_allowed(), 0.0)

    def test_requests_per_poll_are_few(self):
        svc = self.service()
        svc.cycle()
        self.mock.requests.clear()
        svc.tokens.invalidate()  # worst case: a fresh sign-in on every poll
        svc.cycle()
        sign_in = len(self.mock.hits("/auth/token"))
        api = len(self.mock.hits("/wave/"))
        self.assertEqual((sign_in, api), (1, 3), "one sign-in refresh + list + status + fault history, for one heater")

    def test_failed_polls_make_no_extra_requests_beyond_bounded_retries(self):
        svc = self.service()
        svc.cycle()
        self.mock.requests.clear()
        self.mock.api_queue.extend([(503, {}, {})] * 50)
        svc.cycle()
        self.assertLessEqual(len(self.mock.hits("/wave/")), 3, "a dead server costs at most 3 tries of the first request")


class Lifecycle(WaveTestCase):
    def test_run_polls_and_shuts_down_cleanly(self):
        cfg = self.cfg()
        self.sign_in(cfg)
        svc = Service(cfg, stop=threading.Event())
        thread = threading.Thread(target=svc.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while time.time() < deadline and not self.mock.sinks["ntfy"]:
            time.sleep(0.05)
        self.assertTrue(self.mock.sinks["ntfy"], "the first poll ran and its alerts were delivered")
        svc.stop.set()
        thread.join(15)
        self.assertFalse(thread.is_alive(), "stops promptly when asked")
        wal = cfg.data_dir / "bwwatch.db-wal"
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0, "WAL is folded back into the database on a clean stop")
        conn = sqlite3.connect(str(cfg.data_dir / "bwwatch.db"))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
        status = json.loads((cfg.data_dir / "status.json").read_text())
        self.assertEqual(status["consecutive_failures"], 0)
        titles = [m["json"]["title"] for m in self.mock.sinks["ntfy"]]
        self.assertIn("bwwatch started", titles)
        self.assertIn("Watching Basement", titles)
        ok, message = healthcheck(cfg.data_dir)
        self.assertTrue(ok, message)

    def test_only_one_instance_may_run(self):
        cfg = self.cfg()
        lock = acquire_lock(cfg.data_dir / "bwwatch.lock")
        try:
            with self.assertRaises(AlreadyRunning):
                Service(cfg).run()
            with self.assertRaises(AlreadyRunning):
                acquire_lock(cfg.data_dir / "bwwatch.lock")
        finally:
            lock.close()
        acquire_lock(cfg.data_dir / "bwwatch.lock").close()  # released when the holder goes away

    def test_startup_warns_loudly_when_nothing_can_alert_you(self):
        svc = self.service(self.cfg(NTFY_TOPIC="", BW_FAULT_REQUEST=""))
        with self.assertLogs("bwwatch.service", "WARNING") as logs:
            svc.startup(None)
        text = "\n".join(logs.output)
        self.assertIn("NO NOTIFICATION CHANNEL IS CONFIGURED", text)
        self.assertIn("BW_FAULT_REQUEST is not set", text)

    def test_startup_notice_is_not_repeated_on_crash_loops(self):
        svc = self.service()
        svc.startup(None)
        svc.startup(None)
        notices = [r for r in self.outbox(svc) if r["title"] == "bwwatch started"]
        self.assertEqual(len(notices), 1)

    def test_heartbeat_pings_only_after_good_polls_and_never_leaks_the_url(self):
        svc = self.service(self.cfg(HEARTBEAT_URL=self.mock.url + "/heartbeat/my-secret-check-id"))
        svc.cycle()
        self.assertEqual(len(self.mock.sinks["heartbeat"]), 1)
        self.mock.token_override = (500, {}, {})
        svc.tokens.invalidate()
        svc.cycle()
        self.assertEqual(len(self.mock.sinks["heartbeat"]), 1, "no ping when the poll failed")
        self.mock.token_override = None
        self.mock.sink_status["heartbeat"] = 500
        with self.assertLogs("bwwatch.service", "WARNING") as logs:
            svc.ping_heartbeat()
        self.assertNotIn("my-secret-check-id", "\n".join(logs.output))

    def test_a_damaged_database_at_startup_is_recovered_and_you_are_told(self):
        svc = self.service()
        svc.cycle()
        db.backup_now(svc.conn, svc.cfg.data_dir / "backups", 3)
        svc.shutdown()
        (svc.cfg.data_dir / "bwwatch.db").write_bytes(b"garbage" * 1000)
        for suffix in ("-wal", "-shm"):
            try:
                (svc.cfg.data_dir / ("bwwatch.db" + suffix)).unlink()
            except FileNotFoundError:
                pass
        again = Service(svc.cfg)
        recovery = again.open()
        self.addCleanup(again.shutdown)
        self.assertIsNotNone(recovery)
        alert = [r for r in self.outbox(again) if r["title"].startswith("bwwatch database was damaged")]
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert[0]["priority"], 5)
        self.assertIn("Restored from backup", alert[0]["body"])
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0], 1)


class Maintenance(WaveTestCase):
    def test_backup_runs_when_due_and_not_otherwise(self):
        svc = self.service()
        svc.cycle()
        svc.maintenance()
        self.assertEqual(len(db.list_backups(svc.cfg.data_dir / "backups")), 1)
        svc.maintenance()
        self.assertEqual(len(db.list_backups(svc.cfg.data_dir / "backups")), 1, "not again within the interval")

    def test_failed_backup_alerts_once_a_day(self):
        svc = self.service()
        with mock.patch.object(service_module, "backup_now", side_effect=OSError("No space left on device")):
            svc.maintenance()
            svc.conn.execute("DELETE FROM meta WHERE key = 'maint.backup_at'")
            svc.maintenance()
        alerts = [r for r in self.outbox(svc) if r["title"] == "bwwatch backup problem"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("No space left", alerts[0]["body"])

    def test_integrity_problem_found_at_runtime_triggers_recovery(self):
        svc = self.service()
        svc.cycle()
        svc.conn.execute("DELETE FROM meta WHERE key = 'maint.integrity_at'")
        reopened = []
        real_open = service_module.open_database

        def spy(path):
            reopened.append(path)
            return real_open(path)

        with mock.patch.object(service_module, "integrity_check", return_value=["row 5 missing from index"]), \
                mock.patch.object(service_module, "open_database", spy):
            svc.maintenance()
        self.assertEqual(len(reopened), 1)
        self.assertTrue(svc.cycle().ok, "still working on the reopened connection")

    def test_database_write_failure_alerts_without_using_the_database(self):
        svc = self.service()
        error = sqlite3.OperationalError("database or disk is full")
        with mock.patch.object(service_module, "apply_cycle", side_effect=error):
            out = svc.cycle()
            svc.cycle()
        self.assertFalse(out.ok)
        alerts = [m["json"] for m in self.mock.sinks["ntfy"] if m["json"]["title"] == "bwwatch cannot write its database"]
        self.assertEqual(len(alerts), 1, "sent directly, and throttled to once an hour")
        self.assertEqual(alerts[0]["priority"], 5)


class Health(unittest.TestCase):
    def status(self, tmp: Path, **fields):
        base = {"interval": 3600, "updated_epoch": 10_000.0, "last_attempt_epoch": 9_900.0, "last_success_epoch": 9_900.0,
                "started_epoch": 9_000.0, "consecutive_failures": 0, "last_error": ""}
        base.update(fields)
        (tmp / "status.json").write_text(json.dumps(base))

    def test_healthcheck_decisions(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            self.assertFalse(healthcheck(tmp, now=10_000.0)[0], "no status file")
            self.status(tmp)
            self.assertTrue(healthcheck(tmp, now=10_050.0)[0])
            self.status(tmp, updated_epoch=1_000.0)
            self.assertIn("silent", healthcheck(tmp, now=10_050.0)[1])
            # the loop is alive (status refreshed a minute ago) but has not tried to poll for over 2 intervals
            self.status(tmp, last_attempt_epoch=1_000.0, updated_epoch=17_600.0)
            self.assertIn("no poll has been attempted", healthcheck(tmp, now=17_650.0)[1])
            self.status(tmp, last_success_epoch=1_000.0, last_error="sign-in rejected", updated_epoch=20_000.0, last_attempt_epoch=20_000.0)
            ok, message = healthcheck(tmp, now=20_050.0)
            self.assertFalse(ok)
            self.assertIn("sign-in rejected", message)
            (tmp / "status.json").write_text("{not json")
            self.assertFalse(healthcheck(tmp)[0])

    def test_never_succeeded_yet_is_measured_from_start(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            self.status(tmp, last_success_epoch=None, started_epoch=10_000.0, last_attempt_epoch=10_000.0)
            self.assertTrue(healthcheck(tmp, now=10_100.0)[0])


if __name__ == "__main__":
    unittest.main()
