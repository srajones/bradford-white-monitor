"""Delivering notifications: ntfy, Telegram, email, Home Assistant, or a generic webhook."""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

from .config import Config
from .util import iso, scrub, truncate
from .wave import basic_auth, http_request

log = logging.getLogger("bwwatch.notify")


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    priority: int = 3  # 1 (min) .. 5 (urgent), ntfy-style
    tags: Tuple[str, ...] = ()
    kind: str = "info"  # fault | health | recovered | cleared | setting | info
    data: Optional[Dict[str, Any]] = None  # structured extras: {"appliance": {...}, "fault": {...}}
    time: Optional[str] = None


class NotifyError(Exception):
    pass


def _check(status: int, what: str, text: str) -> None:
    if not 200 <= status < 300:
        raise NotifyError("%s answered HTTP %d: %s" % (what, status, scrub(text, 150)))


def structured(msg: Message) -> Dict[str, Any]:
    """The JSON body sent to Home Assistant and generic webhooks. The field names are a stable interface."""
    payload: Dict[str, Any] = {
        "source": "bwwatch",
        "event": msg.kind,
        "title": msg.title,
        "message": msg.body,
        "priority": msg.priority,
        "tags": list(msg.tags),
        "time": msg.time or iso(),
    }
    if msg.data:
        payload.update(msg.data)
    return payload


class Notifier:
    """Sends a message to every configured channel that wants it, and reports each result."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.timeout = cfg.http_timeout
        self._senders: Dict[str, Callable[[Message], None]] = {}
        self._events: Dict[str, Optional[FrozenSet[str]]] = {}
        if cfg.ntfy:
            self._add("ntfy", self._send_ntfy, cfg.ntfy.events)
        if cfg.telegram:
            self._add("telegram", self._send_telegram, cfg.telegram.events)
        if cfg.smtp:
            self._add("email", self._send_email, cfg.smtp.events)
        if cfg.webhook:
            self._add("webhook", self._send_webhook, cfg.webhook.events)
        if cfg.homeassistant:
            self._add("homeassistant", self._send_homeassistant, cfg.homeassistant.events)

    def _add(self, name: str, sender: Callable[[Message], None], events: Optional[FrozenSet[str]]) -> None:
        self._senders[name] = sender
        self._events[name] = events

    @property
    def channels(self) -> Tuple[str, ...]:
        return tuple(self._senders)

    def channels_for(self, kind: str) -> Tuple[str, ...]:
        """Channels that will receive an alert of this kind (after each channel's event filter)."""
        return tuple(n for n in self._senders if self._events[n] is None or kind in (self._events[n] or ()))

    def send(self, msg: Message) -> Dict[str, Optional[str]]:
        """Deliver to every channel that wants this kind.

        Returns ``{channel: None}`` for success or ``{channel: error text}`` for failure;
        channels whose event filter excludes the message are simply absent.
        """
        results: Dict[str, Optional[str]] = {}
        for name in self.channels_for(msg.kind):
            try:
                self._senders[name](msg)
                results[name] = None
            except Exception as exc:  # noqa: BLE001 - one broken channel must not block the others
                results[name] = scrub("%s: %s" % (type(exc).__name__, exc), 300)
        return results

    # --- channels -----------------------------------------------------------
    def _send_ntfy(self, msg: Message) -> None:
        cfg = self.cfg.ntfy
        assert cfg is not None
        headers = {}
        if cfg.token:
            headers["Authorization"] = "Bearer " + cfg.token
        elif cfg.user:
            headers["Authorization"] = basic_auth(cfg.user, cfg.password)
        payload = {
            "topic": cfg.topic,
            "title": truncate(msg.title, 250),
            "message": truncate(msg.body, 3500),
            "priority": max(1, min(5, msg.priority)),
            "tags": list(msg.tags),
        }
        resp = http_request("POST", cfg.url, headers=headers, json_body=payload, timeout=self.timeout, label="ntfy")
        _check(resp.status, "ntfy", resp.text())

    def _send_telegram(self, msg: Message) -> None:
        cfg = self.cfg.telegram
        assert cfg is not None
        payload = {
            "chat_id": cfg.chat_id,
            "text": truncate("%s\n\n%s" % (msg.title, msg.body), 4000),
            "disable_web_page_preview": True,
            "disable_notification": msg.priority < 3,
        }
        url = "%s/bot%s/sendMessage" % (cfg.api_base, cfg.token)  # the token is part of the URL: never show it
        resp = http_request("POST", url, json_body=payload, timeout=self.timeout, label="Telegram")
        _check(resp.status, "Telegram", resp.text())
        try:
            ok = resp.json().get("ok")
        except Exception:  # noqa: BLE001
            ok = None
        if ok is not True:
            raise NotifyError("Telegram did not accept the message: %s" % scrub(resp.text(), 150))

    def _send_homeassistant(self, msg: Message) -> None:
        cfg = self.cfg.homeassistant
        assert cfg is not None
        # The webhook id inside the URL is the only secret protecting the automation: never show it.
        resp = http_request("POST", cfg.url, json_body=structured(msg), timeout=self.timeout, label="Home Assistant webhook")
        _check(resp.status, "Home Assistant", resp.text())

    def _send_webhook(self, msg: Message) -> None:
        cfg = self.cfg.webhook
        assert cfg is not None
        if cfg.format == "slack":
            payload: Dict[str, Any] = {"text": truncate("*%s*\n%s" % (msg.title, msg.body), 3500)}
        elif cfg.format == "discord":
            payload = {"content": truncate("**%s**\n%s" % (msg.title, msg.body), 1900)}
        else:
            payload = structured(msg)
            payload["text"] = truncate("%s\n%s" % (msg.title, msg.body), 3500)
        resp = http_request("POST", cfg.url, json_body=payload, timeout=self.timeout, label="webhook")
        _check(resp.status, "webhook", resp.text())

    def _send_email(self, msg: Message) -> None:
        cfg = self.cfg.smtp
        assert cfg is not None
        mail = EmailMessage()
        mail["Subject"] = "[bwwatch] " + truncate(msg.title, 150)
        mail["From"] = cfg.sender
        mail["To"] = ", ".join(cfg.recipients)
        mail["X-Priority"] = {5: "1", 4: "2", 3: "3", 2: "4", 1: "5"}.get(msg.priority, "3")
        mail.set_content(msg.body)
        if cfg.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=self.timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=self.timeout)
        with server:
            if cfg.security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if cfg.user:
                server.login(cfg.user, cfg.password)
            server.send_message(mail)
