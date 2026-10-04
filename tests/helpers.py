from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

from bwwatch.config import Config
from bwwatch.service import Service
from bwwatch.wave import TokenStore

from .mock_wave import MAC, MockWave

__all__ = ["MAC", "WaveTestCase", "make_env"]


def make_env(mock: MockWave, data_dir: Path, **extra: str) -> Dict[str, str]:
    env = {
        "DATA_DIR": str(data_dir),
        "BW_API_BASE": mock.url,
        "BW_AUTH_BASE": mock.url + "/auth",
        "BW_ALLOW_INSECURE_HTTP": "1",
        "BW_FAULT_REQUEST": "GET /wave/getNotifications?username={account_id}&macAddress={mac}",
        "NTFY_URL": mock.url + "/ntfy",
        "NTFY_TOPIC": "test-topic-long-enough-1234",
        "BW_HTTP_TIMEOUT_SECONDS": "5",
    }
    env.update(extra)
    return env


class WaveTestCase(unittest.TestCase):
    """A running mock cloud, a private data directory, and helpers to build config/services."""

    def setUp(self) -> None:
        self.mock = MockWave().start()
        self.addCleanup(self.mock.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="bwtest."))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.data = self.tmp / "data"
        self.data.mkdir()

    def cfg(self, **extra: str) -> Config:
        return Config.from_env(make_env(self.mock, self.data, **extra))

    def sign_in(self, cfg: Config) -> str:
        """Store a valid refresh token, as `login` would have."""
        token = self.mock.seed_refresh_token()
        TokenStore(cfg.data_dir / "token.json").save(token, source="test")
        return token

    def service(self, cfg: Config = None, signed_in: bool = True, **extra: str) -> Service:
        cfg = cfg or self.cfg(**extra)
        if signed_in:
            self.sign_in(cfg)
        svc = Service(cfg)
        svc.api.retry_delays = (0.0, 0.0)  # keep retry tests fast
        svc.open()
        self.addCleanup(svc.shutdown)
        return svc

    def outbox(self, svc: Service, **where: Any):
        rows = svc.conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        return [r for r in rows if all(r[k] == v for k, v in where.items())]
