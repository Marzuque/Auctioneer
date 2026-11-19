import random

from spv_scraper.pacer import Pacer


def make(**kw):
    kw.setdefault("rng", random.Random(1))
    return Pacer(**kw)


def test_backoff_doubles_and_caps():
    p = make()
    seq = [p.record_failure() for _ in range(7)]
    assert seq == [30, 60, 120, 240, 480, 900, 900]


def test_retry_after_is_honoured_and_capped():
    p = make()
    assert p.record_failure(retry_after=600) == 600
    p2 = make()
    assert p2.record_failure(retry_after=99999) == 3600


def test_trips_after_consecutive_failures_and_success_resets():
    p = make(max_consecutive_failures=5)
    for _ in range(4):
        p.record_failure()
    assert not p.tripped
    p.record_success(0.5)
    assert p.consecutive_failures == 0
    for _ in range(5):
        p.record_failure()
    assert p.tripped
    p.reset_failures()
    assert not p.tripped


def test_delay_respects_floor_and_jitter_bounds():
    p = make(base_delay=0.1, floor=0.75)
    assert p.base_delay == 0.75
    p = make(base_delay=2.0, jitter=0.3)
    for _ in range(200):
        d, why = p.next_delay()
        assert 1.4 <= d <= 2.6 and why is None


def test_slow_site_doubles_then_quadruples_delay():
    p = make(base_delay=1.0, jitter=0, slow_latency=3.0)
    for _ in range(5):
        p.record_success(4.0)
    d, why = p.next_delay()
    assert d == 2.0 and "slow" in why
    for _ in range(20):
        p.record_success(7.0)
    assert p.next_delay()[0] == 4.0


def test_scheduled_break_once_per_block():
    p = make(base_delay=1.0, jitter=0, break_every=3, break_seconds=60)
    for _ in range(3):
        p.record_success(0.5)
    d, why = p.next_delay()
    assert 48 + 1 <= d <= 72 + 1 and "break" in why
    d, why = p.next_delay()  # same count: no second break
    assert d == 1.0 and why is None
