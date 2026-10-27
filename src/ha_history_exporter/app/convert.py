"""Deriving formats a day lacks from an artifact it has, without Home Assistant.

Only a day whose schema 1.4 manifest says ``ok`` is converted. Its source is
the first declared artifact in the order JSONL, Parquet, CSV whose format can
be read here. The source is decoded once: its records stream into the new
formats while its size, SHA-256, row count and logical digest are compared
with the manifest; a source that differs blocks the day. The new artifacts are
read back like a capture's, and the day's manifest gains them and changes in
nothing else. Artifacts and manifest are published through the day
transaction. See internal dev doc § "Commands".
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..archive.layout import ArchiveLayout
from ..archive.reader import ArchiveReader
from ..archive.session import ArchiveSession
from ..archive.store import load_manifest
from ..domain.clock import Clock, SystemClock
from ..domain.formats import FORMAT_ORDER, Format
from ..domain.manifest import LegacyManifest, Manifest14, ManifestStatus
from ..domain.plan import DaySelection
from ..domain.records import HistoryRecord
from ..domain.stored import RecordContract
from ..domain.window import DayWindow
from ..errors import ConfigError, HHEError, InternalError, Remedy, ValidationError
from ..formats import FORMATS, missing_requirements
from ..formats.validation import CaptureSummary, inspect_artifact, validate_artifacts
from ..settings import Config
from .context import RunContext
from .publish import publish_day
from .reading import requested_range
from .results import ConversionOutcome, ConvertResult, DayConversion

logger = logging.getLogger(__name__)

#: The order in which a day's artifacts are considered as the source.
SOURCE_ORDER = (Format.JSONL, Format.PARQUET, Format.CSV)


@dataclass(frozen=True)
class ConvertRequest:
    formats: frozenset[Format]
    #: None converts every day of the archive.
    selection: DaySelection | None = None


@dataclass(frozen=True)
class ConvertHooks:
    #: Called once the archive is locked: the run's log file from now on.
    attach_log: Callable[[Path], None] = lambda _path: None


def run_convert(
    request: ConvertRequest,
    cfg: Config,
    *,
    clock: Clock | None = None,
    hooks: ConvertHooks | None = None,
) -> ConvertResult:
    clock = clock or SystemClock()
    hooks = hooks or ConvertHooks()
    if not request.formats:
        raise ConfigError(
            "Name at least one format to derive.",
            remedies=(Remedy("For example:", "hhe convert --format parquet"),),
        )
    missing = missing_requirements(request.formats)
    if missing:
        raise ConfigError(
            f"Parquet output needs {', '.join(missing)}, which is not installed.",
            remedies=(
                Remedy(
                    "Add it to an existing pipx installation:",
                    "pipx inject ha-history-exporter pyarrow",
                ),
            ),
        )
    run = RunContext.start(clock)
    layout = ArchiveLayout(cfg.export.output_dir)
    reader = ArchiveReader.open(layout, configured=cfg.export.timezone)
    if not reader.manifest_days():
        logger.info("The archive in %s holds no day yet; nothing to convert.", layout.root)
        return ConvertResult(request.formats, ())
    span = requested_range(reader, request.selection, clock)

    with ArchiveSession(
        layout,
        cfg.storage.temp_dir,
        run.run_id,
        holder=run.lock_holder(),
        configured_zone=cfg.export.timezone,
        home_assistant_zone=None,
        now=run.started_at,
        retries=cfg.storage.locked_file_retries,
        retry_sleep=cfg.storage.locked_file_retry_sleep,
    ) as session:
        zone = session.descriptor.timezone
        log_file = layout.log_file(run.local(zone))
        hooks.attach_log(log_file)
        days = [
            day for day, _ in layout.manifest_paths() if span is None or span[0] <= day <= span[1]
        ]
        logger.info(
            "Converting %d day(s) of %s to %s.",
            len(days),
            layout.root,
            ", ".join(fmt.value for fmt in FORMAT_ORDER if fmt in request.formats),
        )
        conversion = _Conversion(cfg, session, request.formats)
        results = tuple(conversion.day(DayWindow.full(day, zone)) for day in days)
    return ConvertResult(request.formats, results, log_file)


class _Conversion:
    def __init__(self, cfg: Config, session: ArchiveSession, formats: frozenset[Format]) -> None:
        self.cfg = cfg
        self.session = session
        self.layout = session.layout
        self.formats = formats

    def day(self, window: DayWindow) -> DayConversion:
        day = window.day
        try:
            return self._convert(window)
        except HHEError as exc:
            logger.error("Day %s was not converted: %s", day, exc.summary)
            return DayConversion(day, ConversionOutcome.FAILED, reason=exc.summary)
        except Exception as exc:
            logger.error("Day %s failed with an unexpected error.", day, exc_info=exc)
            raise InternalError.during(f"the conversion of {day}") from None

    def _convert(self, window: DayWindow) -> DayConversion:
        day = window.day
        manifest = load_manifest(self.layout, window.identity())
        if manifest is None:
            return DayConversion(day, ConversionOutcome.SKIPPED, reason="no manifest")
        if isinstance(manifest, LegacyManifest):
            return DayConversion(
                day,
                ConversionOutcome.BLOCKED,
                reason=f"schema {manifest.schema_version} records no checksums to prove a source",
            )
        if manifest.status is not ManifestStatus.OK:
            return DayConversion(
                day, ConversionOutcome.SKIPPED, reason=f"the day is {manifest.status.value}"
            )
        wanted = tuple(
            fmt for fmt in FORMAT_ORDER if fmt in self.formats and fmt not in manifest.artifacts
        )
        if not wanted:
            return DayConversion(day, ConversionOutcome.PRESENT)
        source = next(
            (
                fmt
                for fmt in SOURCE_ORDER
                if fmt in manifest.artifacts and FORMATS[fmt].requirement_met()
            ),
            None,
        )
        if source is None:
            return DayConversion(
                day, ConversionOutcome.BLOCKED, reason="no declared artifact can be read here"
            )
        return self._derive(window, manifest, source, wanted)

    def _derive(
        self, window: DayWindow, manifest: Manifest14, source: Format, wanted: tuple[Format, ...]
    ) -> DayConversion:
        day = window.day
        source_path = self.layout.day_file(day, source.value)
        expected = manifest.artifacts[source]
        if not source_path.is_file():
            return DayConversion(
                day,
                ConversionOutcome.BLOCKED,
                source=source,
                reason=f"the {source.value} source is missing",
            )
        work_dir = self.session.workspace.work_dir
        targets = {fmt: work_dir / f"{day.isoformat()}.{fmt.value}" for fmt in wanted}
        contract = RecordContract(window)
        with contextlib.ExitStack() as stack:
            writers = [
                stack.enter_context(FORMATS[fmt].open_writer(path)) for fmt, path in targets.items()
            ]

            def each(record: HistoryRecord) -> None:
                for writer in writers:
                    writer.write(record)

            try:
                found = inspect_artifact(source_path, source, contract=contract, each=each)
            except ValidationError as exc:
                found = None
                problem = f"the {source.value} source cannot be read: {exc.summary}"
            else:
                problem = f"the {source.value} source differs from the manifest"
        if found != expected:
            for path in targets.values():
                path.unlink(missing_ok=True)
            return DayConversion(
                day,
                ConversionOutcome.BLOCKED,
                source=source,
                reason=problem,
            )
        artifacts = validate_artifacts(
            targets,
            contract=contract,
            expected=CaptureSummary(
                rows=expected.row_count, logical_sha256=expected.logical_sha256
            ),
        )
        updated = replace(manifest, artifacts={**manifest.artifacts, **artifacts})
        publish_day(
            self.layout, self.session.workspace, day, targets, updated, storage=self.cfg.storage
        )
        logger.info(
            "Day %s: derived %s from %s.", day, ", ".join(fmt.value for fmt in wanted), source.value
        )
        return DayConversion(day, ConversionOutcome.CONVERTED, wanted, source)
