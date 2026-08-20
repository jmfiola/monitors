import pytest
from monitor.timing import MAX_BACKOFF_SEC, next_backoff, with_jitter


def test_with_jitter_returns_base_at_the_midpoint() -> None:
    assert with_jitter(10, 20, lambda: 0.5) == 10


def test_with_jitter_subtracts_the_full_percentage_at_zero() -> None:
    # 20% of 10 = 2; rand 0 => -2
    assert with_jitter(10, 20, lambda: 0.0) == pytest.approx(8)


def test_with_jitter_adds_the_full_percentage_at_one() -> None:
    assert with_jitter(10, 20, lambda: 1.0) == pytest.approx(12)


def test_with_jitter_never_returns_below_one_second() -> None:
    assert with_jitter(1, 100, lambda: 0.0) >= 1


def test_next_backoff_doubles_the_current_delay() -> None:
    assert next_backoff(10, 10, 300) == 20


def test_next_backoff_caps_at_max() -> None:
    assert next_backoff(200, 10, 300) == 300


def test_next_backoff_starts_from_base_when_there_is_no_prior_backoff() -> None:
    assert next_backoff(0, 10, 300) == 10
    assert MAX_BACKOFF_SEC == 300
