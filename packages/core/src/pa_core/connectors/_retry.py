"""Google API retry.

google-api-python-client does not retry usefully by default. Retry only what is
transient: rate limits and 5xx. Never retry 401 or insufficientPermissions -- those are
configuration bugs and must surface loudly instead of being masked as flakiness.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import structlog
from googleapiclient.errors import HttpError

log = structlog.get_logger("pa.google")
T = TypeVar("T")

_RETRY_REASONS = {
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quotaexceeded",
    "backenderror",
    "internalerror",
}


def _retryable(e: HttpError) -> bool:
    status = e.resp.status if e.resp is not None else 0
    if status in (429, 500, 502, 503, 504):
        return True
    if status == 403:
        # 403 is overloaded: rate limiting AND permission denial share it.
        try:
            reasons = {
                d.get("reason", "").lower()
                for d in (e.error_details if isinstance(e.error_details, list) else [])
                if isinstance(d, dict)
            }
        except Exception:
            reasons = set()
        blob = (str(e) or "").lower()
        return bool(reasons & _RETRY_REASONS) or any(r in blob for r in _RETRY_REASONS)
    return False


def retry_google(fn: Callable[[], T], *, attempts: int = 5, base: float = 1.0) -> T:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except HttpError as e:
            last = e
            if not _retryable(e) or i == attempts:
                raise
            wait = min(base * 2 ** (i - 1), 30) * (0.5 + random.random())
            log.warning(
                "google.retry",
                attempt=i,
                wait_s=round(wait, 1),
                status=e.resp.status if e.resp else None,
            )
            time.sleep(wait)
    raise last  # type: ignore[misc]
