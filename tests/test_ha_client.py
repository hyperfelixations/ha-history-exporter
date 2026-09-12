from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from ha_history_exporter import ha_client
from ha_history_exporter.errors import AuthError, HAAPIError, HAConnectionError
from ha_history_exporter.ha_client import HomeAssistantClient
from tests.helpers import FakeResponse, SequenceGet, state_row, timeout_error

UTC = ZoneInfo("UTC")
BASE_URL = "http://home-assistant.invalid"
TOKEN = "synthetic-test-token"


def make_client(*, retries=0, backoff=None, sleep=None):
    """Build a client whose retry pauses are recorded instead of slept."""
    return HomeAssistantClient(
        BASE_URL,
        TOKEN,
        timeout=3,
        max_retries=retries,
        backoff_seconds=backoff or [0.1, 0.2],
        sleep=sleep if sleep is not None else (lambda _: None),
    )


def install_get(monkeypatch, client, responses):
    sequence = SequenceGet(responses)
    monkeypatch.setattr(client._session, "get", sequence)
    return sequence


def test_token_is_only_placed_in_authorization_header():
    client = make_client()

    assert client._base == BASE_URL
    assert client._session.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in client._base


def test_check_api_accepts_expected_message(monkeypatch):
    client = make_client()
    sequence = install_get(
        monkeypatch, client, [FakeResponse(payload={"message": "API running."})]
    )

    client.check_api()

    assert sequence.calls[0][0] == f"{BASE_URL}/api/"
    assert client.total_requests == 1


def test_check_api_rejects_unexpected_message(monkeypatch):
    client = make_client()
    marker = "synthetic-private-response-marker"
    install_get(monkeypatch, client, [FakeResponse(payload={"message": marker})])

    with pytest.raises(HAAPIError, match="Unexpected response contract") as exc:
        client.check_api()
    assert marker not in exc.value.summary


def test_check_api_requires_object(monkeypatch):
    client = make_client()
    install_get(monkeypatch, client, [FakeResponse(payload=["API running."])])

    with pytest.raises(HAAPIError, match="Expected object"):
        client.check_api()


def test_get_states_requires_list(monkeypatch):
    client = make_client()
    install_get(monkeypatch, client, [FakeResponse(payload={"not": "a list"})])

    with pytest.raises(HAAPIError, match="Expected list"):
        client.get_states()


def test_get_states_requires_state_objects(monkeypatch):
    client = make_client()
    install_get(monkeypatch, client, [FakeResponse(payload=[["not", "a", "state"]])])

    with pytest.raises(HAAPIError, match="state objects"):
        client.get_states()


def test_get_states_returns_synthetic_entities(monkeypatch):
    client = make_client()
    states = [state_row()]
    install_get(monkeypatch, client, [FakeResponse(payload=states)])
    assert client.get_states() == states


def test_get_history_builds_expected_path_and_options(monkeypatch):
    client = make_client()
    payload = [[state_row()]]
    sequence = install_get(monkeypatch, client, [FakeResponse(payload=payload)])
    start = datetime(2026, 7, 28, 0, tzinfo=UTC)
    end = datetime(2026, 7, 29, 0, tzinfo=UTC)

    result = client.get_history(
        start,
        end,
        ["sensor.one", "sensor.two"],
        minimal_response=True,
        no_attributes=True,
        significant_changes_only=True,
        skip_initial_state=True,
    )

    url, kwargs = sequence.calls[0]
    assert url == f"{BASE_URL}/api/history/period/{start.isoformat()}"
    assert kwargs["timeout"] == 3
    assert kwargs["params"] == {
        "end_time": end.isoformat(),
        "filter_entity_id": "sensor.one,sensor.two",
        "minimal_response": "1",
        "no_attributes": "1",
        "significant_changes_only": "1",
        "skip_initial_state": "1",
    }
    assert result == payload


@pytest.mark.parametrize(
    (
        "minimal_response",
        "no_attributes",
        "significant_changes_only",
        "skip_initial_state",
        "expected",
    ),
    [
        (False, False, False, False, {"significant_changes_only": "0"}),
        (False, False, True, False, {"significant_changes_only": "1"}),
        (
            True,
            True,
            False,
            True,
            {
                "minimal_response": "1",
                "no_attributes": "1",
                "significant_changes_only": "0",
                "skip_initial_state": "1",
            },
        ),
    ],
)
def test_get_history_serializes_the_home_assistant_query_contract(
    monkeypatch,
    minimal_response,
    no_attributes,
    significant_changes_only,
    skip_initial_state,
    expected,
):
    client = make_client()
    sequence = install_get(monkeypatch, client, [FakeResponse(payload=[])])
    start = datetime(2026, 7, 28, 0, tzinfo=UTC)
    end = datetime(2026, 7, 29, 0, tzinfo=UTC)

    client.get_history(
        start,
        end,
        ["sensor.one"],
        minimal_response=minimal_response,
        no_attributes=no_attributes,
        significant_changes_only=significant_changes_only,
        skip_initial_state=skip_initial_state,
    )

    params = sequence.calls[0][1]["params"]
    assert params == {
        "end_time": end.isoformat(),
        "filter_entity_id": "sensor.one",
        **expected,
    }


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_abort_without_retry(monkeypatch, status):
    sleeps: list[float] = []
    client = make_client(retries=3, sleep=sleeps.append)
    sequence = install_get(monkeypatch, client, [FakeResponse(status_code=status)])

    with pytest.raises(AuthError, match=f"HTTP {status}"):
        client.check_api()

    assert len(sequence.calls) == 1
    assert client.total_retries == 0
    assert sleeps == []


