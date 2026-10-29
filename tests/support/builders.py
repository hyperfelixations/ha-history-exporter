from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.domain.formats import Format
from ha_history_exporter.domain.plan import DayAction, DayDecision, ExportPlan
from ha_history_exporter.settings import (
    Config,
    EntitySettings,
    ExportSettings,
    HistoryRequestSettings,
    HomeAssistantSettings,
    RecorderSettings,
    RequestSettings,
    StorageSettings,
)

SYNTHETIC_URL = "http://home-assistant.invalid"
SYNTHETIC_TOKEN = "synthetic-test-token"


def make_config(
    root: Path,
    *,
    jsonl: bool = True,
    csv: bool = False,
    parquet: bool = False,
    batch_size: int = 2,
    purge_keep_days: int | None = 14,
) -> Config:
    """Build a configuration for a test export into *root*.

    Single adaptation point: tests state what they need, this function knows the
    current shape of the configuration object.
    """
    formats = {
        fmt
        for fmt, wanted in (
            (Format.JSONL, jsonl),
            (Format.CSV, csv),
            (Format.PARQUET, parquet),
        )
        if wanted
    }
    return Config(
        homeassistant=HomeAssistantSettings(url=SYNTHETIC_URL),
        export=ExportSettings(
            output_dir=root / "output",
            timezone=ZoneInfo("Europe/Berlin"),
            formats=frozenset(formats),
        ),
        requests=RequestSettings(
            batch_size=batch_size,
            sleep_between_requests=0.0,
            sleep_between_days=0.0,
            timeout=120,
            max_retries=0,
            backoff=(),
        ),
        history_request=HistoryRequestSettings(),
        entities=EntitySettings(),
        recorder=RecorderSettings(purge_keep_days=purge_keep_days),
        storage=StorageSettings(
            temp_dir=root / "temp",
            locked_file_retries=0,
            locked_file_retry_sleep=0.0,
        ),
    )


def day_path(cfg: Config, day: date, suffix: str) -> Path:
    """Where one day's output file goes.

    Single adaptation point for the frozen output contract: tests state the
    expected path, this function knows which object currently builds it.
    """
    return layout(cfg).day_file(day, suffix)


def layout(cfg: Config) -> ArchiveLayout:
    return ArchiveLayout(cfg.export.output_dir)


def make_export_plan(day: date) -> ExportPlan:
    return ExportPlan(
        requested_start=day,
        requested_end=day,
        today=date(2026, 7, 30),
        decisions=(DayDecision(day, DayAction.EXPORT),),
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
