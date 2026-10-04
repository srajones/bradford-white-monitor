"""Looking for the notifications / fault-history endpoint by trying a few likely names.

Every guess is a plain read-only ``GET`` of a ``get...`` endpoint and passes the same read-only
guard as everything else. It runs only when someone asks for it (``bwwatch probe --yes`` or the
setup wizard, after saying yes), sends about a dozen requests once, and stops at the first
"slow down" answer.
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional, Tuple

from .config import RequestSpec
from .errors import TransientError, WaveError
from .wave import HttpResponse, WaveApi

# Guessed names for the notification / fault-history read. Every one is a plain 'get...' read.
PROBE_NAMES = (
    "getNotifications", "getNotificationList", "getNotificationHistory", "getApplianceNotifications",
    "getFaults", "getFaultHistory", "getFaultList", "getApplianceFaults",
    "getAlerts", "getAlarms", "getEvents", "getApplianceHistory",
)
PROBE_CONTROL = "getZzzNoSuchEndpoint"
PROBE_PAUSE_SECONDS = 3.0  # between guesses: gentle on the server
REQUEST_COUNT = len(PROBE_NAMES) + 2  # the guesses + one "does not exist" control + the appliance list


def _signature(resp: HttpResponse) -> Tuple[int, str]:
    return resp.status, " ".join(resp.text().split())[:80]


def run_probe(
    api: WaveApi,
    say: Callable[[str], None],
    stop: Optional[threading.Event] = None,
) -> List[str]:
    """Try the guesses; returns the names whose answer differs from "no such endpoint".

    Raises WaveError for sign-in / network problems. Prints progress through ``say``.
    """
    stop = stop or threading.Event()
    items = api.list_appliances()
    if not items:
        raise WaveError("the account has no appliances")
    ctx = {"mac": str(items[0].get("macAddress") or ""), "serial": "", "name": ""}

    def ask(name: str) -> HttpResponse:
        spec = RequestSpec.parse("GET /wave/%s?username={account_id}&macAddress={mac}" % name, label="probe")
        return api.raw(spec, ctx)

    baseline = _signature(ask(PROBE_CONTROL))
    say("An endpoint that does not exist answers:  HTTP %d  %s" % baseline)
    found: List[str] = []
    for name in PROBE_NAMES:
        if stop.wait(PROBE_PAUSE_SECONDS):
            break
        resp = ask(name)
        if resp.status == 429:
            raise TransientError("the server asked us to slow down (HTTP 429), so the search was stopped", retry_after=3600.0)
        if _signature(resp) == baseline:
            say("  %-28s no such endpoint" % name)
            continue
        found.append(name)
        say("  %-28s HTTP %d  <-- different from an unknown endpoint" % (name, resp.status))
        snippet = " ".join(resp.text().split())[:160]
        if snippet:
            say("      %s" % snippet)
    return found
