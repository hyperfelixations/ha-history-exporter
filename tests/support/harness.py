"""The single driver seam between the output contract and the implementation.

Contract tests and ``tools/refresh_golden.py`` describe what an export is asked
to do as a :class:`Scenario` and receive the produced output directory. Only
:func:`produce` knows how the current implementation is driven, so the
implementation can be rebuilt without touching a contract test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ha_history_exporter.app.convert import ConvertRequest, run_convert
from ha_history_exporter.app.export import ExportRequest, run_export
from ha_history_exporter.app.results import ConvertResult
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.metadata import write_snapshot
from ha_history_exporter.archive.store import load_manifest
from ha_history_exporter.archive.workspace import new_run_id
from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.plan import DaySelection
from ha_history_exporter.domain.snapshot import snapshot_document
from ha_history_exporter.domain.window import DayWindow
from ha_history_exporter.errors import AuthError, HAAPIError, HAConnectionError, HHEError
from ha_history_exporter.settings import EntitySettings, HistoryRequestSettings
from tests.helpers import make_config
from tests.support.clock import FixedClock
from tests.support.fake_source import HistoryRequest, RecordedHistorySource

FailureKind = Literal["unreachable", "api", "auth"]

#: The domain of the entities a scenario counts but does not request.
_EXCLUDED_DOMAIN = "update"


@dataclass(frozen=True)
class Failure:
    """A history request that fails instead of answering."""

    kind: FailureKind


@dataclass(frozen=True)
class Scenario:
    """Everything an export run is asked to do, stated independently of the code.

    *history* is the Recorder of the instance: every row of every entity.
    *failures* fail history requests by their 1-based number. Of the
    *entity_count_total* current entities, those beyond *entity_ids* are
    excluded from the request by configuration.
    """

    name: str
    days: tuple[date, ...]
    entity_ids: tuple[str, ...]
    entity_count_total: int
    history: tuple[dict[str, Any], ...] = ()
    failures: Mapping[int, Failure] = field(default_factory=dict)
    formats: frozenset[str] = frozenset({"jsonl"})
    batch_size: int = 2
    timezone: str = "Europe/Berlin"
    purge_keep_days: int | None = 14
    skip_initial_state: bool = True
    partial_day: date | None = None
    now: datetime | None = None
    today: date = date(2026, 10, 27)


@dataclass(frozen=True)
class Production:
    """What one scenario produced."""

    output_dir: Path
    exit_code: int | None
    error: type[BaseException] | None = None
    history_calls: list[HistoryRequest] = field(default_factory=list)


def _exception(failure: Failure) -> HHEError:
    if failure.kind == "unreachable":
        return HAConnectionError("Cannot reach Home Assistant at http://home-assistant.invalid.")
    if failure.kind == "api":
        return HAAPIError("HTTP 500 from /api/history/period.")
    return AuthError("Authentication failed: Home Assistant rejected the access token (HTTP 401).")


def _states(scenario: Scenario) -> list[dict[str, Any]]:
    excluded = scenario.entity_count_total - len(scenario.entity_ids)
    ids = [*scenario.entity_ids, *(f"{_EXCLUDED_DOMAIN}.synthetic_{n}" for n in range(excluded))]
    return [{"entity_id": entity_id, "state": "on", "attributes": {}} for entity_id in ids]


def produce(scenario: Scenario, root: Path) -> Production:
    """Run *scenario* into *root* and report what happened."""
    cfg = make_config(
        root,
        jsonl="jsonl" in scenario.formats,
        csv="csv" in scenario.formats,
        parquet="parquet" in scenario.formats,
        batch_size=scenario.batch_size,
        purge_keep_days=scenario.purge_keep_days,
    )
    cfg = replace(
        cfg,
        export=replace(cfg.export, timezone=None),
        history_request=HistoryRequestSettings(skip_initial_state=scenario.skip_initial_state),
        entities=EntitySettings(exclude_domains=(_EXCLUDED_DOMAIN,)),
    )
    source = RecordedHistorySource(
        scenario.history,
        failures={number: _exception(kind) for number, kind in scenario.failures.items()},
        states=_states(scenario),
        time_zone=scenario.timezone,
    )
    zone = ZoneInfo(scenario.timezone)
    now = scenario.now or datetime.combine(scenario.today, time(12), tzinfo=zone)
    selection = (
        DaySelection.single(scenario.partial_day)
        if scenario.partial_day is not None
        else DaySelection(start=scenario.days[0], end=scenario.days[-1])
    )
    try:
        result = run_export(
            ExportRequest(selection),
            cfg,
            lambda: source,
            clock=FixedClock(now),
            sleep=lambda _seconds: None,
        )
    except HHEError as exc:
        return Production(cfg.export.output_dir, None, type(exc), source.calls)
    return Production(cfg.export.output_dir, int(result.exit_code), None, source.calls)


def produce_snapshot(states: list[dict[str, Any]], output_dir: Path) -> Path:
    """Write the entity snapshot of one run for *states*; return its path."""
    created_local = datetime.now(ZoneInfo("Europe/Berlin"))
    return write_snapshot(
        ArchiveLayout(output_dir),
        snapshot_document(states, created_local),
        created_local,
        new_run_id(created_local.astimezone(UTC)),
    )


def stored_status(output_dir: Path, day: date, timezone: str = "Europe/Berlin") -> str | None:
    """The status of the day manifest in *output_dir*, or None without one.

    Raises the implementation's error for a manifest that cannot be trusted.
    """
    loaded = load_manifest(
        ArchiveLayout(output_dir), DayWindow.full(day, ZoneInfo(timezone)).identity()
    )
    return None if loaded is None else loaded.status.value


def derive(root: Path, formats: frozenset[str], *, now: datetime) -> ConvertResult:
    """Derive *formats* for every day that :func:`produce` wrote into *root*."""
    cfg = make_config(root)
    cfg = replace(cfg, export=replace(cfg.export, timezone=None))
    return run_convert(
        ConvertRequest(frozenset(Format(name) for name in formats)), cfg, clock=FixedClock(now)
    )
