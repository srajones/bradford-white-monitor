"""Exception types shared across bwwatch."""
from __future__ import annotations

from typing import Optional


class ConfigError(Exception):
    """The environment/.env is wrong; the message says how to fix it."""


class WaveError(Exception):
    """Base class for problems talking to the Wave cloud."""


class AuthError(WaveError):
    """Sign-in was rejected: waiting will not fix it, a new `login` is needed."""


class TransientError(WaveError):
    """Network trouble / server error / rate limit: trying again later may work.

    ``retry_after`` (seconds) is set when the server explicitly asked us to slow down.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ApiError(WaveError):
    """The server answered, but not with what we expected."""


class ReadOnlyViolation(ApiError):
    """A request was refused because it could change something on the water heater."""
