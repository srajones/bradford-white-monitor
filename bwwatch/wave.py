"""Talking to Bradford White's Wave cloud: sign-in (Azure AD B2C) and the REST API.

Sign-in model (matches the community client gclenaghan/bradford-white-wave-client,
which found that scripted password login does not work):

* The account is signed in once in a *browser*. The OAuth redirect goes to the
  app's custom URL scheme, so the browser shows an error - but the address of
  that redirect contains a one-time ``code``.
* ``login`` exchanges that code for a *refresh token*. From then on only the
  refresh token is used; the server rotates it, and every new one is written to
  disk (atomically) before it is relied on.
"""
from __future__ import annotations

import base64
import email.utils
import http.client
import http.cookiejar
import json
import logging
import re
import socket
import ssl
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .config import Config, RequestSpec
from .errors import ApiError, AuthError, TransientError, WaveError
from .readonly import check_read_only
from .util import atomic_write_json, iso, jwt_claims, scrub, truncate

__all__ = ["ApiError", "AuthError", "TransientError", "WaveError"]

log = logging.getLogger("bwwatch.wave")

MAX_BODY = 5 * 1024 * 1024


# --- minimal HTTP -----------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a bearer token must not be forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str]
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        if not self.body.strip():
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError:
            raise ApiError("response was not valid JSON: %s" % scrub(self.text(), 120))


def shown_url(url: str) -> str:
    """URL without query string, for logs and error messages."""
    parts = urllib.parse.urlsplit(url)
    return "%s://%s%s" % (parts.scheme, parts.netloc, parts.path)


def http_request(
    method: str,
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    json_body: Any = None,
    form: Optional[Mapping[str, str]] = None,
    timeout: float = 20.0,
    label: Optional[str] = None,
) -> HttpResponse:
    """One HTTP request. ``label`` replaces the URL in error text (for URLs that are secrets)."""
    shown = label or shown_url(url)
    data = None
    out_headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        out_headers.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        out_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=data, method=method, headers=out_headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(MAX_BODY + 1)
            status = response.status
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:  # 3xx/4xx/5xx still carry a useful body
        try:
            body = exc.read(MAX_BODY + 1)
        finally:
            exc.close()
        status = exc.code
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
    except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, http.client.HTTPException, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransientError("%s %s: %s" % (method, shown, scrub(reason, 200))) from exc
    if len(body) > MAX_BODY:
        raise ApiError("%s %s: response is larger than %d bytes" % (method, shown, MAX_BODY))
    return HttpResponse(status, resp_headers, body)


def retry_after_seconds(resp: "HttpResponse", default: float = 3600.0) -> float:
    """How long the server asked us to wait (Retry-After seconds or date), bounded to 6 hours."""
    raw = resp.headers.get("retry-after", "").strip()
    seconds = default
    if raw:
        try:
            seconds = float(raw)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
                seconds = max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                seconds = default
    return max(60.0, min(seconds, 6 * 3600.0))


# --- a record of every request (for the request log) -------------------------
# Response headers worth keeping: they say how the server is doing (rate limits, region, request ids), never who we are.
_KEEP_HEADER = re.compile(
    r"^(date|server|via|age|retry-after|content-type|content-length|cache-control|"
    r"x-[a-z-]*(request|rate|limit|trace|correlation|region|version|cache|backend)[a-z-]*|"
    r"(x-)?ratelimit[a-z-]*|apim[a-z-]*|(x-)?ms-[a-z-]*|x-ms-[a-z-]*)$"
)
MAX_CALL_RECORDS = 200


@dataclass
class CallRecord:
    at: str
    kind: str  # "api" (the Wave API) or "token" (the sign-in server)
    endpoint: str  # "GET /wave/getApplianceStatus": never the query string, which can hold ids
    status: Optional[int]
    ms: int
    size: int
    headers: Dict[str, str]
    error: Optional[str] = None


def keep_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    return {k: truncate(v, 200) for k, v in headers.items() if _KEEP_HEADER.match(k.lower())}


def note_call(
    sink: List[CallRecord], kind: str, method: str, url: str, resp: Optional["HttpResponse"], started: float,
    error: Optional[BaseException] = None,
) -> None:
    if len(sink) >= MAX_CALL_RECORDS:
        return
    sink.append(
        CallRecord(
            at=iso(), kind=kind, endpoint="%s %s" % (method, urllib.parse.urlsplit(url).path or "/"),
            status=resp.status if resp is not None else None, ms=int((time.monotonic() - started) * 1000),
            size=len(resp.body) if resp is not None else 0, headers=keep_headers(resp.headers) if resp is not None else {},
            error=scrub(error, 200) if error is not None else None,
        )
    )


