from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable

import requests

from ha_history_exporter.config import AppConfig
from ha_history_exporter.planner import DayDecision, ExportPlan


def make_config(
    root: Path,
    *,
    jsonl: bool = True,
    csv: bool = False,
    parquet: bool = False,
    batch_size: int = 2,
) -> AppConfig:
    cfg = AppConfig()
    cfg.ha_url = "http://home-assistant.invalid"
    cfg.ha_token = "synthetic-test-token"
    cfg.export.output_dir = str(root / "output")
    cfg.storage.temp_dir = str(root / "temp")
    cfg.storage.cloud_storage_retry_count = 0
    cfg.storage.cloud_storage_retry_sleep_seconds = 0
    cfg.requests.batch_size_entities = batch_size
    cfg.requests.sleep_between_requests_seconds = 0
    cfg.requests.sleep_between_days_seconds = 0
    cfg.requests.max_retries = 0
    cfg.formats.jsonl = jsonl
    cfg.formats.csv = csv
    cfg.formats.parquet = parquet
    return cfg


def make_export_plan(day: date) -> ExportPlan:
    return ExportPlan(
        requested_start=day,
        requested_end=day,
        today=date(2026, 7, 30),
        latest_complete=date(2026, 7, 29),
        decisions=[DayDecision(day=day, action="export")],
    )


def state_row(
    entity_id: str = "sensor.test_temperature",
    state: str = "21.5",
    timestamp: str = "2026-07-28T10:00:00+00:00",
    *,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": timestamp,
        "last_updated": timestamp,
        "attributes": attributes
        if attributes is not None
        else {"friendly_name": "Synthetic temperature", "unit_of_measurement": "°C"},
    }


class FakeHomeAssistantClient:
    """In-memory client used by exporter integration tests."""

    def __init__(
        self,
        responses: Iterable[list[list[dict[str, Any]]] | Exception],
    ) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []
        self.total_requests = 0
        self.total_retries = 0

    def get_history(self, **kwargs):
        self.calls.append(kwargs)
        self.total_requests += 1
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        return response


class FakeCliClient:
    """In-memory stand-in for HomeAssistantClient at the CLI boundary.

    Class attributes configure the responses; ``reset()`` restores the defaults
    and is called by an autouse fixture before every test.
    """

    instances: ClassVar[list["FakeCliClient"]] = []
    check_error: ClassVar[Exception | None] = None
    states: ClassVar[list[dict[str, Any]]] = []
    history_payload: ClassVar[list[list[dict[str, Any]]]] = []

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.check_error = None
        cls.states = [
            {
                "entity_id": "sensor.synthetic",
                "state": "1",
                "attributes": {"friendly_name": "Synthetic"},
            }
        ]
        cls.history_payload = [[state_row("sensor.synthetic", "1")]]

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.total_requests = 0
        self.total_retries = 0
        self.history_calls: list[dict[str, Any]] = []
        self.closed = False
        type(self).instances.append(self)

    def __enter__(self) -> "FakeCliClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.closed = True

    def check_api(self) -> None:
        if self.check_error is not None:
            raise self.check_error

    def get_states(self) -> list[dict[str, Any]]:
        return list(type(self).states)

    def get_history(self, **kwargs: Any) -> list[list[dict[str, Any]]]:
        self.history_calls.append(kwargs)
        self.total_requests += 1
        return type(self).history_payload


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: Any = None
    text: str = ""
    json_error: Exception | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class SequenceGet:
    """Callable Session.get replacement that records calls."""

    def __init__(
        self,
        responses: Iterable[FakeResponse | Exception | Callable[..., FakeResponse]],
    ) -> None:
        self._responses = iter(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append((url, kwargs))
        response = next(self._responses)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(url, **kwargs)
        return response


def timeout_error() -> requests.Timeout:
    return requests.Timeout("synthetic timeout")
