from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from ha_history_exporter import ha_client
from ha_history_exporter.exceptions import AuthError, HAAPIError, HAConnectionError
from ha_history_exporter.ha_client import HomeAssistantClient
from tests.helpers import FakeResponse, SequenceGet, state_row, timeout_error


UTC = ZoneInfo("UTC")
BASE_URL = "http://home-assistant.invalid"
TOKEN = "synthetic-test-token"


def make_client(*, retries=0, backoff=None):
    return HomeAssistantClient(
        BASE_URL,
        TOKEN,
        timeout=3,
        max_retries=retries,
        backoff_seconds=backoff or [0.1, 0.2],
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
    install_get(monkeypatch, client, [FakeResponse(payload={"message": "wrong"})])

    with pytest.raises(HAAPIError, match="Unexpected /api/ response"):
        client.check_api()


def test_get_states_requires_list(monkeypatch):
    client = make_client()
    install_get(monkeypatch, client, [FakeResponse(payload={"not": "a list"})])

    with pytest.raises(HAAPIError, match="Expected list"):
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
    )

    url, kwargs = sequence.calls[0]
    assert url == f"{BASE_URL}/api/history/period/{start.isoformat()}"
    assert kwargs["timeout"] == 3
    assert kwargs["params"] == {
        "end_time": end.isoformat(),
        "filter_entity_id": "sensor.one,sensor.two",
        "minimal_response": "true",
        "no_attributes": "true",
        "significant_changes_only": "true",
    }
    assert result == payload


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_abort_without_retry(monkeypatch, status):
    client = make_client(retries=3)
    sequence = install_get(monkeypatch, client, [FakeResponse(status_code=status)])
    sleeps = []
    monkeypatch.setattr(ha_client.time, "sleep", sleeps.append)

    with pytest.raises(AuthError, match=f"HTTP {status}"):
        client.check_api()

    assert len(sequence.calls) == 1
    assert client.total_retries == 0
    assert sleeps == []


def test_retryable_status_retries_then_succeeds(monkeypatch):
    client = make_client(retries=2, backoff=[0.25, 0.5])
    sequence = install_get(
        monkeypatch,
        client,
        [
            FakeResponse(status_code=503),
            FakeResponse(payload={"message": "API running."}),
        ],
    )
    sleeps = []
    monkeypatch.setattr(ha_client.time, "sleep", sleeps.append)

    client.check_api()

    assert len(sequence.calls) == 2
    assert client.total_requests == 2
    assert client.total_retries == 1
    assert sleeps == [0.25]


def test_network_timeout_exhaustion_raises_connection_error(monkeypatch):
    client = make_client(retries=1, backoff=[0])
    sequence = install_get(monkeypatch, client, [timeout_error(), timeout_error()])
    monkeypatch.setattr(ha_client.time, "sleep", lambda _: None)

    with pytest.raises(HAConnectionError, match="after 2 attempt"):
        client.check_api()

    assert len(sequence.calls) == 2
    assert client.total_retries == 1


def test_non_retryable_http_error_is_immediate(monkeypatch):
    client = make_client(retries=3)
    sequence = install_get(
        monkeypatch,
        client,
        [FakeResponse(status_code=400, text="synthetic bad request")],
    )

    with pytest.raises(HAAPIError, match="synthetic bad request"):
        client.check_api()

    assert len(sequence.calls) == 1


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
    monkeypatch.setattr(ha_client.time, "sleep", lambda _: None)

    client.check_api()

    assert len(sequence.calls) == 2
    assert client.total_retries == 1


@pytest.mark.known_bug
@pytest.mark.xfail(
    strict=True,
    reason="JSON decoding failures are not wrapped in HAAPIError",
)
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


@pytest.mark.known_bug
@pytest.mark.xfail(
    strict=True,
    reason="history response validates only the outer list",
)
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
