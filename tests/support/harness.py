"""The single driver seam between the output contract and the implementation.

Contract tests and ``tools/refresh_golden.py`` describe what an export is asked
to do as a :class:`Scenario` and receive the produced output directory. Only
:func:`produce` knows how the current implementation is driven, so the
implementation can be rebuilt without touching a contract test.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ha_history_exporter.app.export import run_export
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.metadata import write_snapshot
from ha_history_exporter.archive.store import load_manifest
from ha_history_exporter.archive.workspace import new_run_id
from ha_history_exporter.domain.plan import DayAction, DayDecision, ExportPlan
from ha_history_exporter.domain.snapshot import snapshot_document
from ha_history_exporter.domain.window import DayWindow
from ha_history_exporter.errors import AuthError, HAAPIError, HAConnectionError, HHEError
from ha_history_exporter.settings import HistoryRequestSettings
from tests.helpers import make_config
from tests.support.fake_source import FakeHistorySource, HistoryRequest

FailureKind = Literal["unreachable", "api", "auth"]


@dataclass(frozen=True)
class Failure:
    """A history request that fails instead of answering."""

    kind: FailureKind


#: One history request's answer: the list-of-lists payload or a failure.
Response = list[list[dict[str, Any]]] | Failure


@dataclass(frozen=True)
class Scenario:
    """Everything an export run is asked to do, stated independently of the code."""

    name: str
    days: tuple[date, ...]
    responses: tuple[Response, ...]
    entity_ids: tuple[str, ...]
    entity_count_total: int
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
        export=replace(cfg.export, timezone=ZoneInfo(scenario.timezone)),
        history_request=HistoryRequestSettings(skip_initial_state=scenario.skip_initial_state),
    )
    source = FakeHistorySource(
        _exception(response) if isinstance(response, Failure) else response
        for response in scenario.responses
    )
    plan = ExportPlan(
        requested_start=scenario.days[0],
        requested_end=scenario.days[-1],
        today=scenario.today,
        decisions=tuple(
            DayDecision(
                day,
                DayAction.EXPORT_PARTIAL if day == scenario.partial_day else DayAction.EXPORT,
            )
            for day in scenario.days
        ),
    )
    try:
        exit_code = run_export(
            cfg=cfg,
            plan=plan,
            entity_ids=list(scenario.entity_ids),
            entity_count_total=scenario.entity_count_total,
            source=source,
            tz=ZoneInfo(scenario.timezone),
            sleep=lambda _seconds: None,
            now=scenario.now,
        )
    except HHEError as exc:
        return Production(cfg.export.output_dir, None, type(exc), source.calls)
    return Production(cfg.export.output_dir, exit_code, None, source.calls)


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
