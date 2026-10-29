"""What the application needs from Home Assistant.

:class:`HistorySource` is the seam between the use cases and the REST API;
tests implement it in memory. A history request reports how many attempts it
took, also when it fails, so a day counts every retry. See internal dev doc
§ "Home Assistant source".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

from ..domain.query import HistoryQuery
from ..domain.records import JsonValue
from ..domain.window import DayWindow
from ..domain.wire import WirePayload
from ..errors import HHEError

type StateObject = Mapping[str, JsonValue]


@dataclass(frozen=True)
class CallStats:
    """The attempts one request took; every attempt after the first is a retry."""

    attempts: int

    @property
    def retries(self) -> int:
        return max(self.attempts - 1, 0)


@dataclass(frozen=True)
class InstanceConfig:
    """What ``/api/config`` says about the instance."""

    time_zone: ZoneInfo
    version: str


@dataclass(frozen=True)
class HistoryCall:
    payload: WirePayload
    stats: CallStats


class HistoryFetchError(Exception):
    """A history request that failed for good, with the attempts it took."""

    def __init__(self, error: HHEError, stats: CallStats) -> None:
        super().__init__(error.summary)
        self.error = error
        self.stats = stats


class HistorySource(Protocol):
    def check_api(self) -> None:
        """Prove that the API answers and accepts the token."""
        ...

    def instance_config(self) -> InstanceConfig: ...

    def states(self) -> Sequence[StateObject]: ...

    def history(
        self, window: DayWindow, entity_ids: Sequence[str], query: HistoryQuery
    ) -> HistoryCall:
        """The history of *entity_ids* in *window*; raises :class:`HistoryFetchError`."""
        ...

    def close(self) -> None: ...
