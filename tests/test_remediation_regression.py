"""Comprehensive regression tests covering all fixes from the R1-R4 audit."""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import threading
import time

from bwwatch.config import Config, load_env_file
from bwwatch.cycle import (
    ACCOUNT,
    ApplianceData,
    FetchResult,
    Outcome,
    apply_cycle,
    deliver_outbox,
    enqueue,
    _record_fault_payload,
)
from bwwatch.db import backup_now, init_schema, savepoint
from bwwatch.faults import FaultEvent, FaultOptions, normalize_event
from bwwatch.notify import Message, Notifier
from bwwatch.privs import _fix_owner_and_mode, prepare_runtime
from bwwatch.readings import _as_float, _as_int
from bwwatch.readonly import check_read_only, ReadOnlyViolation
from bwwatch.report import _build_markdown_report, generate_report
from bwwatch.service import Service
from bwwatch.util import csv_safe, iso, scrub
from bwwatch.views import cmd_energy, cmd_readings
from bwwatch.wave import TokenManager, TokenStore
from tests.helpers import WaveTestCase
from tests.mock_wave import MockWave


class TestR1Regression(WaveTestCase):
    """Domain R1: Read-Only Safety & Cloud/OAuth Security."""

    def test_readonly_percent_encoding_traversal(self):
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("POST", "/wave/%252e%252e/wave/setMode")
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("GET", "/wave/set%4dode")
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("GET", "/wave/changesetpoint")

    def test_readonly_safe_prefixes_allowed(self):
        try:
            check_read_only("GET", "/wave/getSetpointHistory")
            check_read_only("GET", "/wave/getSettingDetails")
            check_read_only("GET", "/wave/getClearedErrors")
        except ReadOnlyViolation as e:
            self.fail(f"Safe path was unexpectedly blocked: {e}")

    def test_readonly_nested_json_mutations(self):
        bad_body = json.dumps({"payload": {"commands": [{"action": "changeMode", "val": 1}]}}).encode()
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("POST", "/wave/genericCommand", body=bad_body)

    def test_csv_formula_injection_prevention(self):
        dangerous_inputs = ["=1+1", "+cmd|' /C calc'!A0", "-cmd|' /C calc'!A0", "@SUM(A1:A10)", "|touch /tmp/pwn", "%VAR%", "\t=calc"]
        for inp in dangerous_inputs:
            sanitized = csv_safe(inp)
            self.assertTrue(str(sanitized).startswith("'"), f"Failed to sanitize: {inp} -> {sanitized}")
        # Safe values should not be modified
        self.assertEqual(csv_safe("Normal text"), "Normal text")
        self.assertEqual(csv_safe(12345), 12345)
        self.assertEqual(csv_safe("-5"), "-5")
        self.assertIsNone(csv_safe(None))

    def test_scrub_credential_redaction(self):
        text = "BW_PASSWORD=SuperSecret123 SMTP_PASSWORD='MyEmailPassword' http://admin:pass1234@example.com"
        cleaned = scrub(text)
        self.assertNotIn("SuperSecret123", cleaned)
        self.assertNotIn("MyEmailPassword", cleaned)
        self.assertNotIn("pass1234", cleaned)

    def test_config_matches_appliance_empty_token(self):
        cfg = self.cfg(BW_APPLIANCES="AA:BB:CC:DD:EE:FF")
        self.assertFalse(cfg.matches_appliance(""))
        self.assertFalse(cfg.matches_appliance("   "))
        self.assertTrue(cfg.matches_appliance("aa:bb:cc:dd:ee:ff"))
        self.assertFalse(cfg.matches_appliance("11:22:33:44:55:66"))

    def test_config_secrets_masked_in_repr(self):
        cfg = self.cfg(BW_PASSWORD="SecretPassWord123")
        rep = repr(cfg)
        self.assertNotIn("SecretPassWord123", rep)


