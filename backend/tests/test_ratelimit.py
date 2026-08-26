"""Rate limiting: the layer that stands between a public URL and the Maps bill."""
import pytest

from app.ratelimit import GLOBAL_KEY, Rule, SlidingWindowLimiter, client_key


def test_allows_up_to_the_limit_then_refuses():
    limiter, rule = SlidingWindowLimiter(), Rule(3, 60)
    assert [limiter.check("ip", rule, now=1000) for _ in range(3)] == [None, None, None]
    assert limiter.check("ip", rule, now=1000) is not None


def test_window_slides_rather_than_resetting_on_a_boundary():
    limiter, rule = SlidingWindowLimiter(), Rule(2, 60)
    limiter.check("ip", rule, now=1000)
    limiter.check("ip", rule, now=1030)
    assert limiter.check("ip", rule, now=1040) is not None
    # The first hit ages out at 1060, freeing exactly one slot.
    assert limiter.check("ip", rule, now=1061) is None
    assert limiter.check("ip", rule, now=1061) is not None


def test_retry_after_points_at_when_a_slot_frees_up():
    limiter, rule = SlidingWindowLimiter(), Rule(1, 60)
    limiter.check("ip", rule, now=1000)
    retry = limiter.check("ip", rule, now=1010)
    assert 45 <= retry <= 61


def test_clients_are_limited_independently():
    limiter, rule = SlidingWindowLimiter(), Rule(1, 60)
    assert limiter.check("a", rule, now=1000) is None
    assert limiter.check("b", rule, now=1000) is None, "b must not inherit a's usage"


def test_global_key_is_shared_across_clients():
    """Per-client limits do nothing against many clients, so the global cap is
    what actually bounds the worst case."""
    limiter, rule = SlidingWindowLimiter(), Rule(2, 86_400)
    assert limiter.check(GLOBAL_KEY, rule, now=1000) is None
    assert limiter.check(GLOBAL_KEY, rule, now=1000) is None
    assert limiter.check(GLOBAL_KEY, rule, now=1000) is not None


def test_peek_does_not_consume_a_slot():
    limiter, rule = SlidingWindowLimiter(), Rule(2, 60)
    limiter.check("ip", rule, now=1000)
    assert limiter.peek("ip", rule, now=1000) == 1
    assert limiter.peek("ip", rule, now=1000) == 1
    assert limiter.check("ip", rule, now=1000) is None


def test_sweep_drops_only_stale_keys():
    limiter, rule = SlidingWindowLimiter(), Rule(5, 60)
    limiter.check("old", rule, now=1000)
    limiter.check("fresh", rule, now=90_000)
    assert limiter.sweep(86_400, now=90_000) == 1
    assert limiter.peek("fresh", rule, now=90_000) == 1


@pytest.mark.parametrize(
    "forwarded,host,trust,expected",
    [
        ("203.0.113.9, 10.0.0.1", "10.0.0.1", True, "203.0.113.9"),
        # Spoofable when not actually behind a proxy, so it must be ignored.
        ("203.0.113.9", "198.51.100.4", False, "198.51.100.4"),
        (None, "198.51.100.4", True, "198.51.100.4"),
        ("", "198.51.100.4", True, "198.51.100.4"),
        (None, None, False, "unknown"),
    ],
)
def test_client_key_only_trusts_the_proxy_header_when_told_to(forwarded, host, trust, expected):
    assert client_key(forwarded, host, trust) == expected


def test_rule_describes_its_window_in_plain_words():
    assert Rule(12, 3600).describe() == "12 per hour"
    assert Rule(40, 86_400).describe() == "40 per day"
    assert Rule(60, 60).describe() == "60 per minute"