def check_reachable(url: str, user_agent: str = "bwwatch", timeout: float = 10.0) -> Tuple[bool, str]:
    """Can we get *any* HTTP answer from this address? DNS, connection, proxy and TLS must all work.

    Used by the setup wizard; it goes through the same code (and proxy settings) as real traffic.
    """
    try:
        resp = http_request("GET", url, headers={"User-Agent": user_agent}, timeout=timeout, label=shown_url(url))
    except WaveError as exc:
        return False, scrub(exc, 200)
    return True, "HTTP %d" % resp.status


# --- token storage ----------------------------------------------------------
class TokenStore:
    """The rotating refresh token, kept in one small file that is only ever replaced atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.error("token file %s is unreadable (%s); a new `login` is needed", self.path, exc)
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def save(self, refresh_token: str, **extra: Any) -> None:
        record: Dict[str, Any] = {"refresh_token": refresh_token, "saved_at": iso()}
        record.update({k: v for k, v in extra.items() if v is not None})
        atomic_write_json(self.path, record, mode=0o600)


def _oauth_error(resp: HttpResponse) -> str:
    """Short, secret-free description of an OAuth error response."""
    try:
        data = resp.json()
    except ApiError:
        data = None
    if isinstance(data, dict) and (data.get("error") or data.get("error_description")):
        description = str(data.get("error_description") or "").strip().splitlines()
        first = description[0] if description else ""
        return scrub("%s %s" % (data.get("error", ""), first), 300)
    return scrub(resp.text(), 200)


def parse_redirect(text: str) -> Tuple[str, Dict[str, str]]:
    """Pull the one-time ``code`` out of what the user pasted.

    Accepts the full ``com.bradfordwhiteapps.bwconnect://oauth/redirect?...`` address,
    a bare ``code=...&state=...`` query, or just the code itself. Returns
    ``(code, all_query_params)``. Raises AuthError with a plain-English reason.
    """
    raw = (text or "").strip().strip("\"'<>").strip()
    if not raw:
        raise AuthError("nothing was pasted")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme in ("http", "https"):
        if parts.path.rstrip("/").endswith("/confirmed"):
            raise AuthError(
                "that is the intermediate 'confirmed' page. Use the final address that starts with "
                "com.bradfordwhiteapps.bwconnect:// (the 'location' response header of the failed request)."
            )
        if "authorize" in parts.path and "code=" not in parts.query:
            raise AuthError(
                "that is the sign-in address itself, not where the browser was sent afterwards. "
                "Copy the 'location' response header of the *failed* request instead."
            )
    query = parts.query if "://" in raw or "?" in raw else raw
    params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items() if v}
    if params.get("error"):
        raise AuthError("the sign-in page reported an error: %s %s" % (params["error"], params.get("error_description", "")))
    code = params.get("code")
    if not code and "://" not in raw and "=" not in raw and len(raw) > 20 and " " not in raw:
        code = raw  # a bare code
    if not code:
        raise AuthError("no 'code=' value found in what was pasted")
    return code, params


# --- sign-in ----------------------------------------------------------------
class TokenManager:
    """Keeps a valid bearer token, rotating and persisting the refresh token as it goes."""

    def __init__(self, cfg: Config, store: TokenStore, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.store = store
        self._clock = clock
        self._bearer: Optional[str] = None
        self._expires_at = 0.0
        self._margin = 120.0  # renew this long before expiry (shrinks for very short-lived tokens)
        self.account_id: Optional[str] = cfg.account_id or None
        self.refresh_expires_at: Optional[str] = None
        self.retry_after = 0.0  # seconds the sign-in server last asked us to wait (0 = no request)
        self.calls: List[CallRecord] = []  # sign-in requests of the current poll (the service stores and clears them)

    def _headers(self) -> Dict[str, str]:
        return {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}

    def authorization_url(self, state: str, nonce: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self.cfg.client_id,
                "redirect_uri": self.cfg.redirect_uri,
                "response_type": "code",
                "scope": self.cfg.scope,
                "state": state,
                "nonce": nonce,
            }
        )
        return "%s?%s" % (self.cfg.authorize_url, query)

    def has_credentials(self) -> bool:
        return bool(self.store.load() or self.cfg.seed_refresh_token or (self.cfg.username and self.cfg.password))

    def _current_refresh_token(self) -> Optional[str]:
        saved = self.store.load()
        if saved:
            return str(saved["refresh_token"])
        if self.cfg.seed_refresh_token:
            # First start with a token from .env: adopt it so later rotations are persisted.
            self.store.save(self.cfg.seed_refresh_token, source="env")
            log.info("adopted BW_REFRESH_TOKEN from the environment; later tokens are kept in %s", self.store.path)
            return self.cfg.seed_refresh_token
        if self.cfg.username and self.cfg.password:
            log.info("no token found; attempting automated sign-in using credentials from .env")
            self.login_with_credentials()
            saved = self.store.load()
            if saved:
                return str(saved["refresh_token"])
        return None

    def login_with_credentials(
        self, username: Optional[str] = None, password: Optional[str] = None
    ) -> Dict[str, Any]:
        """Perform automated headless Azure AD B2C sign-in using username and password."""
        user = (username or self.cfg.username or "").strip()
        pwd = password if password is not None else self.cfg.password
        if not user or not pwd:
            raise AuthError("username and password are required for automated login")

        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

        state = uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        auth_url = self.authorization_url(state, nonce)
        req = urllib.request.Request(
            auth_url,
            headers={
                "User-Agent": self.cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        try:
            resp = opener.open(req, timeout=self.cfg.http_timeout)
            html = resp.read().decode("utf-8", "replace")
        except Exception as exc:
            raise AuthError("failed to load Azure AD B2C sign-in page: %s" % scrub(exc, 150))

        csrf_match = re.search(r'\"csrf\":\"(.*?)\"', html)
        trans_match = re.search(r'\"transId\":\"(.*?)\"', html)
        if not csrf_match or not trans_match:
            raise AuthError("Azure AD B2C sign-in page did not return required tokens")
        csrf = csrf_match.group(1)
        trans_id = trans_match.group(1)

        b2c_base = self.cfg.auth_base.rsplit("/oauth2", 1)[0]
        self_asserted_url = f"{b2c_base}/SelfAsserted?tx={urllib.parse.quote(trans_id)}&p=B2C_1_Wave_SignIn"
        post_data = {
            "request_type": "RESPONSE",
            "email": user,
            "password": pwd,
        }
        post_bytes = urllib.parse.urlencode(post_data).encode("ascii")
        req2 = urllib.request.Request(
            self_asserted_url,
            data=post_bytes,
            headers={
                "User-Agent": self.cfg.user_agent,
                "X-CSRF-TOKEN": csrf,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            resp2 = opener.open(req2, timeout=self.cfg.http_timeout)
            raw2 = resp2.read().decode("utf-8", "replace")
            res2 = json.loads(raw2)
        except Exception as exc:
            raise AuthError("credential submission to Azure AD B2C failed: %s" % scrub(exc, 150))

        if str(res2.get("status")) != "200":
            msg = res2.get("message") or res2.get("error_description") or "invalid username or password"
            raise AuthError("sign-in rejected by Azure AD B2C: %s" % scrub(msg, 200))

        confirmed_url = (
            f"{b2c_base}/api/CombinedSigninAndSignUp/confirmed?rememberMe=false"
            f"&csrf_token={urllib.parse.quote(csrf)}&tx={urllib.parse.quote(trans_id)}&p=B2C_1_Wave_SignIn"
        )
        opener_noredir = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), _NoRedirect())
        req3 = urllib.request.Request(
            confirmed_url,
            headers={"User-Agent": self.cfg.user_agent},
        )
        redirect_loc = None
        try:
            opener_noredir.open(req3, timeout=self.cfg.http_timeout)
        except urllib.error.HTTPError as exc:
            redirect_loc = exc.headers.get("Location")
        except Exception as exc:
            raise AuthError("failed to complete Azure AD B2C sign-in confirmation: %s" % scrub(exc, 150))

        if not redirect_loc:
            raise AuthError("sign-in succeeded but no redirect location was returned by Azure AD B2C")

        code, _ = parse_redirect(redirect_loc)
        self.exchange_code(code)
        log.info("completed automated headless sign-in for %s (account %s)", scrub(user, 50), self.account_id)
        return self.store.load() or {}

    def _post_token(self, form: Mapping[str, str], what: str) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            resp = http_request("POST", self.cfg.token_url, headers=self._headers(), form=form, timeout=self.cfg.http_timeout)
        except WaveError as exc:
            note_call(self.calls, "token", "POST", self.cfg.token_url, None, started, exc)
            raise
        note_call(self.calls, "token", "POST", self.cfg.token_url, resp, started)
        if resp.status == 200:
            data = resp.json()
            if not isinstance(data, dict):
                raise ApiError("%s: unexpected token response" % what)
            return data
        detail = _oauth_error(resp)
        if resp.status == 429:
            wait = retry_after_seconds(resp)
            self.retry_after = max(self.retry_after, wait)
            raise TransientError("%s was rate limited (HTTP 429); asked to wait %d s" % (what, wait), retry_after=wait)
        if resp.status in (400, 401, 403):
            raise AuthError("%s was rejected (HTTP %d): %s" % (what, resp.status, detail))
        if resp.status in (408, 425) or resp.status >= 500:
            raise TransientError("%s failed (HTTP %d): %s" % (what, resp.status, detail))
        raise ApiError("%s failed (HTTP %d): %s" % (what, resp.status, detail))

    def _adopt(self, tokens: Mapping[str, Any]) -> None:
        # The API accepts the id_token when no access_token is issued (as the app's own client does).
        bearer = tokens.get("access_token") or tokens.get("id_token")
        if not bearer:
            raise ApiError("the token response had neither an access_token nor an id_token")
        claims = jwt_claims(str(bearer))
        now = self._clock()
        expires = claims.get("exp")
        if not isinstance(expires, (int, float)):
            lifetime = tokens.get("expires_in") or tokens.get("id_token_expires_in") or 600
            try:
                expires = now + float(lifetime)
            except (TypeError, ValueError):
                expires = now + 600
        self._bearer = str(bearer)
        self._expires_at = float(expires)
        self._margin = min(120.0, max(1.0, (self._expires_at - now) / 2.0))
        if not self.cfg.account_id:
            self.account_id = claims.get("oid") or claims.get("sub") or self.account_id

    def exchange_code(self, code: str) -> None:
        """Turn the one-time login code into a saved refresh token."""
        tokens = self._post_token(
            {
                "grant_type": "authorization_code",
                "client_id": self.cfg.client_id,
                "scope": self.cfg.scope,
                "code": code,
                "redirect_uri": self.cfg.redirect_uri,
            },
            "code exchange",
        )
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise AuthError("sign-in worked but no refresh token came back (is 'offline_access' in BW_SCOPE?)")
        self._adopt(tokens)
        self.store.save(str(refresh), source="login", refresh_expires_in=tokens.get("refresh_token_expires_in"))

    def refresh(self) -> None:
        refresh_token = self._current_refresh_token()
        if not refresh_token:
            raise AuthError("not signed in yet. Run: docker compose run --rm bwwatch login")
        tokens = self._post_token(
            {
                "grant_type": "refresh_token",
                "client_id": self.cfg.client_id,
                "refresh_token": refresh_token,
                "scope": self.cfg.scope,
            },
            "token refresh",
        )
        rotated = tokens.get("refresh_token")
        if rotated and rotated != refresh_token:
            # Save BEFORE using anything from this response: the old token may now be dead.
            self.store.save(str(rotated), source="refresh", refresh_expires_in=tokens.get("refresh_token_expires_in"))
        self._adopt(tokens)

    def bearer(self, force: bool = False) -> str:
        if not force and self._bearer and self._clock() < self._expires_at - self._margin:
            return self._bearer
        self.refresh()
        assert self._bearer is not None
        return self._bearer

    def invalidate(self) -> None:
        self._bearer = None
        self._expires_at = 0.0


# --- API calls --------------------------------------------------------------
class WaveApi:
    def __init__(
        self,
        cfg: Config,
        tokens: TokenManager,
        stop: Optional[threading.Event] = None,
        retry_delays: Tuple[float, ...] = (2.0, 6.0),
    ):
        self.cfg = cfg
        self.tokens = tokens
        self.stop = stop or threading.Event()
        self.retry_delays = retry_delays
        self.retry_after = 0.0  # seconds the API last asked us to wait (0 = no request)
        self.calls: List[CallRecord] = []  # every request of the current poll (the service stores and clears them)

    def _headers(self, token: str) -> Dict[str, str]:
        headers = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        headers.update(self.cfg.extra_headers)
        headers["Authorization"] = "Bearer " + token
        return headers

    def _absolute(self, target: str) -> str:
        if target.startswith("/"):
            return self.cfg.api_base + target
        wanted = urllib.parse.urlsplit(self.cfg.api_base)
        given = urllib.parse.urlsplit(target)
        if (given.scheme, given.netloc) != (wanted.scheme, wanted.netloc):
            raise ApiError("refusing to send the sign-in token to %s (only %s is allowed)" % (given.netloc, wanted.netloc))
        return target

    def call(self, spec: RequestSpec, extra_ctx: Optional[Mapping[str, str]] = None) -> Any:
        """Run a configured request and return the decoded JSON (None for an empty body).

        This is the ONLY way bwwatch talks to the Wave API, and the read-only check below
        runs before anything else - before signing in, before any network traffic.
        """
        check_read_only(spec.method, spec.target, spec.body)
        self.tokens.bearer()  # sign in first: it also tells us the account id
        ctx: Dict[str, str] = {"account_id": self.tokens.account_id or ""}
        ctx.update(extra_ctx or {})
        try:
            target, body = spec.render(ctx)
        except KeyError as exc:
            raise ApiError(
                "the request needs %s, which is not available (set BW_ACCOUNT_ID if the sign-in token has no account id)"
                % exc
            )
        url = self._absolute(target)
        for attempt in range(len(self.retry_delays) + 1):
            try:
                return self._send(spec.method, url, body)
            except TransientError as exc:
                if exc.retry_after is not None or attempt >= len(self.retry_delays):
                    raise  # a rate limit is never retried inside the same poll
                log.warning("%s - retrying in %.0fs", exc, self.retry_delays[attempt])
                if self.stop.wait(self.retry_delays[attempt]):
                    raise

    def _exchange(self, method: str, url: str, body: Any) -> HttpResponse:
        """The single place a request is sent to the Wave API (callers have run check_read_only)."""
        if method not in ("GET", "POST"):  # belt and braces
            raise ApiError("refusing to send a %s request" % method)
        resp: Optional[HttpResponse] = None
        for round_ in (1, 2):
            token = self.tokens.bearer(force=(round_ == 2))
            started = time.monotonic()
            try:
                resp = http_request(method, url, headers=self._headers(token), json_body=body, timeout=self.cfg.http_timeout)
            except WaveError as exc:
                note_call(self.calls, "api", method, url, None, started, exc)
                raise
            note_call(self.calls, "api", method, url, resp, started)
            if resp.status == 401 and round_ == 1:
                log.info("the API answered 401; refreshing the sign-in and trying once more")
                continue
            break
        assert resp is not None
        log.debug("%s %s -> HTTP %d (%d bytes)", method, shown_url(url), resp.status, len(resp.body))
        return resp

    def raw(self, spec: RequestSpec, extra_ctx: Optional[Mapping[str, str]] = None) -> HttpResponse:
        """One read-only request, answer returned as-is (no error mapping, no retries). Used by `probe`."""
        check_read_only(spec.method, spec.target, spec.body)
        self.tokens.bearer()
        ctx: Dict[str, str] = {"account_id": self.tokens.account_id or ""}
        ctx.update(extra_ctx or {})
        try:
            target, body = spec.render(ctx)
        except KeyError as exc:
            raise ApiError("the request needs %s, which is not available" % exc)
        return self._exchange(spec.method, self._absolute(target), body)

    def _send(self, method: str, url: str, body: Any) -> Any:
        resp = self._exchange(method, url, body)
        status = resp.status
        if 200 <= status < 300:
            return resp.json()
        snippet = scrub(resp.text(), 200)
        where = "%s %s" % (method, shown_url(url))
        if status == 401:
            raise AuthError("%s: the API rejected the freshly refreshed sign-in (HTTP 401)" % where)
        if status == 403 and "access denied" in resp.text().lower():
            raise AuthError("%s: access denied (HTTP 403)" % where)
        if status == 429:
            wait = retry_after_seconds(resp)
            self.retry_after = max(self.retry_after, wait)
            raise TransientError("%s: rate limited (HTTP 429); asked to wait %d s" % (where, wait), retry_after=wait)
        if status in (408, 425) or status >= 500:
            raise TransientError("%s: HTTP %d %s" % (where, status, snippet))
        raise ApiError("%s: HTTP %d %s" % (where, status, snippet))

    def list_appliances(self) -> List[Dict[str, Any]]:
        payload = self.call(self.cfg.list_request)
        items = payload.get("appliances") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            raise ApiError("unexpected appliance-list response (expected an 'appliances' list; got %s)" % (keys,))
        return [item for item in items if isinstance(item, dict)]


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
