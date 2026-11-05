"""Timing and Retry-After parsing for Spotify Web API requests."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


def normalize_request_interval_seconds(value: float | None) -> float:
    interval = 0.0 if value is None else value
    if not math.isfinite(interval) or interval < 0:
        raise ValueError("--request-interval-seconds must be finite and non-negative")
    return interval


def parse_retry_after(value: object) -> float:
    try:
        seconds = float(str(value or "").strip())
    except ValueError:
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


@dataclass(frozen=True)
class SpotifyRateLimitInfo:
    kind: Literal["rate_limit", "quota_exceeded"]
    retry_after_seconds: float | None
    received_at_monotonic: float
    received_at_unix: float
    operation_name: str


class SpotifyRequestControl:
    def __init__(
        self,
        minimum_interval_seconds: float = 0.0,
        *,
        continuous: bool = False,
        wall_time: Callable[[], float] = time.time,
        notice: Callable[[str], None] = print,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.minimum_interval_seconds = normalize_request_interval_seconds(minimum_interval_seconds)
        self.base_interval_seconds = self.minimum_interval_seconds
        self.continuous = continuous
        self.wall_time = wall_time
        self.notice = notice
        self.rate_limit_count = 0
        self.quota_count = 0
        self.wait_seconds = 0.0
        self._fallback_count = 0
        self.monotonic = monotonic
        self.sleep = sleep
        self.request_count = 0
        self._last_request_start: float | None = None

    def before_request(self) -> None:
        while self._last_request_start is not None:
            elapsed = self.monotonic() - self._last_request_start
            remaining = self.minimum_interval_seconds - elapsed
            if remaining <= 0:
                return
            # Bound each sleep, not the interval, so large finite values remain interruptible.
            self.sleep(min(remaining, 60.0))

    def record_request_start(self) -> None:
        self._last_request_start = self.monotonic()
        self.request_count += 1

    def on_response(
        self, status: int, headers: Mapping[str, str], body: str, operation_name: str,
    ) -> SpotifyRateLimitInfo | None:
        if status != 429 or not self.continuous:
            return None
        received_at_monotonic, received_at_unix = self.monotonic(), self.wall_time()
        self.rate_limit_count += 1
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        kind = "quota_exceeded" if isinstance(error, dict) and error.get("reason") == "QUOTA_EXCEEDED" else "rate_limit"
        if kind == "quota_exceeded":
            self.quota_count += 1
        delay = parse_retry_after(next((value for key, value in headers.items() if key.lower() == "retry-after"), None))
        if not delay and kind == "rate_limit":
            self._fallback_count += 1
        self.minimum_interval_seconds = min(self.minimum_interval_seconds * 2, max(self.base_interval_seconds, 30.0))
        return SpotifyRateLimitInfo(kind, delay or None, received_at_monotonic, received_at_unix, operation_name)

    def pause_delay_seconds(self, info: SpotifyRateLimitInfo) -> float | None:
        if info.retry_after_seconds is not None:
            return info.retry_after_seconds
        if info.kind == "quota_exceeded":
            return None
        return min(60.0 * 2 ** min(max(self._fallback_count - 1, 0), 4), 900.0)

    def resume_at(self, info: SpotifyRateLimitInfo) -> str | None:
        delay = self.pause_delay_seconds(info)
        if delay is None:
            return None
        try:
            return datetime.fromtimestamp(info.received_at_unix + delay, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    def pause_description(self, info: SpotifyRateLimitInfo) -> str:
        delay = self.pause_delay_seconds(info)
        if delay is None:
            return "Spotify quota exceeded; reset time was not provided; resolve quota before restarting"
        remaining = max(0.0, delay - (self.monotonic() - info.received_at_monotonic))
        resume = self.resume_at(info)
        return (f"Spotify {info.kind} during {info.operation_name}; {remaining:g} seconds remaining"
                + (f"; resume at {resume} UTC" if resume else "; resume date outside calendar range"))

    def wait_for_pause(self, info: SpotifyRateLimitInfo) -> None:
        delay = self.pause_delay_seconds(info)
        if delay is None:
            raise ValueError("Spotify quota reset time was not provided")
        started = self.monotonic()
        self.notice(self.pause_description(info))
        next_notice = started + 60.0
        try:
            while True:
                now = self.monotonic()
                remaining = delay - (now - info.received_at_monotonic)
                if remaining <= 0:
                    return
                if now >= next_notice:
                    self.notice(self.pause_description(info))
                    next_notice = now + 60.0
                self.sleep(min(remaining, 60.0))
        finally:
            self.wait_seconds += max(0.0, self.monotonic() - started)

    def reset_fallback_after_progress(self) -> None:
        self._fallback_count = 0
