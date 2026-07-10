"""Shared Discogs API request helpers."""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DISCOGS_API_ROOT = "https://api.discogs.com"
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.0
DISCOGS_RATE_LIMIT_WINDOW_SECONDS = 60.0
DISCOGS_RATE_LIMIT_SAFETY_MARGIN = 2
DISCOGS_AUTHENTICATED_RATE_LIMIT = 60
DISCOGS_UNAUTHENTICATED_RATE_LIMIT = 25
MAX_RETRIES = 3


class DiscogsRateLimiterProtocol(Protocol):
    def wait_before_request(self) -> None: ...

    def update_from_headers(self, headers: object) -> None: ...

    def sleep_for_retry_after(self, retry_after_seconds: float) -> None: ...


class DiscogsRateLimiter:
    def __init__(
        self,
        fallback_request_interval_seconds: float,
        initial_rate_limit: int | None = None,
        window_seconds: float = DISCOGS_RATE_LIMIT_WINDOW_SECONDS,
        safety_margin: int = DISCOGS_RATE_LIMIT_SAFETY_MARGIN,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.fallback_request_interval_seconds = max(0.0, fallback_request_interval_seconds)
        self.window_seconds = max(1.0, window_seconds)
        self.safety_margin = max(0, safety_margin)
        self.now = now
        self.sleep = sleep
        self.lock = threading.Lock()
        self.request_timestamps: deque[float] = deque()
        self.rate_limit = initial_rate_limit if initial_rate_limit and initial_rate_limit > 0 else None
        self.minimum_interval_seconds = self.calculate_minimum_interval()
        self.next_request_time = 0.0
        self.paused_until = 0.0

    def calculate_minimum_interval(self) -> float:
        if not self.rate_limit:
            return self.fallback_request_interval_seconds
        effective_limit = max(1, self.rate_limit - self.safety_margin)
        header_interval_seconds = self.window_seconds / effective_limit
        return max(self.fallback_request_interval_seconds, header_interval_seconds)

    def wait_before_request(self) -> None:
        while True:
            with self.lock:
                current_time = self.now()
                self.prune_old_timestamps(current_time)
                wait_seconds = self.next_wait_seconds(current_time)
                if wait_seconds <= 0:
                    self.request_timestamps.append(current_time)
                    self.next_request_time = current_time + self.minimum_interval_seconds
                    return
            self.sleep(wait_seconds)

    def next_wait_seconds(self, current_time: float) -> float:
        wait_seconds = 0.0
        if self.paused_until > current_time:
            wait_seconds = max(wait_seconds, self.paused_until - current_time)
        if self.next_request_time > current_time:
            wait_seconds = max(wait_seconds, self.next_request_time - current_time)

        if self.rate_limit:
            effective_limit = max(1, self.rate_limit - self.safety_margin)
            if len(self.request_timestamps) >= effective_limit:
                oldest_request_time = self.request_timestamps[0]
                wait_seconds = max(
                    wait_seconds,
                    oldest_request_time + self.window_seconds - current_time + 0.01,
                )

        return wait_seconds

    def prune_old_timestamps(self, current_time: float) -> None:
        oldest_allowed_time = current_time - self.window_seconds
        while self.request_timestamps and self.request_timestamps[0] <= oldest_allowed_time:
            self.request_timestamps.popleft()

    def update_from_headers(self, headers: object) -> None:
        rate_limit = parse_int_header(headers, "x-discogs-ratelimit")
        remaining = parse_int_header(headers, "x-discogs-ratelimit-remaining")
        with self.lock:
            if rate_limit and rate_limit > 0:
                self.rate_limit = rate_limit
                self.minimum_interval_seconds = self.calculate_minimum_interval()
            if remaining is not None and remaining <= self.safety_margin:
                current_time = self.now()
                if self.request_timestamps:
                    self.paused_until = max(
                        self.paused_until,
                        self.request_timestamps[0] + self.window_seconds + 0.25,
                    )
                else:
                    self.paused_until = max(self.paused_until, current_time + self.window_seconds)

    def sleep_for_retry_after(self, retry_after_seconds: float) -> None:
        retry_after_seconds = max(0.0, retry_after_seconds)
        with self.lock:
            self.paused_until = max(self.paused_until, self.now() + retry_after_seconds)
        self.sleep(retry_after_seconds)


def parse_int_header(headers: object, name: str) -> int | None:
    value = get_header_value(headers, name)
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def get_header_value(headers: object, name: str) -> object | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return value
        return getter(name.lower())
    if isinstance(headers, Mapping):
        return headers.get(name) or headers.get(name.lower())
    return None


def parse_retry_after_seconds(headers: object) -> float | None:
    value = get_header_value(headers, "Retry-After")
    if value is None:
        return None
    clean_value = str(value).strip()
    try:
        return max(0.0, float(clean_value))
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(clean_value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=dt.UTC)
        return max(0.0, (retry_at - dt.datetime.now(dt.UTC)).total_seconds())


def default_discogs_rate_limit(token: str) -> int:
    if token:
        return DISCOGS_AUTHENTICATED_RATE_LIMIT
    return DISCOGS_UNAUTHENTICATED_RATE_LIMIT


def make_http_json_getter(
    user_agent: str,
    token: str,
    timeout_seconds: int,
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
) -> Callable[[str], Mapping[str, object] | None]:
    rate_limiter = DiscogsRateLimiter(
        fallback_request_interval_seconds=request_interval_seconds,
        initial_rate_limit=default_discogs_rate_limit(token),
    )

    def get_json(url: str) -> Mapping[str, object] | None:
        body = http_get(
            url,
            user_agent=user_agent,
            token=token,
            timeout_seconds=timeout_seconds,
            accept="application/json",
            rate_limiter=rate_limiter,
        )
        if not body:
            return None
        return json.loads(body)

    return get_json


def http_get(
    url: str,
    user_agent: str,
    token: str,
    timeout_seconds: int,
    accept: str,
    rate_limiter: DiscogsRateLimiterProtocol | None = None,
) -> str | None:
    last_error = ""
    for attempt_number in range(1, MAX_RETRIES + 1):
        try:
            if rate_limiter:
                rate_limiter.wait_before_request()
            request = Request(url, headers=build_headers(user_agent=user_agent, token=token, accept=accept))
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                if rate_limiter:
                    rate_limiter.update_from_headers(response.headers)
                return body
        except HTTPError as error:
            last_error = f"HTTP {error.code}"
            error_headers = getattr(error, "headers", None)
            if rate_limiter and error_headers:
                rate_limiter.update_from_headers(error_headers)
            if error.code == 429:
                retry_after_seconds = parse_retry_after_seconds(error_headers) or 65
                if rate_limiter:
                    rate_limiter.sleep_for_retry_after(retry_after_seconds)
                else:
                    time.sleep(retry_after_seconds)
            elif 500 <= error.code < 600:
                time.sleep(5 * attempt_number)
            else:
                break
        except (TimeoutError, URLError) as error:
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(5 * attempt_number)
    raise RuntimeError(f"request failed for {url}: {last_error or 'unknown error'}")


def build_headers(user_agent: str, token: str, accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": user_agent}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    return headers
