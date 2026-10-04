"""A stand-in for the Wave cloud (sign-in + API) and for the notification services.

It deliberately includes the two real *write* endpoints (changeSetpoint / changeOpMode)
and records every hit in ``writes`` so tests can prove bwwatch never touches them.
"""
from __future__ import annotations

import base64
import json
import socketserver
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

CLIENT_ID = "7899415d-1c23-46d8-8a79-4c15ed5f7f22"
REDIRECT = "com.bradfordwhiteapps.bwconnect://oauth/redirect"
SCOPE = "openid email offline_access profile"
MAC = "AA:BB:CC:DD:EE:FF"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(claims: Dict[str, Any]) -> str:
    return ".".join([_b64(b'{"alg":"RS256","typ":"JWT"}'), _b64(json.dumps(claims).encode()), _b64(b"signature-bytes-here")])


class MockWave:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.oid = "11111111-2222-3333-4444-555555555555"
        self.requests: List[Dict[str, Any]] = []
        self.writes: List[Dict[str, Any]] = []
        self.valid_codes: set = set()
        self.valid_refresh: set = set()
        self.valid_access: Dict[str, float] = {}
        self.rotate_refresh = True
        self.id_token_only = True  # B2C with only OIDC scopes issues just an id_token
        self.access_lifetime = 3600
        self.token_override: Optional[Tuple[int, Any, Dict[str, str]]] = None
        self.api_queue: List[Tuple[int, Any, Dict[str, str]]] = []
        self.appliances: List[Dict[str, Any]] = [
            {"macAddress": MAC, "friendlyName": "Basement", "serialNumber": "SN123", "applianceType": "HEAT_PUMP", "accessLevel": 1}
        ]
        self.status: Dict[str, Dict[str, Any]] = {
            MAC: {"macAddress": MAC, "friendlyName": "Basement", "serialNumber": "SN123", "setpointFahrenheit": 120,
                  "mode": "Heat Pump", "heatModeValue": 3, "applianceType": "HEAT_PUMP", "accessLevel": 1}
        }
        self.notifications: Any = {"notifications": []}
        self.extra_routes: Dict[str, Tuple[int, Any]] = {}
        self.disabled_routes: set = set()
        self.sink_status: Dict[str, int] = {}
        self.sinks: Dict[str, List[Dict[str, Any]]] = {"ntfy": [], "telegram": [], "webhook": [], "ha": [], "heartbeat": []}
        self._counter = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.mock = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    # --- lifecycle ----------------------------------------------------------
    def start(self) -> "MockWave":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    # --- helpers for tests --------------------------------------------------
    def issue_login_code(self, code: str = "one-time-login-code-abcdefghijklmnop") -> str:
        with self.lock:
            self.valid_codes.add(code)
        return code

    def seed_refresh_token(self, token: str = "seed-refresh-token-0001") -> str:
        with self.lock:
            self.valid_refresh.add(token)
        return token

    def hits(self, prefix: str) -> List[Dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if r["path"].startswith(prefix)]

    def api_hits(self, name: str) -> List[Dict[str, Any]]:
        return self.hits("/wave/" + name)

    def revoke_all_tokens(self) -> None:
        with self.lock:
            self.valid_refresh.clear()
            self.valid_access.clear()

    # --- behaviour ----------------------------------------------------------
    def _issue(self) -> Dict[str, Any]:
        with self.lock:
            self._counter += 1
            now = time.time()
            refresh = "refresh-token-%04d-%s" % (self._counter, uuid.uuid4().hex[:8])
            self.valid_refresh.add(refresh)
            jwt = make_jwt({"oid": self.oid, "sub": self.oid, "exp": int(now) + self.access_lifetime, "n": self._counter})
            self.valid_access[jwt] = now + self.access_lifetime
            body: Dict[str, Any] = {
                "id_token": jwt, "token_type": "Bearer", "expires_in": self.access_lifetime,
                "refresh_token": refresh, "refresh_token_expires_in": 1209600, "scope": SCOPE,
            }
            if not self.id_token_only:
                body["access_token"] = jwt
            return body

    def handle_token(self, h: "_Handler", rec: Dict[str, Any]) -> None:
        form = {k: v[0] for k, v in urllib.parse.parse_qs(rec["body"]).items()}
        rec["form"] = form
        with self.lock:
            override = self.token_override
        if override:
            h.reply(*override)
            return
        if form.get("client_id") != CLIENT_ID or form.get("scope") != SCOPE:
            h.reply(400, {"error": "invalid_request", "error_description": "AADB2C90xxx: bad client or scope"})
            return
        grant = form.get("grant_type")
        grant_error = (400, {"error": "invalid_grant", "error_description":
                             "AADB2C90080: The provided grant has expired. Please re-authenticate and try again.\r\n"
                             "Trace ID: 0000\r\nCorrelation ID: 1111\r\nTimestamp: now"}, {})
        with self.lock:
            if grant == "authorization_code":
                if form.get("redirect_uri") != REDIRECT or form.get("code") not in self.valid_codes:
                    h.reply(*grant_error)
                    return
                self.valid_codes.discard(form["code"])
            elif grant == "refresh_token":
                token = form.get("refresh_token", "")
                if token not in self.valid_refresh:
                    h.reply(*grant_error)
                    return
                if self.rotate_refresh:
                    self.valid_refresh.discard(token)
            else:
                h.reply(400, {"error": "unsupported_grant_type"})
                return
        h.reply(200, self._issue())

    def handle_api(self, h: "_Handler", rec: Dict[str, Any]) -> None:
        name = rec["path"].rsplit("/", 1)[-1]
        if name.lower() in ("changesetpoint", "changeopmode"):
            with self.lock:
                self.writes.append(rec)
            h.reply(200, {"status": "success"})  # a real server would happily change the heater
            return
        auth = rec["headers"].get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        with self.lock:
            expiry = self.valid_access.get(token)
            queued = self.api_queue.pop(0) if self.api_queue and expiry and expiry > time.time() else None
        if not expiry or expiry < time.time():
            h.reply(401, {"message": "Unauthorized"})
            return
        if queued:
            h.reply(*queued)
            return
        query = rec["query"]
        if name in self.disabled_routes:
            h.reply(404, {"message": "Not Found"})
            return
        if name == "getApplianceList":
            if query.get("username") != self.oid:
                h.reply(400, {"message": "bad username"})
                return
            h.reply(200, {"appliances": self.appliances})
        elif name == "getApplianceStatus":
            status = self.status.get(query.get("macAddress", ""))
            if status is None:
                h.reply(404, {"message": "unknown appliance"})
                return
            h.reply(200, dict(status, requestId=uuid.uuid4().hex))
        elif name == "getNotifications":
            h.reply(200, self.notifications)
        elif name in self.extra_routes:
            status, body = self.extra_routes[name]
            h.reply(status, body)
        else:
            h.reply(404, {"message": "Not Found"})

    def handle_sink(self, h: "_Handler", rec: Dict[str, Any]) -> bool:
        path = rec["path"]
        try:
            payload = json.loads(rec["body"]) if rec["body"] else None
        except ValueError:
            payload = rec["body"]
        if path == "/ntfy":
            name, extra, reply_body = "ntfy", {}, {"id": "abc", "event": "message"}
        elif path.startswith("/telegram/bot") and path.endswith("/sendMessage"):
            name, extra, reply_body = "telegram", {"token": path[len("/telegram/bot"):-len("/sendMessage")]}, {"ok": True}
        elif path == "/webhook":
            name, extra, reply_body = "webhook", {}, {"ok": True}
        elif path.startswith("/ha/api/webhook/"):
            name, extra, reply_body = "ha", {"webhook_id": path.rsplit("/", 1)[-1]}, ""
        elif path.startswith("/heartbeat/"):
            name, extra, reply_body = "heartbeat", {"id": path.rsplit("/", 1)[-1]}, ""
        else:
            return False
        status = self.sink_status.get(name, 200)
        if status == 200:
            with self.lock:
                self.sinks[name].append({"json": payload, "headers": rec["headers"], **extra})
        h.reply(status, reply_body if status == 200 else {"error": "sink down"})
        return True


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockWave/1"

    def log_message(self, *args: Any) -> None:  # silence
        pass

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def reply(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
        raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
        data = raw.encode() if isinstance(raw, str) else raw
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self) -> None:
        mock: MockWave = self.server.mock  # type: ignore[attr-defined]
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        rec = {
            "method": self.command,
            "path": parsed.path,
            "query": {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()},
            "headers": {k: v for k, v in self.headers.items()},
            "body": body,
        }
        with mock.lock:
            mock.requests.append(rec)
        if parsed.path == "/auth/token":
            mock.handle_token(self, rec)
        elif parsed.path.startswith("/wave/"):
            mock.handle_api(self, rec)
        elif not mock.handle_sink(self, rec):
            self.reply(404, {"message": "Not Found"})


class FakeSMTP:
    """Just enough SMTP (no TLS, no auth) to receive one message."""

    def __init__(self) -> None:
        self.messages: List[str] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                def send(line: str) -> None:
                    self.wfile.write((line + "\r\n").encode())
                    self.wfile.flush()

                send("220 fake ESMTP")
                while True:
                    line = self.rfile.readline().decode("utf-8", "replace").strip()
                    if not line:
                        return
                    verb = line.split(" ")[0].upper()
                    if verb in ("EHLO", "HELO"):
                        send("250 fake")
                    elif verb in ("MAIL", "RCPT", "RSET", "NOOP"):
                        send("250 ok")
                    elif verb == "DATA":
                        send("354 go ahead")
                        lines = []
                        while True:
                            part = self.rfile.readline().decode("utf-8", "replace")
                            if part.strip() == ".":
                                break
                            lines.append(part)
                        outer.messages.append("".join(lines))
                        send("250 queued")
                    elif verb == "QUIT":
                        send("221 bye")
                        return
                    else:
                        send("502 no")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    def start(self) -> "FakeSMTP":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def port(self) -> int:
        return self.server.server_address[1]
