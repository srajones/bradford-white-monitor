from __future__ import annotations

import email
import email.policy
import unittest

from bwwatch.notify import Message, Notifier, structured

from .helpers import WaveTestCase
from .mock_wave import FakeSMTP

TG_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"
HA_ID = "super-secret-webhook-id-8f3a"
FAULT_DATA = {
    "appliance": {"name": "Basement", "mac": "AA:BB:CC:DD:EE:FF", "serial": "SN123"},
    "fault": {"id": 7, "code": "10", "description": "Heating element failure", "occurred_at": "2026-10-04T19:03:00Z",
              "detected_at": "2026-10-04T19:05:00Z", "kind": "event", "source": "fault_history"},
}


def fault_message() -> Message:
    return Message("Water heater fault 10 — Basement", "details", 4, ("rotating_light",), kind="fault", data=FAULT_DATA,
                   time="2026-10-04T19:05:00Z")


class Channels(WaveTestCase):
    def notifier(self, **extra: str) -> Notifier:
        return Notifier(self.cfg(**extra))

    def test_ntfy_json_publish(self):
        result = self.notifier().send(fault_message())
        self.assertEqual(result, {"ntfy": None})
        got = self.mock.sinks["ntfy"][0]["json"]
        self.assertEqual(got["topic"], "test-topic-long-enough-1234")
        self.assertEqual(got["title"], "Water heater fault 10 — Basement")  # non-ASCII is fine: JSON, not headers
        self.assertEqual((got["priority"], got["tags"]), (4, ["rotating_light"]))

    def test_ntfy_authentication(self):
        self.notifier(NTFY_TOKEN="tk_abc").send(fault_message())
        self.assertEqual(self.mock.sinks["ntfy"][0]["headers"]["Authorization"], "Bearer tk_abc")
        self.notifier(NTFY_USER="me", NTFY_PASSWORD="pw").send(fault_message())
        self.assertTrue(self.mock.sinks["ntfy"][1]["headers"]["Authorization"].startswith("Basic "))

    def test_telegram(self):
        n = self.notifier(TELEGRAM_BOT_TOKEN=TG_TOKEN, TELEGRAM_CHAT_ID="42", TELEGRAM_API_BASE=self.mock.url + "/telegram")
        self.assertEqual(n.send(fault_message())["telegram"], None)
        hit = self.mock.sinks["telegram"][0]
        self.assertEqual(hit["token"], TG_TOKEN)
        self.assertEqual(hit["json"]["chat_id"], "42")
        self.assertEqual(hit["json"]["text"], "Water heater fault 10 — Basement\n\ndetails")
        self.assertFalse(hit["json"]["disable_notification"])
        n.send(Message("info", "quiet", 2, kind="info"))
        self.assertTrue(self.mock.sinks["telegram"][1]["json"]["disable_notification"], "low priority is silent")

    def test_webhook_formats(self):
        for fmt, key in (("slack", "text"), ("discord", "content"), ("json", "title")):
            with self.subTest(fmt):
                self.mock.sinks["webhook"].clear()
                self.notifier(WEBHOOK_URL=self.mock.url + "/webhook", WEBHOOK_FORMAT=fmt, NTFY_TOPIC="").send(fault_message())
                payload = self.mock.sinks["webhook"][0]["json"]
                self.assertIn(key, payload)
        self.assertEqual(payload["event"], "fault")  # the generic json format carries the structured fields

    def test_home_assistant_payload_is_the_documented_interface(self):
        n = self.notifier(HA_WEBHOOK_URL=self.mock.url + "/ha/api/webhook/" + HA_ID, NTFY_TOPIC="")
        self.assertEqual(n.send(fault_message()), {"homeassistant": None})
        hit = self.mock.sinks["ha"][0]
        self.assertEqual(hit["webhook_id"], HA_ID)
        body = hit["json"]
        self.assertEqual(body["source"], "bwwatch")
        self.assertEqual(body["event"], "fault")
        self.assertEqual(body["priority"], 4)
        self.assertEqual(body["time"], "2026-10-04T19:05:00Z")
        self.assertEqual(body["appliance"]["name"], "Basement")
        self.assertEqual(body["fault"]["code"], "10")
        self.assertEqual(body["fault"]["occurred_at"], "2026-10-04T19:03:00Z")
        for key in ("title", "message", "tags"):
            self.assertIn(key, body)
        self.assertEqual(hit["headers"]["Content-Type"], "application/json")

    def test_non_fault_alerts_have_no_fault_block(self):
        body = structured(Message("t", "b", 3, kind="health"))
        self.assertEqual(body["event"], "health")
        self.assertNotIn("fault", body)

    def test_email(self):
        smtp = FakeSMTP().start()
        self.addCleanup(smtp.stop)
        n = self.notifier(SMTP_HOST="127.0.0.1", SMTP_PORT=str(smtp.port), SMTP_SECURITY="none", SMTP_TO="me@example.com",
                          SMTP_FROM="bwwatch@example.com", NTFY_TOPIC="")
        self.assertEqual(n.send(fault_message()), {"email": None})
        parsed = email.message_from_string(smtp.messages[0], policy=email.policy.default)
        self.assertEqual(parsed["Subject"], "[bwwatch] Water heater fault 10 — Basement")
        self.assertEqual(parsed["To"], "me@example.com")
        self.assertEqual(parsed["From"], "bwwatch@example.com")
        self.assertEqual(parsed["X-Priority"], "2")  # priority 4 = high
        self.assertEqual(parsed.get_content().strip(), "details")

    def test_event_filters(self):
        n = self.notifier(HA_WEBHOOK_URL=self.mock.url + "/ha/api/webhook/" + HA_ID, HA_WEBHOOK_EVENTS="fault,health", NTFY_EVENTS="fault")
        self.assertEqual(n.channels_for("fault"), ("ntfy", "homeassistant"))
        self.assertEqual(n.channels_for("health"), ("homeassistant",))
        self.assertEqual(n.channels_for("info"), ())
        self.assertEqual(n.send(Message("hi", "there", 3, kind="info")), {}, "nobody wants it: nothing is sent")
        self.assertEqual(self.mock.sinks["ntfy"] + self.mock.sinks["ha"], [])
        n.send(Message("down", "x", 4, kind="health"))
        self.assertEqual((len(self.mock.sinks["ntfy"]), len(self.mock.sinks["ha"])), (0, 1))

    def test_one_failing_channel_does_not_block_the_others(self):
        self.mock.sink_status["ntfy"] = 500
        n = self.notifier(HA_WEBHOOK_URL=self.mock.url + "/ha/api/webhook/" + HA_ID)
        result = n.send(fault_message())
        self.assertIn("HTTP 500", result["ntfy"])
        self.assertIsNone(result["homeassistant"])
        self.assertEqual(len(self.mock.sinks["ha"]), 1)

    def test_secret_urls_never_appear_in_errors(self):
        n = self.notifier(
            HA_WEBHOOK_URL="http://127.0.0.1:9/ha/api/webhook/" + HA_ID,  # nothing listens there -> network error
            TELEGRAM_BOT_TOKEN=TG_TOKEN, TELEGRAM_CHAT_ID="42", TELEGRAM_API_BASE="http://127.0.0.1:9/telegram",
            NTFY_TOPIC="",
        )
        result = n.send(fault_message())
        self.assertTrue(result["homeassistant"] and result["telegram"], result)
        for error in result.values():
            self.assertNotIn(HA_ID, error)
            self.assertNotIn(TG_TOKEN, error)
            self.assertNotIn("AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1", error)

    def test_http_error_bodies_are_scrubbed(self):
        self.mock.sink_status["ha"] = 500
        n = self.notifier(HA_WEBHOOK_URL=self.mock.url + "/ha/api/webhook/" + HA_ID, NTFY_TOPIC="")
        error = n.send(fault_message())["homeassistant"]
        self.assertNotIn(HA_ID, error)


if __name__ == "__main__":
    unittest.main()
