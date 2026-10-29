"""The REST source against a scripted transport: classification, retries, secrecy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from ha_history_exporter.domain.query import HistoryQuery
from ha_history_exporter.domain.window import DayWindow
from ha_history_exporter.errors import (
    AuthError,
    HAAPIError,
    HAConnectionError,
    HHEError,
    TLSError,
)
from ha_history_exporter.source.protocol import HistoryCall, HistoryFetchError
from ha_history_exporter.source.rest import USER_AGENT, RestHistorySource, history_params
from ha_history_exporter.source.retry import RetryPolicy

TOKEN = "synthetic-test-token"
URL = "http://home-assistant.invalid:8123"
WINDOW = DayWindow.full(date(2026, 7, 28), ZoneInfo("Europe/Berlin"))
NOW = datetime(2026, 7, 29, 6, 0, tzinfo=UTC)


@dataclass
class Reply:
    status: int = 200
    body: object = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None
    broken: bool = False

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def content(self) -> bytes:
        if self.broken:
            raise requests.exceptions.ChunkedEncodingError("synthetic broken transfer")
        if self.raw is not None:
            return self.raw
        return json.dumps(self.body).encode("utf-8")


@dataclass
class Call:
    url: str
    params: Mapping[str, str] | None
    headers: Mapping[str, str]
    timeout: tuple[float, float]
    verify: bool | str
    allow_redirects: bool


class ScriptedSession:
    """Answers each request with the next scripted reply or exception."""

    def __init__(self, *script: Reply | Exception) -> None:
        self.script = list(script)
        self.calls: list[Call] = []
        self.closed = False

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: tuple[float, float],
        verify: bool | str,
        allow_redirects: bool,
    ) -> Reply:
        self.calls.append(Call(url, params, dict(headers), timeout, verify, allow_redirects))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def close(self) -> None:
        self.closed = True


class FixedClock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0.0


def source(
    session: ScriptedSession,
    *,
    retries: int = 3,
    timeout: float = 120,
    ca_bundle: Path | None = None,
    sleeps: list[float] | None = None,
) -> RestHistorySource:
    waited = sleeps if sleeps is not None else []
    return RestHistorySource(
        URL,
        TOKEN,
        ca_bundle=ca_bundle,
        timeout=timeout,
        retry=RetryPolicy(max_retries=retries, backoff=(2.0, 5.0, 15.0)),
        session=session,
        sleep=waited.append,
        clock=FixedClock(),
    )


def history(rest: RestHistorySource) -> HistoryCall:
    return rest.history(WINDOW, ["sensor.one", "sensor.two"], HistoryQuery())


def test_requests_carry_the_token_in_the_header_only() -> None:
    session = ScriptedSession(Reply(body={"message": "API running."}))
    source(session).check_api()
    call = session.calls[0]
    assert call.url == f"{URL}/api/"
    assert call.headers == {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    assert call.allow_redirects is False
    assert call.verify is True
    assert call.timeout == (10.0, 120)


def test_the_connect_timeout_never_exceeds_the_read_timeout_and_a_ca_bundle_verifies(
    tmp_path: Path,
) -> None:
    session = ScriptedSession(Reply(body={"message": "API running."}))
    source(session, timeout=5, ca_bundle=tmp_path / "ca.pem").check_api()
    assert session.calls[0].timeout == (5, 5)
    assert session.calls[0].verify == str(tmp_path / "ca.pem")


def test_a_history_request_asks_from_one_microsecond_before_the_day() -> None:
    session = ScriptedSession(Reply(body=[[{"entity_id": "sensor.one"}]]))
    call = history(source(session))
    request = session.calls[0]
    assert request.url == f"{URL}/api/history/period/2026-07-27T21:59:59.999999%2B00:00"
    assert request.params == {
        "end_time": "2026-07-28T22:00:00+00:00",
        "filter_entity_id": "sensor.one,sensor.two",
        "significant_changes_only": "0",
        "skip_initial_state": "1",
    }
    assert call.payload == [[{"entity_id": "sensor.one"}]]


def test_history_options_are_sent_the_way_home_assistant_reads_them() -> None:
    reduced = HistoryQuery(
        minimal_response=True,
        no_attributes=True,
        significant_changes_only=True,
        skip_initial_state=False,
    )
    params = history_params(WINDOW, ["sensor.one"], reduced)
    assert params["significant_changes_only"] == "1"
    assert params["minimal_response"] == params["no_attributes"] == "1"
    assert "skip_initial_state" not in params


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_never_retried(status: int) -> None:
    sleeps: list[float] = []
    with pytest.raises(AuthError, match=f"HTTP {status}"):
        source(ScriptedSession(Reply(status)), sleeps=sleeps).check_api()
    assert sleeps == []


def test_an_unverifiable_certificate_is_never_retried() -> None:
    session = ScriptedSession(requests.exceptions.SSLError("synthetic certificate failure"))
    with pytest.raises(TLSError) as caught:
        source(session).check_api()
    assert len(session.calls) == 1
    assert "homeassistant.ca_bundle" in (caught.value.remedies[0].command or "")


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_an_unavailable_instance_is_retried_then_reported_as_unavailable(status: int) -> None:
    sleeps: list[float] = []
    session = ScriptedSession(*[Reply(status)] * 4)
    with pytest.raises(HAConnectionError, match="not available") as caught:
        source(session, sleeps=sleeps).check_api()
    assert sleeps == [2.0, 5.0, 15.0]
    assert f"HTTP {status} on every one of 4 attempts" in (caught.value.details or "")


def test_lost_connections_and_broken_transfers_are_retried() -> None:
    session = ScriptedSession(
        requests.exceptions.ConnectionError("synthetic refused"),
        Reply(broken=True),
        Reply(body={"message": "API running."}),
    )
    source(session).check_api()
    assert len(session.calls) == 3


def test_a_timeout_says_how_to_give_more_time() -> None:
    session = ScriptedSession(*[requests.exceptions.ReadTimeout("synthetic timeout")] * 2)
    with pytest.raises(HAConnectionError, match="did not answer in time") as caught:
        source(session, retries=1).check_api()
    assert "--timeout" in (caught.value.remedies[0].command or "")


def test_an_internal_server_error_is_retried_then_fails_the_request() -> None:
    session = ScriptedSession(Reply(500), Reply(500))
    with pytest.raises(HAAPIError, match="HTTP 500"):
        source(session, retries=1).check_api()
    assert len(session.calls) == 2


@pytest.mark.parametrize(
    ("reply", "details"),
    [
        (Reply(404), "base URL"),
        (Reply(302, headers={"Location": "https://elsewhere.invalid/"}), "redirect"),
        (Reply(400), "not shown"),
        (Reply(raw=b"<html>synthetic body</html>"), "not shown"),
    ],
)
def test_other_answers_fail_at_once_without_quoting_the_body(reply: Reply, details: str) -> None:
    session = ScriptedSession(reply)
    with pytest.raises(HAAPIError) as caught:
        source(session).check_api()
    assert len(session.calls) == 1
    assert details in (caught.value.details or "")
    assert "synthetic body" not in f"{caught.value.summary} {caught.value.details}"


def test_the_server_sets_the_wait_before_a_retry() -> None:
    sleeps: list[float] = []
    session = ScriptedSession(
        Reply(429, headers={"Retry-After": "7"}),
        Reply(503, headers={"Retry-After": "Wed, 29 Jul 2026 06:01:30 GMT"}),
        Reply(429, headers={"Retry-After": "86400"}),
        Reply(body={"message": "API running."}),
    )
    source(session, sleeps=sleeps).check_api()
    assert sleeps == [7.0, 90.0, 300.0]


def test_a_history_call_reports_its_retries_also_when_it_fails() -> None:
    success = history(source(ScriptedSession(Reply(503), Reply(503), Reply(body=[]))))
    assert success.stats.retries == 2
    with pytest.raises(HistoryFetchError) as caught:
        history(source(ScriptedSession(*[Reply(503)] * 4)))
    assert caught.value.stats.attempts == 4
    assert caught.value.stats.retries == 3
    assert isinstance(caught.value.error, HAConnectionError)


@pytest.mark.parametrize(
    "reply",
    [Reply(raw=b"{"), Reply(body={"not": "a list"}), Reply(body=[[1]]), Reply(body=[{}])],
)
def test_an_unusable_history_answer_fails_the_call_with_its_stats(reply: Reply) -> None:
    with pytest.raises(HistoryFetchError) as caught:
        history(source(ScriptedSession(reply)))
    assert isinstance(caught.value.error, HAAPIError)
    assert caught.value.stats.attempts == 1


def test_the_instance_configuration_names_its_time_zone() -> None:
    session = ScriptedSession(Reply(body={"time_zone": "Europe/Berlin", "version": "2026.10.1"}))
    config = source(session).instance_config()
    assert config.time_zone == ZoneInfo("Europe/Berlin")
    assert config.version == "2026.10.1"


@pytest.mark.parametrize(
    "body",
    [{"time_zone": "Mars/Olympus", "version": "x"}, {"version": "x"}, ["Europe/Berlin"]],
)
def test_an_unusable_instance_configuration_is_reported(body: object) -> None:
    with pytest.raises(HAAPIError):
        source(ScriptedSession(Reply(body=body))).instance_config()


def test_states_are_a_list_of_objects() -> None:
    states = [{"entity_id": "sensor.one", "state": "1"}]
    assert source(ScriptedSession(Reply(body=states))).states() == states
    with pytest.raises(HAAPIError, match="unexpected structure"):
        source(ScriptedSession(Reply(body=[1]))).states()
    with pytest.raises(HAAPIError, match="unexpected structure"):
        source(ScriptedSession(Reply(body={"message": "x"}))).check_api()


def test_the_token_never_reaches_a_url_a_log_line_or_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    script: list[Reply | Exception] = [
        Reply(503),
        requests.exceptions.ConnectionError(f"synthetic failure {TOKEN}"),
        Reply(401),
    ]
    session = ScriptedSession(*script)
    with caplog.at_level("DEBUG"), pytest.raises(HHEError) as caught:
        source(session).check_api()
    error = caught.value
    assert TOKEN not in caplog.text
    assert TOKEN not in f"{error.summary} {error.details} {error.context} {error.remedies}"
    assert all(TOKEN not in call.url for call in session.calls)


def test_closing_the_source_closes_the_session() -> None:
    session = ScriptedSession()
    source(session).close()
    assert session.closed
