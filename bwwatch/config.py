"""Configuration: everything comes from environment variables (the .env file)."""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Tuple

from .errors import ConfigError, ReadOnlyViolation
from .readonly import check_read_only
from .util import norm_key

__all__ = ["Config", "ConfigError", "FaultOptions", "RequestSpec", "norm_key"]

DEFAULT_API_BASE = "https://gw.prdapi.bradfordwhiteapps.com"
DEFAULT_AUTH_BASE = (
    "https://consumer.bradfordwhiteapps.com/consumer.bradfordwhiteapps.com/"
    "B2C_1_Wave_SignIn/oauth2/v2.0"
)
DEFAULT_CLIENT_ID = "7899415d-1c23-46d8-8a79-4c15ed5f7f22"
DEFAULT_REDIRECT_URI = "com.bradfordwhiteapps.bwconnect://oauth/redirect"
DEFAULT_SCOPE = "openid email offline_access profile"
# The Wave app is a Flutter app; the community client sends the same value.
DEFAULT_USER_AGENT = "Dart/3.8 (dart:io)"
DEFAULT_LIST_REQUEST = "GET /wave/getApplianceList?username={account_id}"
DEFAULT_STATUS_REQUEST = "GET /wave/getApplianceStatus?macAddress={mac}"

# Keys whose value changes on every call and must not count as a "change".
DEFAULT_VOLATILE_KEYS = (
    "requestId,traceId,correlationId,serverTime,responseTime,"
    "read,isRead,unread,seen,viewed,acknowledged,ack,age,ago,relativeTime,elapsed"
)
# Config-ish flags that merely *mention* faults/alerts (not an active fault).
DEFAULT_SCAN_IGNORE = r"(?i)enabled|support|capab|setting|prefer|subscri|threshold|logging|configur"