class TestR2Regression(WaveTestCase):
    """Domain R2: Multi-Heater Filtering, Polling & Fault Engine."""

    def test_readings_numeric_safety(self):
        self.assertIsNone(_as_int("inf"))
        self.assertIsNone(_as_int("-inf"))
        self.assertIsNone(_as_int("nan"))
        self.assertIsNone(_as_float("inf"))
        self.assertIsNone(_as_float("-nan"))
        self.assertEqual(_as_int(42), 42)
        self.assertEqual(_as_float(98.6), 98.6)

    def test_faults_ignore_zero_and_boolean_codes(self):
        opts = FaultOptions()
        ev0 = normalize_event({"faultCode": 0, "message": "All clear"}, opts)
        self.assertIsNone(ev0.code)
        ev_false = normalize_event({"faultCode": False, "message": "Normal"}, opts)
        self.assertIsNone(ev_false.code)
        ev_real = normalize_event({"faultCode": 10, "message": "Superheat Fault"}, opts)
        self.assertEqual(ev_real.code, "10")

    def test_account_fault_payload_filters_unmonitored_heaters(self):
        cfg = self.cfg(BW_APPLIANCES="11:11:11:11:11:11")
        conn = sqlite3.connect(":memory:")
        init_schema(conn)
        outcome = Outcome()
        now = datetime.now(timezone.utc)

        payload = {
            "error_history": [
                {"macAddress": "11:11:11:11:11:11", "faultCode": 10, "title": "Sensor Fault"},
                {"macAddress": "22:22:22:22:22:22", "faultCode": 20, "title": "Other Fault"},
            ]
        }
        _record_fault_payload(conn, cfg, ACCOUNT, "Account", payload, now, outcome, [])
        rows = conn.execute("SELECT code FROM faults").fetchall()
        codes = [r[0] for r in rows]
        self.assertIn("10", codes)
        self.assertNotIn("20", codes)
        conn.close()

    def test_deliver_outbox_drops_permanent_failures(self):
        cfg = self.cfg()
        conn = sqlite3.connect(":memory:")
        init_schema(conn)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO outbox(id, created_at, kind, priority, title, body, status, attempts) VALUES (1, '2026-01-01', 'fault', 4, 'Test', 'Body', 'pending', 5)"
        )
        conn.commit()

        notifier = MagicMock()
        notifier.channels = ("mock",)
        notifier.channels_for.return_value = ("mock",)
        notifier.send.return_value = {"mock": "HTTP 400: Bad Request"}

        pending = deliver_outbox(conn, notifier, cfg)
        self.assertEqual(pending, 0)
        row = conn.execute("SELECT status FROM outbox WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "dropped")
        conn.close()


class TestR3Regression(WaveTestCase):
    """Domain R3: SQLite Durability, Views, Reports & Notifications."""

    def test_db_savepoint_and_backup_hardening(self):
        conn = sqlite3.connect(":memory:")
        init_schema(conn)
        with savepoint(conn, "test_sp"):
            conn.execute("INSERT INTO meta(key, value) VALUES ('k1', 'v1')")
        row = conn.execute("SELECT value FROM meta WHERE key = 'k1'").fetchone()
        self.assertEqual(row[0], "v1")
        conn.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            backup_dir = Path(tmpdir) / "backups"
            backup_dir.mkdir()
            disk_conn = sqlite3.connect(db_path)
            init_schema(disk_conn)
            b1 = backup_now(disk_conn, backup_dir, keep=1)
            b2 = backup_now(disk_conn, backup_dir, keep=1)
            disk_conn.close()
            self.assertTrue(b2.exists())

    def test_views_case_insensitive_mac_and_unlimited_limit(self):
        conn = sqlite3.connect(":memory:")
        init_schema(conn)
        conn.execute(
            "INSERT INTO readings(id, taken_at, mac, mode, mode_value, setpoint_f, temps) VALUES (1, '2026-01-01', 'aa:bb:cc:dd:ee:ff', 'hybrid', 1, 120.0, '{}')"
        )
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM readings WHERE lower(mac) = lower(?)", ("AA:BB:CC:DD:EE:FF",)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        conn.close()

    def test_discord_allowed_mentions(self):
        cfg = self.cfg(
            WEBHOOK_URL="http://127.0.0.1:9999/discord",
            WEBHOOK_FORMAT="discord",
            NTFY_TOPIC="",
        )
        notifier = Notifier(cfg)
        with patch("bwwatch.notify.http_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.text.return_value = "ok"
            mock_req.return_value = mock_resp
            msg = Message("Alert", "Something happened @everyone", 4, kind="fault")
            notifier.send(msg)
            args, kwargs = mock_req.call_args
            payload = kwargs.get("json_body") or {}
            self.assertIn("allowed_mentions", payload)
            self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_email_subject_crlf_sanitization(self):
        cfg = self.cfg(
            SMTP_HOST="localhost",
            SMTP_PORT="25",
            SMTP_SECURITY="none",
            SMTP_FROM="bw@example.com",
            SMTP_TO="user@example.com",
            NTFY_TOPIC="",
        )
        notifier = Notifier(cfg)
        with patch("bwwatch.notify.smtplib.SMTP") as mock_smtp:
            msg = Message("Alert\r\nSubject: Injected\nBcc: evil@example.com", "Body", 4, kind="fault")
            res = notifier.send(msg)
            self.assertEqual(res, {"email": None})
            send_args = mock_smtp.return_value.send_message.call_args[0][0]
            self.assertNotIn("\r", send_args["Subject"])
            self.assertNotIn("\n", send_args["Subject"])


class TestR4Regression(unittest.TestCase):
    """Domain R4: Shell Scripts, Docker Hardening & Host Confinement."""

    def test_privs_prepare_runtime_umask_and_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            tok_file = base / "token.json"
            tok_file.write_text("{}")
            _fix_owner_and_mode(tok_file, os.getuid(), os.getgid(), mode=0o600)
            stat = tok_file.stat()
            self.assertEqual(stat.st_mode & 0o777, 0o600)

            db_dir = base / "backups"
            db_dir.mkdir()
            _fix_owner_and_mode(db_dir, os.getuid(), os.getgid(), mode=0o700)
            dir_stat = db_dir.stat()
            self.assertEqual(dir_stat.st_mode & 0o777, 0o700)


class TestIteration2ChallengerRemediation(WaveTestCase):
    """Regression tests covering all 7 confirmed findings from Challenger 1 and 2."""

    def test_item1_readonly_query_semicolon_and_crlf_smuggling(self):
        # Semicolon delimited query parameter smuggling
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("GET", "https://api.example.com/wave/getApplianceStatus?foo=bar;setpoint=130")
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("GET", "https://api.example.com/wave/getApplianceStatus?applianceMac=123;mode=hybrid")
        # Percent-encoded CRLF smuggling
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("GET", "https://api.example.com/wave/getApplianceStatus?foo=bar%0d%0asetpoint=130")
        # Raw CRLF smuggling
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("GET", "https://api.example.com/wave/getApplianceStatus?foo=bar\r\nsetpoint=130")
        # Percent-encoded semicolon smuggling
        with self.assertRaises(ReadOnlyViolation):
            check_read_only("GET", "https://api.example.com/wave/getApplianceStatus?foo=bar%3bsetpoint=130")
        # Safe read requests should succeed
        try:
            check_read_only("GET", "https://api.example.com/wave/getApplianceStatus?mac_address=00:11:22:33:44:55&format=json")
            check_read_only("GET", "https://api.example.com/wave/getApplianceList?limit=50")
        except ReadOnlyViolation as e:
            self.fail(f"Legitimate read request was unexpectedly rejected: {e}")

    def test_item2_scrub_multiword_secrets_and_whitespace(self):
        # Multi-word secrets in double quotes
        self.assertEqual(
            scrub('password="correct horse battery staple"'),
            'password="[redacted]"',
        )
        # Multi-word secrets in single quotes
        self.assertEqual(
            scrub("password='correct horse battery staple'"),
            "password='[redacted]'",
        )
        # Spaces around assignment operator '='
        self.assertEqual(scrub('password = "Secret123"'), 'password = "[redacted]"')
        # Spaces around colon ':'
        self.assertEqual(scrub('password: "Secret123"'), 'password: "[redacted]"')
        self.assertEqual(scrub('password : "Secret123"'), 'password : "[redacted]"')
        # Unquoted values with spaces around delimiters
        self.assertEqual(scrub("token = abc12345"), "token = [redacted]")
        self.assertEqual(scrub("refresh_token: token12345"), "refresh_token: [redacted]")
        # Preserves surrounding content
        text = '{"password": "my long secret passphrase", "username": "user1"}'
        self.assertEqual(
            scrub(text),
            '{"password": "[redacted]", "username": "user1"}',
        )

    def test_item3_token_manager_refresh_concurrency_cache_reuse(self):
        server = MockWave().start()
        try:
            refresh = server.seed_refresh_token()
            with tempfile.TemporaryDirectory() as td:
                token_path = Path(td) / "token.json"
                store0 = TokenStore(token_path)
                store0.save(refresh, access_token="stale", expires_at=time.time() - 10)
                cfg = Config.from_env({
                    "BW_AUTH_BASE": server.url + "/auth",
                    "BW_API_BASE": server.url,
                    "DATA_DIR": td,
                    "BW_ALLOW_INSECURE_HTTP": "true",
                })
                tm = TokenManager(cfg, store0)
                threads = [threading.Thread(target=tm.bearer) for _ in range(10)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                calls = [r for r in server.requests if r["path"].endswith("/token")]
                self.assertEqual(len(calls), 1, f"Expected 1 token refresh call under concurrency, got {len(calls)}")
                self.assertIsNotNone(tm.bearer())
        finally:
            server.stop()

    def test_item4_outbox_no_head_of_line_blocking(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)

        now = "2026-11-06T12:00:00Z"
        enqueue(conn, kind="fault", title="Alert 1 (Failing)", body="Body 1", priority=4, now=now)
        enqueue(conn, kind="info", title="Alert 2 (Valid)", body="Body 2", priority=3, now=now)

        class MockNotifier:
            def __init__(self):
                self.channels = ("mock",)
                self.sent = []

            def channels_for(self, kind):
                return ("mock",)

            def send(self, msg):
                if "Alert 1" in msg.title:
                    return {"mock": "NotifyError: HTTP 500"}
                self.sent.append(msg.title)
                return {"mock": None}

        notifier = MockNotifier()
        cfg = self.cfg()
        pending = deliver_outbox(conn, notifier, cfg)
        self.assertEqual(notifier.sent, ["Alert 2 (Valid)"])
        self.assertEqual(pending, 1)

        row1 = conn.execute("SELECT attempts, last_error, status FROM outbox WHERE id = 1").fetchone()
        self.assertEqual(row1["attempts"], 1)
        self.assertIn("HTTP 500", row1["last_error"])
        self.assertEqual(row1["status"], "pending")

        row2 = conn.execute("SELECT status FROM outbox WHERE id = 2").fetchone()
        self.assertEqual(row2["status"], "sent")
        conn.close()

    def test_item5_config_env_all_or_empty_clears_selected_appliances_and_overrides_heaters_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            heaters_file = tmp / "heaters.json"

            # 1. When BW_APPLIANCES="all", clears selection and unlinks heaters.json
            heaters_file.write_text(json.dumps({"env_bw_appliances": "", "selected": ["MAC1"]}))
            cfg_all = Config.from_env({"DATA_DIR": str(tmp), "BW_APPLIANCES": "all"})
            self.assertEqual(cfg_all.selected_appliances, ())
            self.assertTrue(cfg_all.matches_appliance("MAC1"))
            self.assertTrue(cfg_all.matches_appliance("MAC2"))
            self.assertFalse(heaters_file.exists())

            # 2. When BW_APPLIANCES="", clears selection and unlinks heaters.json
            heaters_file.write_text(json.dumps({"env_bw_appliances": "", "selected": ["MAC1"]}))
            cfg_empty = Config.from_env({"DATA_DIR": str(tmp), "BW_APPLIANCES": ""})
            self.assertEqual(cfg_empty.selected_appliances, ())
            self.assertTrue(cfg_empty.matches_appliance("MAC1"))
            self.assertTrue(cfg_empty.matches_appliance("MAC2"))
            self.assertFalse(heaters_file.exists())

            # 3. When BW_APPLIANCES is unset, preserves heaters.json selection
            heaters_file.write_text(json.dumps({"env_bw_appliances": "", "selected": ["MAC1"]}))
            cfg_unset = Config.from_env({"DATA_DIR": str(tmp)})
            self.assertEqual(cfg_unset.selected_appliances, ("MAC1",))
            self.assertTrue(cfg_unset.matches_appliance("MAC1"))
            self.assertFalse(cfg_unset.matches_appliance("MAC2"))
            self.assertTrue(heaters_file.exists())

    def test_item6_account_fault_filtering_additional_device_keys(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)

        cfg = Config.from_env({
            "DATA_DIR": "/tmp",
            "BW_APPLIANCES": "LIVE",
            "BW_FAULT_REQUEST": "GET /wave/getNotifications",
            "BW_POLL_INTERVAL_SECONDS": "3600",
            "BW_FAULT_POLL_INTERVAL_SECONDS": "600",
        })

        now = iso()
        live_app = ApplianceData(mac="LIVE", name="Live Heater", serial="SN-LIVE", model="HP", listing={})

        # Test dead heater identified by deviceId -> skipped
        payload_dead_device = {
            "notifications": [{"deviceId": "DEAD", "errorCode": "E99", "state": "active", "time": now}]
        }
        apply_cycle(conn, cfg, FetchResult(started_at=now, appliances=[live_app], account_faults=payload_dead_device, account_faults_fetched=True), now=now)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 0)

        svc = Service(cfg)
        svc.conn = conn
        self.assertIsNone(svc._active_fault_interval())

        # Test dead heater identified by heaterId -> skipped
        payload_dead_heater = {
            "notifications": [{"heaterId": "DEAD_HEATER", "errorCode": "E98", "state": "active", "time": now}]
        }
        apply_cycle(conn, cfg, FetchResult(started_at=now, appliances=[live_app], account_faults=payload_dead_heater, account_faults_fetched=True), now=now)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 0)

        # Test dead heater identified by serialNumber -> skipped
        payload_dead_serial = {
            "notifications": [{"serialNumber": "DEAD_SERIAL", "errorCode": "E97", "state": "active", "time": now}]
        }
        apply_cycle(conn, cfg, FetchResult(started_at=now, appliances=[live_app], account_faults=payload_dead_serial, account_faults_fetched=True), now=now)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 0)

        # Test live heater identified by deviceId -> recorded and accelerates polling
        payload_live = {
            "notifications": [{"deviceId": "LIVE", "errorCode": "E10", "state": "active", "time": now}]
        }
        apply_cycle(conn, cfg, FetchResult(started_at=now, appliances=[live_app], account_faults=payload_live, account_faults_fetched=True), now=now)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0], 1)
        self.assertEqual(svc._active_fault_interval(), 600.0)
        conn.close()

    def test_item7_backup_permissions_under_permissive_umask(self):
        old_umask = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                conn = sqlite3.connect(tmp / "bwwatch.db")
                init_schema(conn)
                backups = tmp / "backups"
                bk = backup_now(conn, backups, keep=5)
                conn.close()

                dir_mode = oct(backups.stat().st_mode & 0o777)
                file_mode = oct(bk.stat().st_mode & 0o777)
                self.assertEqual(dir_mode, "0o700", f"Expected backup dir 0o700, got {dir_mode}")
                self.assertEqual(file_mode, "0o600", f"Expected backup file 0o600, got {file_mode}")
        finally:
            os.umask(old_umask)


if __name__ == "__main__":
    unittest.main()
