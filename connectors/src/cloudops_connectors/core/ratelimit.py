"""Source rate limits (LLD, Connector contract table).

ServiceNow: "Honour X-RateLimit-Remaining, back off at 20%". The decision is a
pure function of the response headers so it can be tested without a clock;
the client does the waiting.

- 429: wait ``Retry-After`` seconds (or until ``X-RateLimit-Reset``), then retry.
- Remaining below 20% of the limit: wait until the window resets before the
  next request, so the platform never spends the last fifth of a shared quota
  that other ServiceNow integrations also need.
- Otherwise, no wait.

Waits are capped, so a nonsense header cannot park a worker for hours; a
capped wait that is still too short shows up as another 429 and another wait.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Final

BACKOFF_THRESHOLD: Final = 0.20
MAX_WAIT_SECONDS: Final = 300.0
DEFAULT_RETRY_AFTER: Final = 30.0


@dataclass(frozen=True, slots=True)
class Pause:
    seconds: float
    reason: str


def _float(headers: Mapping[str, str], name: str) -> float | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _until_reset(headers: Mapping[str, str], now: float) -> float | None:
    reset = _float(headers, "x-ratelimit-reset")
    if reset is None:
        return None
    return max(0.0, reset - now)


def _retry_after(headers: Mapping[str, str], now: float) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:  # HTTP-date form
        return max(0.0, parsedate_to_datetime(raw).timestamp() - now)
    except (TypeError, ValueError):
        return None


def pause_for(status: int, headers: Mapping[str, str], now: float) -> Pause | None:
    """How long to wait before the next request, or None. ``headers`` must be
    case-insensitive (httpx.Headers) or already lower-cased."""
    if status == 429:
        wait = _retry_after(headers, now)
        if wait is None:
            wait = _until_reset(headers, now)
        return Pause(min(wait if wait is not None else DEFAULT_RETRY_AFTER, MAX_WAIT_SECONDS),
                     "throttled (429)")
    limit = _float(headers, "x-ratelimit-limit")
    remaining = _float(headers, "x-ratelimit-remaining")
    if limit and remaining is not None and limit > 0 and remaining / limit < BACKOFF_THRESHOLD:
        wait = _until_reset(headers, now)
        return Pause(min(wait if wait is not None else DEFAULT_RETRY_AFTER, MAX_WAIT_SECONDS),
                     f"quota below {BACKOFF_THRESHOLD:.0%}")
    return None
