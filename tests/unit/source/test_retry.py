from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ha_history_exporter.source.retry import RETRY_AFTER_CAP, RetryPolicy, parse_retry_after

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def test_the_backoff_schedule_repeats_its_last_wait() -> None:
    policy = RetryPolicy(max_retries=5, backoff=(2.0, 5.0, 15.0))
    assert [policy.delay(retry) for retry in range(5)] == [2.0, 5.0, 15.0, 15.0, 15.0]


def test_the_server_decides_the_wait_up_to_the_cap() -> None:
    policy = RetryPolicy(max_retries=3, backoff=(2.0,))
    assert policy.delay(0, retry_after=7.0) == 7.0
    assert policy.delay(0, retry_after=0.0) == 0.0
    assert policy.delay(0, retry_after=3600.0) == RETRY_AFTER_CAP


def test_retries_need_a_schedule() -> None:
    with pytest.raises(ValueError, match="backoff"):
        RetryPolicy(max_retries=1, backoff=())
    assert RetryPolicy(max_retries=0, backoff=()).max_retries == 0


@given(
    st.lists(st.floats(0, 1000), min_size=1, max_size=5),
    st.integers(0, 20),
    st.none() | st.floats(-100, 10_000),
)
def test_a_wait_is_never_negative_nor_beyond_the_cap_for_the_server(
    backoff: list[float], retry: int, retry_after: float | None
) -> None:
    delay = RetryPolicy(max_retries=20, backoff=tuple(backoff)).delay(retry, retry_after)
    if retry_after is None:
        assert delay in backoff
    else:
        assert 0.0 <= delay <= RETRY_AFTER_CAP


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        (None, None),
        ("120", 120.0),
        (" 0 ", 0.0),
        ("Tue, 27 Oct 2026 12:01:30 GMT", 90.0),
        ("Tue, 27 Oct 2026 11:00:00 GMT", 0.0),
        ("soon", None),
        ("-5", None),
    ],
)
def test_retry_after_is_seconds_or_a_date(header: str | None, seconds: float | None) -> None:
    assert parse_retry_after(header, NOW) == seconds
