import math
import sys
import unittest
from pathlib import Path


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from publishers.spotify.request_control import (  # noqa: E402
    SpotifyRequestControl,
    normalize_request_interval_seconds,
    parse_retry_after,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class SpotifyRequestControlTests(unittest.TestCase):
    def test_waiting_does_not_record_a_request(self):
        clock = FakeClock()
        control = SpotifyRequestControl(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
        control.before_request()
        self.assertEqual(control.request_count, 0)
        self.assertEqual(clock.sleeps, [])
        control.record_request_start()
        control.before_request()
        self.assertEqual(clock.now, 1.0)
        self.assertEqual(control.request_count, 1)

    def test_actual_dispatch_after_slow_preparation_sets_the_next_slot(self):
        clock = FakeClock()
        control = SpotifyRequestControl(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
        control.record_request_start()
        control.before_request()
        clock.now += 2.0
        control.record_request_start()
        self.assertEqual(clock.now, 3.0)
        control.before_request()
        control.record_request_start()
        self.assertEqual(clock.now, 4.0)
        self.assertEqual(control.request_count, 3)

    def test_elapsed_transport_time_satisfies_the_interval(self):
        clock = FakeClock()
        control = SpotifyRequestControl(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
        control.record_request_start()
        clock.now = 2.0
        control.before_request()
        self.assertEqual(clock.sleeps, [])

    def test_early_sleep_return_rechecks_remaining_time(self):
        clock = FakeClock()

        def sleep(seconds: float) -> None:
            clock.sleeps.append(seconds)
            clock.now += 0.25 if len(clock.sleeps) == 1 else seconds

        control = SpotifyRequestControl(1.0, monotonic=clock.monotonic, sleep=sleep)
        control.record_request_start()
        control.before_request()
        self.assertEqual(clock.sleeps, [1.0, 0.75])
        self.assertEqual(clock.now, 1.0)

    def test_huge_finite_interval_is_interruptible_without_overflow(self):
        clock = FakeClock()

        def interrupt(seconds: float) -> None:
            clock.sleeps.append(seconds)
            raise KeyboardInterrupt

        control = SpotifyRequestControl(1e308, monotonic=clock.monotonic, sleep=interrupt)
        control.record_request_start()
        with self.assertRaises(KeyboardInterrupt):
            control.before_request()
        self.assertEqual(clock.sleeps, [60.0])
        self.assertEqual(control.request_count, 1)

    def test_zero_interval_never_sleeps(self):
        clock = FakeClock()
        control = SpotifyRequestControl(monotonic=clock.monotonic, sleep=clock.sleep)
        for _ in range(3):
            control.before_request()
            control.record_request_start()
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(control.request_count, 3)

    def test_request_interval_requires_finite_nonnegative_seconds(self):
        for value in (-1.0, math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SpotifyRequestControl(value)
        self.assertEqual(normalize_request_interval_seconds(None), 0.0)
        self.assertEqual(normalize_request_interval_seconds(0.0), 0.0)
        self.assertEqual(normalize_request_interval_seconds(0.5), 0.5)

    def test_retry_after_requires_finite_positive_seconds(self):
        for value in (None, "", "0", "-1", "nan", "inf", "-inf", "1e309", "not-seconds"):
            with self.subTest(value=value):
                self.assertEqual(parse_retry_after(value), 0.0)
        self.assertEqual(parse_retry_after(" 0.5 "), 0.5)
        self.assertEqual(parse_retry_after("480"), 480.0)


class SpotifyContinuousControlTests(unittest.TestCase):
    def make_control(self, interval=1.0):
        clock = FakeClock()
        notices = []
        control = SpotifyRequestControl(interval, continuous=True, monotonic=clock.monotonic,
                                        wall_time=lambda: 1700000000 + clock.now,
                                        sleep=clock.sleep, notice=notices.append)
        return control, clock, notices

    def test_receipt_deadline_deducts_checkpoint_time_and_honors_hours(self):
        control, clock, notices = self.make_control()
        info = control.on_response(429, {"rEtRy-AfTeR": "4"}, "{}", "spotify_search")
        assert info is not None
        assert info is not None
        clock.now = 2.0
        control.wait_for_pause(info)
        self.assertEqual(clock.now, 4.0)
        self.assertEqual(control.wait_seconds, 2.0)
        self.assertIn("2023-11-14T22:13:24", notices[0])
        info = control.on_response(429, {"Retry-After": "7200"}, "{}", "spotify_search")
        assert info is not None
        control.wait_for_pause(info)
        self.assertEqual(clock.now, 7204.0)
        self.assertEqual(control.wait_seconds, 7202.0)
        self.assertLessEqual(max(clock.sleeps), 60)
        self.assertEqual(control.minimum_interval_seconds, 4.0)
        self.assertEqual(len(notices), 121)

    def test_quota_exact_shape_and_unusable_headers(self):
        for body in ('[]', 'null', '0', '"QUOTA_EXCEEDED"', '{', '{"reason":"QUOTA_EXCEEDED"}',
                     '{"error":[]}', '{"error":{"reason":"quota_exceeded"}}'):
            control, clock, _ = self.make_control()
            info = control.on_response(429, {}, body, "spotify_search")
            assert info is not None
            self.assertEqual(info.kind, "rate_limit")
            control.wait_for_pause(info)
            self.assertEqual(clock.now, 60.0)
        for header in (None, "0", "-1", "nan", "inf", "garbage"):
            control, clock, _ = self.make_control()
            info = control.on_response(429, {} if header is None else {"Retry-After": header},
                                       '{"error":{"reason":"QUOTA_EXCEEDED"}}', "spotify_search")
            assert info is not None
            self.assertEqual(info.kind, "quota_exceeded")
            self.assertIsNone(info.retry_after_seconds)
            with self.assertRaises(ValueError):
                control.wait_for_pause(info)
            self.assertEqual(clock.sleeps, [])
            self.assertEqual(control.quota_count, 1)

    def test_fallback_escalation_reset_and_adaptive_interval(self):
        control, clock, _ = self.make_control()
        ends = []
        for _ in range(6):
            control.on_response(200, {}, "{}", "spotify_playlist_list")
            info = control.on_response(429, {}, "{}", "spotify_search")
            assert info is not None
            control.wait_for_pause(info)
            ends.append(clock.now)
        self.assertEqual(ends, [60, 180, 420, 900, 1800, 2700])
        self.assertEqual(control.minimum_interval_seconds, 30.0)
        control.reset_fallback_after_progress()
        info = control.on_response(429, {}, "{}", "spotify_search")
        assert info is not None
        control.wait_for_pause(info)
        self.assertEqual(clock.now, 2760)
        self.assertEqual(control.minimum_interval_seconds, 30)
        self.assertEqual(control.rate_limit_count, 7)

    def test_huge_delay_does_not_overflow_and_interrupt_counts_elapsed_wait(self):
        control, clock, notices = self.make_control(40.0)
        def interrupt(seconds):
            self.assertEqual(seconds, 60)
            clock.now += 3
            raise KeyboardInterrupt
        control.sleep = interrupt
        info = control.on_response(429, {"Retry-After": "1e308"}, "{}", "spotify_search")
        assert info is not None
        with self.assertRaises(KeyboardInterrupt):
            control.wait_for_pause(info)
        self.assertEqual(control.minimum_interval_seconds, 40.0)
        self.assertEqual(control.wait_seconds, 3)
        self.assertIn("1e+308", notices[0])
        self.assertNotIn("UTC", notices[0])


if __name__ == "__main__":
    unittest.main()
