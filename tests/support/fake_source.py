"""An in-memory Home Assistant behind the :class:`HistorySource` protocol."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ha_history_exporter.domain.query import HistoryQuery
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
