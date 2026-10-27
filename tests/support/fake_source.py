"""An in-memory Home Assistant behind the :class:`HistorySource` protocol."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ha_history_exporter.domain.query import HistoryQuery
from ha_history_exporter.domain.records import JsonValue
from ha_history_exporter.domain.window import DayWindow
from ha_history_exporter.domain.wire import WirePayload
from ha_history_exporter.errors import HHEError
from ha_history_exporter.source.protocol import (
    CallStats,
    HistoryCall,
    HistoryFetchError,
    InstanceConfig,
    StateObject,
)


@dataclass(frozen=True)
class Retried:
    """An answer that took *attempts* attempts: a payload or a final error."""

    answer: WirePayload | HHEError
    attempts: int


type Answer = WirePayload | HHEError | Retried | BaseException


@dataclass(frozen=True)
class HistoryRequest:
    window: DayWindow
    entity_ids: tuple[str, ...]
    query: HistoryQuery


class FakeHistorySource:
    """Answers history requests from a script, in call order.

    A payload answers, an :class:`HHEError` fails the request after one attempt,
    any other exception escapes the source as a defect would.
    """

    def __init__(
        self,
        answers: Iterable[Answer] = (),
        *,
        states: Sequence[StateObject] = (),
        time_zone: str = "Europe/Berlin",
        version: str = "2026.10.0",
        api_error: HHEError | None = None,
    ) -> None:
        self._answers = list(answers)
        self._states = list(states)
        self._config = InstanceConfig(ZoneInfo(time_zone), version)
        self._api_error = api_error
        self.calls: list[HistoryRequest] = []
        self.closed = False

    def check_api(self) -> None:
        if self._api_error is not None:
            raise self._api_error

    def instance_config(self) -> InstanceConfig:
        return self._config

    def states(self) -> Sequence[StateObject]:
        return self._states

    def history(
        self, window: DayWindow, entity_ids: Sequence[str], query: HistoryQuery
    ) -> HistoryCall:
        self.calls.append(HistoryRequest(window, tuple(entity_ids), query))
        answer = self._answers.pop(0)
        attempts = 1
        if isinstance(answer, Retried):
            attempts = answer.attempts
            answer = answer.answer
        if isinstance(answer, HHEError):
            raise HistoryFetchError(answer, CallStats(attempts))
        if isinstance(answer, BaseException):
            raise answer
        return HistoryCall(answer, CallStats(attempts))

    def close(self) -> None:
        self.closed = True

    @property
    def unanswered(self) -> int:
        return len(self._answers)


class RecordedHistorySource(FakeHistorySource):
    """Home Assistant with a fixed Recorder, answering like ``/api/history/period``.

    A request returns, for each requested entity in request order, the rows
    with ``query_start < last_updated < end``; a row stamped exactly at the
    query start is the carry-in state and is returned only without
    ``skip_initial_state``. *failures* fail requests by their 1-based number.
    """

    def __init__(
        self,
        rows: Sequence[Mapping[str, JsonValue]],
        *,
        failures: Mapping[int, HHEError] | None = None,
        states: Sequence[StateObject] = (),
        time_zone: str = "Europe/Berlin",
    ) -> None:
        super().__init__(states=states, time_zone=time_zone)
        self._rows = list(rows)
        self._failures = dict(failures or {})

    def history(
        self, window: DayWindow, entity_ids: Sequence[str], query: HistoryQuery
    ) -> HistoryCall:
        self.calls.append(HistoryRequest(window, tuple(entity_ids), query))
        failure = self._failures.get(len(self.calls))
        if failure is not None:
            raise HistoryFetchError(failure, CallStats(1))
        start, end = window.query_start, window.end_utc
        payload = []
        for entity_id in entity_ids:
            group = []
            for row in self._rows:
                if row["entity_id"] != entity_id:
                    continue
                updated = datetime.fromisoformat(str(row["last_updated"]))
                carry_in = updated == start and not query.skip_initial_state
                if start < updated < end or carry_in:
                    group.append(row)
            if group:
                payload.append(group)
        return HistoryCall(payload, CallStats(1))