# --- request specs ----------------------------------------------------------
KNOWN_PLACEHOLDERS = frozenset({"account_id", "mac", "serial", "name"})
APPLIANCE_PLACEHOLDERS = frozenset({"mac", "serial", "name"})
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def _walk(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_walk(item, fn) for item in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    return obj


@dataclass(frozen=True)
class RequestSpec:
    """``METHOD path-or-url [json body]`` with ``{account_id} {mac} {serial} {name}`` placeholders."""

    method: str
    target: str
    body: Any = None

    @classmethod
    def parse(cls, text: str, *, label: str) -> "RequestSpec":
        """Parse and validate. Anything that could change the heater is refused (see readonly.py)."""
        raw = (text or "").strip()
        if not raw:
            raise ConfigError("%s is empty" % label)
        first, *rest = raw.split(None, 1)
        if first.upper() in ("GET", "POST"):
            method, remainder = first.upper(), (rest[0] if rest else "")
        elif first.upper() in ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            raise ConfigError("%s: only GET and POST are supported (got %s)" % (label, first))
        else:
            method, remainder = "GET", raw
        pieces = remainder.split(None, 1)
        if not pieces:
            raise ConfigError("%s: missing the path or URL after %s" % (label, method))
        target = pieces[0]
        body_text = pieces[1].strip() if len(pieces) > 1 else ""
        if not (target.startswith("/") or target.lower().startswith(("https://", "http://"))):
            raise ConfigError(
                "%s: expected a path starting with '/' (or a full https:// URL), got %r" % (label, target)
            )
        body: Any = None
        if body_text:
            if method == "GET":
                raise ConfigError("%s: a GET request cannot have a body" % label)
            try:
                body = json.loads(body_text)
            except ValueError as exc:
                raise ConfigError("%s: the body after the URL must be valid JSON (%s)" % (label, exc))
        try:
            check_read_only(method, target, body)
        except ReadOnlyViolation as exc:
            raise ConfigError("%s: %s" % (label, exc))
        spec = cls(method, target, body)
        unknown = spec.placeholders() - KNOWN_PLACEHOLDERS
        if unknown:
            raise ConfigError(
                "%s: unknown placeholder(s) %s; use only {account_id} {mac} {serial} {name}"
                % (label, ", ".join("{%s}" % u for u in sorted(unknown)))
            )
        return spec

    def placeholders(self) -> FrozenSet[str]:
        text = self.target + (json.dumps(self.body) if self.body is not None else "")
        return frozenset(_PLACEHOLDER_RE.findall(text))

    @property
    def per_appliance(self) -> bool:
        """True if the request must be repeated for each heater."""
        return bool(self.placeholders() & APPLIANCE_PLACEHOLDERS)

    def render(self, ctx: Mapping[str, str]) -> Tuple[str, Any]:
        """Return ``(target, body)`` with placeholders filled in (URL-quoted in the target)."""
        missing = [p for p in self.placeholders() if p not in ctx or ctx[p] in (None, "")]
        if missing:
            raise KeyError(", ".join(sorted(missing)))
        target = _PLACEHOLDER_RE.sub(lambda m: urllib.parse.quote(str(ctx[m.group(1)]), safe=""), self.target)
        body = _walk(self.body, lambda s: _PLACEHOLDER_RE.sub(lambda m: str(ctx[m.group(1)]), s))
        return target, body

    def describe(self) -> str:
        return "%s %s%s" % (self.method, self.target, " " + json.dumps(self.body) if self.body is not None else "")


# --- notification channel settings -----------------------------------------
# What kinds of alert exist. A channel may be limited to some of them (<CHANNEL>_EVENTS).
VALID_EVENTS = frozenset({"fault", "health", "recovered", "cleared", "setting", "info"})
# Never poll the Wave cloud more often than this, whatever the settings say.
MIN_POLL_SECONDS = 300


@dataclass(frozen=True)
class NtfyConfig:
    url: str
    topic: str
    token: str = ""
    user: str = ""
    password: str = ""
    events: Optional[FrozenSet[str]] = None


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str
    api_base: str = "https://api.telegram.org"
    events: Optional[FrozenSet[str]] = None


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    security: str  # starttls | ssl | none
    user: str
    password: str
    sender: str
    recipients: Tuple[str, ...]
    events: Optional[FrozenSet[str]] = None


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    format: str  # json | slack | discord
    events: Optional[FrozenSet[str]] = None


@dataclass(frozen=True)
class HomeAssistantConfig:
    """A Home Assistant automation webhook: POST {url} with a JSON body (see README)."""

    url: str
    events: Optional[FrozenSet[str]] = None


@dataclass(frozen=True)
class FaultOptions:
    """Knobs for reading the (unknown-shape) fault/notification response."""

    list_path: str = ""
    code_field: str = ""
    time_field: str = ""
    text_field: str = ""
    id_fields: Tuple[str, ...] = ()
    match: Optional["re.Pattern[str]"] = None
    volatile: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class Config:
    data_dir: Path
    interval: int
    api_base: str
    auth_base: str
    client_id: str
    redirect_uri: str
    scope: str
    user_agent: str
    http_timeout: float
    extra_headers: Dict[str, str]
    account_id: str
    seed_refresh_token: str
    allow_insecure_http: bool
    list_request: RequestSpec
    status_request: Optional[RequestSpec]
    fault_request: Optional[RequestSpec]
    fault_options: FaultOptions
    scan_status: bool
    scan_ignore: Optional["re.Pattern[str]"]
    # alerting
    fault_priority: int
    notify_retry_seconds: int
    max_alerts_per_cycle: int
    notify_status_changes: bool
    notify_cleared: bool
    health_after_failures: int
    health_repeat_hours: int
    heartbeat_url: str
    display_tz: str
    # storage
    backup_every_hours: int
    backup_keep: int
    # channels
    ntfy: Optional[NtfyConfig]
    telegram: Optional[TelegramConfig]
    smtp: Optional[SmtpConfig]
    webhook: Optional[WebhookConfig]
    homeassistant: Optional[HomeAssistantConfig] = None
    warnings: Tuple[str, ...] = field(default=())

    @property
    def token_url(self) -> str:
        return self.auth_base.rstrip("/") + "/token"

    @property
    def authorize_url(self) -> str:
        return self.auth_base.rstrip("/") + "/authorize"

    @property
    def channel_names(self) -> Tuple[str, ...]:
        names = []
        if self.ntfy:
            names.append("ntfy")
        if self.telegram:
            names.append("telegram")
        if self.smtp:
            names.append("email")
        if self.webhook:
            names.append("webhook")
        if self.homeassistant:
            names.append("homeassistant")
        return tuple(names)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        return _build(env)


# --- parsing helpers --------------------------------------------------------
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _get(env: Mapping[str, str], name: str, default: str = "") -> str:
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name, "").lower()
    if raw == "":
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError("%s must be true/false (got %r)" % (name, raw))


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = _get(env, name, "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("%s must be a whole number (got %r)" % (name, raw))
    if not lo <= value <= hi:
        raise ConfigError("%s must be between %d and %d (got %d)" % (name, lo, hi, value))
    return value


def _float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    raw = _get(env, name, "")
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("%s must be a number (got %r)" % (name, raw))
    if not lo <= value <= hi:
        raise ConfigError("%s must be between %s and %s (got %s)" % (name, lo, hi, value))
    return value


def _csv(raw: str) -> Tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _regex(env: Mapping[str, str], name: str, default: str = "") -> Optional["re.Pattern[str]"]:
    raw = _get(env, name, default)
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as exc:
        raise ConfigError("%s is not a valid regular expression: %s" % (name, exc))


def _events(env: Mapping[str, str], name: str) -> Optional[FrozenSet[str]]:
    """``fault,health`` -> {'fault','health'}; unset/blank -> None (the channel gets every kind)."""
    chosen = frozenset(part.lower() for part in _csv(_get(env, name)))
    if not chosen:
        return None
    unknown = chosen - VALID_EVENTS
    if unknown:
        raise ConfigError(
            "%s: unknown event kind(s) %s; choose from %s" % (name, ", ".join(sorted(unknown)), ", ".join(sorted(VALID_EVENTS)))
        )
    return chosen


def _url(env: Mapping[str, str], name: str, default: str, *, insecure_ok: bool, required_https: bool = True) -> str:
    value = _get(env, name, default).rstrip("/")
    lower = value.lower()
    if lower.startswith("https://"):
        return value
    if lower.startswith("http://") and (insecure_ok or not required_https):
        return value
    if required_https:
        raise ConfigError("%s must start with https:// (got %r)" % (name, value))
    raise ConfigError("%s must start with http:// or https:// (got %r)" % (name, value))


def _build(env: Mapping[str, str]) -> Config:
    warnings = []
    insecure = _bool(env, "BW_ALLOW_INSECURE_HTTP", False)
    if insecure:
        warnings.append("BW_ALLOW_INSECURE_HTTP is on: tokens may be sent over plain HTTP. Use only for testing.")

    api_base = _url(env, "BW_API_BASE", DEFAULT_API_BASE, insecure_ok=insecure)
    auth_base = _url(env, "BW_AUTH_BASE", DEFAULT_AUTH_BASE, insecure_ok=insecure)

    extra_headers: Dict[str, str] = {}
    raw_headers = _get(env, "BW_EXTRA_HEADERS", "")
    if raw_headers:
        try:
            parsed = json.loads(raw_headers)
        except ValueError as exc:
            raise ConfigError("BW_EXTRA_HEADERS must be a JSON object like {\"X-Header\": \"value\"} (%s)" % exc)
        if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
            raise ConfigError("BW_EXTRA_HEADERS must be a JSON object of string keys and string values")
        extra_headers = dict(parsed)

    list_request = RequestSpec.parse(_get(env, "BW_LIST_REQUEST", DEFAULT_LIST_REQUEST), label="BW_LIST_REQUEST")
    # Unset -> default request; set but empty -> status polling disabled.
    status_text = _get(env, "BW_STATUS_REQUEST") if "BW_STATUS_REQUEST" in env else DEFAULT_STATUS_REQUEST
    status_request = RequestSpec.parse(status_text, label="BW_STATUS_REQUEST") if status_text else None
    fault_text = _get(env, "BW_FAULT_REQUEST", "")
    fault_request = RequestSpec.parse(fault_text, label="BW_FAULT_REQUEST") if fault_text else None

    volatile = frozenset(
        norm_key(k) for k in _csv(_get(env, "BW_VOLATILE_KEYS", DEFAULT_VOLATILE_KEYS))
    )
    fault_options = FaultOptions(
        list_path=_get(env, "BW_FAULT_LIST_PATH"),
        code_field=_get(env, "BW_FAULT_CODE_FIELD"),
        time_field=_get(env, "BW_FAULT_TIME_FIELD"),
        text_field=_get(env, "BW_FAULT_TEXT_FIELD"),
        id_fields=_csv(_get(env, "BW_FAULT_ID_FIELDS")),
        match=_regex(env, "BW_FAULT_MATCH"),
        volatile=volatile,
    )

    # --- channels ---
    ntfy = None
    topic = _get(env, "NTFY_TOPIC")
    if topic:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise ConfigError("NTFY_TOPIC may only contain letters, digits, '-' and '_' (max 64 characters)")
        ntfy_url = _url(env, "NTFY_URL", "https://ntfy.sh", insecure_ok=True, required_https=False)
        if ntfy_url.lower().startswith("http://"):
            warnings.append("NTFY_URL is plain http://; notifications (fault codes, appliance name) travel unencrypted.")
        if "ntfy.sh" in ntfy_url and len(topic) < 12:
            warnings.append(
                "NTFY_TOPIC is short and ntfy.sh topics are public: anyone who guesses it can read your alerts. "
                "Use a long random name (e.g. `openssl rand -hex 12`)."
            )
        ntfy = NtfyConfig(
            ntfy_url, topic, _get(env, "NTFY_TOKEN"), _get(env, "NTFY_USER"), _get(env, "NTFY_PASSWORD"),
            _events(env, "NTFY_EVENTS"),
        )

    telegram = None
    tg_token, tg_chat = _get(env, "TELEGRAM_BOT_TOKEN"), _get(env, "TELEGRAM_CHAT_ID")
    if tg_token or tg_chat:
        if not (tg_token and tg_chat):
            raise ConfigError("Telegram needs both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        telegram = TelegramConfig(
            tg_token, tg_chat, _get(env, "TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            _events(env, "TELEGRAM_EVENTS"),
        )

    smtp = None
    smtp_host = _get(env, "SMTP_HOST")
    if smtp_host:
        security = _get(env, "SMTP_SECURITY", "starttls").lower()
        if security not in ("starttls", "ssl", "none"):
            raise ConfigError("SMTP_SECURITY must be starttls, ssl or none")
        recipients = _csv(_get(env, "SMTP_TO"))
        if not recipients:
            raise ConfigError("Email needs SMTP_TO (one or more addresses, comma separated)")
        user = _get(env, "SMTP_USER")
        smtp = SmtpConfig(
            smtp_host,
            _int(env, "SMTP_PORT", 465 if security == "ssl" else 587, 1, 65535),
            security,
            user,
            _get(env, "SMTP_PASSWORD"),
            _get(env, "SMTP_FROM") or user or "bwwatch@localhost",
            recipients,
            _events(env, "SMTP_EVENTS"),
        )

    webhook = None
    hook_url = _get(env, "WEBHOOK_URL")
    if hook_url:
        if not hook_url.lower().startswith(("https://", "http://")):
            raise ConfigError("WEBHOOK_URL must start with https:// or http://")
        hook_format = _get(env, "WEBHOOK_FORMAT", "json").lower()
        if hook_format not in ("json", "slack", "discord"):
            raise ConfigError("WEBHOOK_FORMAT must be json, slack or discord")
        webhook = WebhookConfig(hook_url, hook_format, _events(env, "WEBHOOK_EVENTS"))

    homeassistant = None
    ha_url = _get(env, "HA_WEBHOOK_URL")
    if ha_url:
        if not ha_url.lower().startswith(("https://", "http://")):
            raise ConfigError(
                "HA_WEBHOOK_URL must be the full webhook address, e.g. https://your-ha.example/api/webhook/<webhook_id>"
            )
        if ha_url.lower().startswith("http://"):
            warnings.append(
                "HA_WEBHOOK_URL is plain http://: the webhook address (which acts as a password) and the alerts "
                "are not encrypted. Use https, or http only over a private VPN such as Tailscale."
            )
        homeassistant = HomeAssistantConfig(ha_url, _events(env, "HA_WEBHOOK_EVENTS"))

    heartbeat = _get(env, "HEARTBEAT_URL")
    if heartbeat and not heartbeat.lower().startswith(("https://", "http://")):
        raise ConfigError("HEARTBEAT_URL must start with https:// or http://")

    display_tz = _get(env, "DISPLAY_TZ", "UTC") or "UTC"

    return Config(
        data_dir=Path(_get(env, "DATA_DIR", "/data")),
        interval=_int(env, "BW_POLL_INTERVAL_SECONDS", 3600, MIN_POLL_SECONDS, 86400),
        api_base=api_base,
        auth_base=auth_base,
        client_id=_get(env, "BW_CLIENT_ID", DEFAULT_CLIENT_ID),
        redirect_uri=_get(env, "BW_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        scope=_get(env, "BW_SCOPE", DEFAULT_SCOPE),
        user_agent=_get(env, "BW_USER_AGENT", DEFAULT_USER_AGENT),
        http_timeout=_float(env, "BW_HTTP_TIMEOUT_SECONDS", 20.0, 3.0, 120.0),
        extra_headers=extra_headers,
        account_id=_get(env, "BW_ACCOUNT_ID"),
        seed_refresh_token=_get(env, "BW_REFRESH_TOKEN"),
        allow_insecure_http=insecure,
        list_request=list_request,
        status_request=status_request,
        fault_request=fault_request,
        fault_options=fault_options,
        scan_status=_bool(env, "BW_SCAN_STATUS", True),
        scan_ignore=_regex(env, "BW_SCAN_IGNORE", DEFAULT_SCAN_IGNORE),
        fault_priority=_int(env, "FAULT_PRIORITY", 4, 1, 5),
        notify_retry_seconds=_int(env, "NOTIFY_RETRY_SECONDS", 300, 30, 86400),
        max_alerts_per_cycle=_int(env, "MAX_ALERTS_PER_CYCLE", 5, 1, 50),
        notify_status_changes=_bool(env, "NOTIFY_STATUS_CHANGES", True),
        notify_cleared=_bool(env, "NOTIFY_CLEARED", True),
        health_after_failures=_int(env, "HEALTH_ALERT_AFTER_FAILURES", 3, 1, 100),
        health_repeat_hours=_int(env, "HEALTH_REPEAT_HOURS", 12, 1, 24 * 30),
        heartbeat_url=heartbeat,
        display_tz=display_tz,
        backup_every_hours=_int(env, "BACKUP_EVERY_HOURS", 24, 1, 24 * 30),
        backup_keep=_int(env, "BACKUP_KEEP", 14, 1, 365),
        ntfy=ntfy,
        telegram=telegram,
        smtp=smtp,
        webhook=webhook,
        homeassistant=homeassistant,
        warnings=tuple(warnings),
    )