def test_retryable_status_retries_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    client = make_client(retries=2, backoff=[0.25, 0.5], sleep=sleeps.append)
    sequence = install_get(
        monkeypatch,
        client,
        [
            FakeResponse(status_code=503),
            FakeResponse(payload={"message": "API running."}),
        ],
    )

    client.check_api()

    assert len(sequence.calls) == 2
    assert client.total_requests == 2
    assert client.total_retries == 1
    assert sleeps == [0.25]


def test_network_timeout_exhaustion_raises_connection_error(monkeypatch):
    client = make_client(retries=1, backoff=[0])
    sequence = install_get(monkeypatch, client, [timeout_error(), timeout_error()])

    with pytest.raises(HAConnectionError) as exc:
        client.check_api()

    assert exc.value.summary == f"Cannot reach Home Assistant at {BASE_URL}."
    assert "after 2 attempt(s)" in (exc.value.details or "")
    assert "Timeout" in (exc.value.details or "")
    assert len(sequence.calls) == 2
    assert client.total_retries == 1
    rendered = " ".join(
        [
            exc.value.summary,
            exc.value.details or "",
            *exc.value.context.values(),
            *(remedy.command or "" for remedy in exc.value.remedies),
        ]
    )
    assert exc.value.context["url"] == BASE_URL
    assert f"curl -sS {BASE_URL}/api/" in rendered


def test_non_retryable_http_error_is_immediate(monkeypatch):
    client = make_client(retries=3)
    sequence = install_get(
        monkeypatch,
        client,
        [FakeResponse(status_code=400, text="synthetic bad request")],
    )

    with pytest.raises(HAAPIError, match="HTTP 400"):
        client.check_api()

    assert len(sequence.calls) == 1


def test_http_error_never_exposes_the_response_body(monkeypatch, caplog):
    """A response body may hold private content and must not be reported."""
    import logging

    client = make_client(retries=0)
    install_get(
        monkeypatch,
        client,
        [FakeResponse(status_code=400, text="synthetic-private-body-marker")],
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(HAAPIError) as exc:
        client.check_api()

    rendered = " ".join(
        [
            exc.value.summary,
            exc.value.details or "",
            *exc.value.context.values(),
            *(remedy.description for remedy in exc.value.remedies),
            caplog.text,
        ]
    )
    assert "synthetic-private-body-marker" not in rendered


def test_context_manager_closes_session(monkeypatch):
    client = make_client()
    closed = []
    monkeypatch.setattr(client._session, "close", lambda: closed.append(True))

    with client as active:
        assert active is client

    assert closed == [True]


def test_requests_connection_error_is_retried(monkeypatch):
    client = make_client(retries=1, backoff=[0])
    sequence = install_get(
        monkeypatch,
        client,
        [
            requests.ConnectionError("synthetic disconnect"),
            FakeResponse(payload={"message": "API running."}),
        ],
    )

    client.check_api()

    assert len(sequence.calls) == 2
    assert client.total_retries == 1


def test_invalid_json_is_reported_as_api_error(monkeypatch):
    client = make_client()
    decode_error = json.JSONDecodeError("synthetic", "x", 0)
    install_get(
        monkeypatch,
        client,
        [FakeResponse(json_error=decode_error)],
    )

    with pytest.raises(HAAPIError, match="JSON"):
        client.get_states()


def test_history_rejects_non_list_inner_payload(monkeypatch):
    client = make_client()
    install_get(
        monkeypatch,
        client,
        [FakeResponse(payload=[{"not": "an entity history list"}])],
    )

    with pytest.raises(HAAPIError, match="history"):
        client.get_history(
            datetime(2026, 7, 28, tzinfo=UTC),
            datetime(2026, 7, 29, tzinfo=UTC),
            ["sensor.one"],
        )


def test_history_rejects_non_object_state(monkeypatch):
    client = make_client()
    install_get(
        monkeypatch,
        client,
        [FakeResponse(payload=[["not a state object"]])],
    )

    with pytest.raises(HAAPIError, match="state objects"):
        client.get_history(
            datetime(2026, 7, 28, tzinfo=UTC),
            datetime(2026, 7, 29, tzinfo=UTC),
            ["sensor.one"],
        )


def test_retry_backoff_follows_the_configured_schedule(monkeypatch):
    """Retry timing is injectable, so the schedule is asserted without waiting."""
    waits: list[float] = []
    client = ha_client.HomeAssistantClient(
        url="http://home-assistant.invalid",
        token="synthetic-test-token",
        max_retries=3,
        backoff_seconds=[2, 5, 15],
        sleep=waits.append,
    )
    install_get(
        monkeypatch,
        client,
        [FakeResponse(status_code=503) for _ in range(4)],
    )

    with pytest.raises(HAConnectionError):
        client.check_api()

    assert waits == [2, 5, 15]
    assert client.total_retries == 3


def test_backoff_schedule_repeats_its_last_value(monkeypatch):
    waits: list[float] = []
    client = ha_client.HomeAssistantClient(
        url="http://home-assistant.invalid",
        token="synthetic-test-token",
        max_retries=3,
        backoff_seconds=[1],
        sleep=waits.append,
    )
    install_get(
        monkeypatch,
        client,
        [FakeResponse(status_code=500) for _ in range(4)],
    )

    with pytest.raises(HAConnectionError):
        client.check_api()

    assert waits == [1, 1, 1]


def test_an_injected_session_is_used(monkeypatch):
    import requests

    session = requests.Session()
    client = ha_client.HomeAssistantClient(
        url="http://home-assistant.invalid",
        token="synthetic-test-token",
        session=session,
    )

    assert client._session is session
    assert session.headers["Authorization"].startswith("Bearer ")
