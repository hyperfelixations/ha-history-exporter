"""Checking installation, configuration and archive; changing nothing.

Every check reports ok, warn or fail, a failure with the command that
resolves it. Nothing is created, locked or recovered: a directory that does
not exist yet is judged by its nearest existing parent, and the archive is
read like ``verify`` reads it, without checksums. Without *connect*, no check
contacts Home Assistant. See internal dev doc § "Commands".
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
from collections.abc import Callable
from pathlib import Path

from .. import __version__
from ..archive import descriptor
from ..archive.layout import ArchiveLayout
from ..archive.reader import ArchiveReader
from ..domain.formats import FORMAT_ORDER
from ..errors import HHEError
from ..formats import missing_requirements
from ..settings import ResolvedSettings, secrets
from ..source.protocol import HistorySource
from ..source.transport import plaintext_warning
from .reading import archive_state
from .results import ArtifactStatus, Check, CheckStatus, DoctorResult
from .verify import Depth, verify_day

OK, WARN, FAIL = CheckStatus.OK, CheckStatus.WARN, CheckStatus.FAIL
LOW_DISK_GIB = 5
#: The lock file of earlier versions; HHE no longer uses it.
FOREIGN_LOCK = ".hhe.lock"


def diagnose(
    resolve: Callable[[], ResolvedSettings],
    *,
    connect: Callable[[ResolvedSettings], HistorySource] | None,
) -> DoctorResult:
    checks = [Check(OK, "HHE version", f"{__version__} on Python {platform.python_version()}")]
    try:
        settings = resolve()
    except HHEError as exc:
        checks.append(Check(FAIL, "configuration", exc.summary, _first_command(exc)))
        return DoctorResult(tuple(checks))
    checks += _configuration(settings)
    checks += _directories(settings)
    checks += _archive(settings)
    if connect is None:
        checks.append(Check(OK, "home assistant", "not contacted (--offline)"))
    else:
        checks += _home_assistant(settings, connect)
    return DoctorResult(tuple(checks))


def _first_command(exc: HHEError) -> str:
    return next((remedy.command for remedy in exc.remedies if remedy.command), "")


def _configuration(settings: ResolvedSettings) -> list[Check]:
    cfg = settings.config
    checks = [
        Check(OK, "configuration", str(settings.config_file))
        if settings.config_file is not None
        else Check(WARN, "configuration", "no file; built-in defaults apply", "hhe init")
    ]
    checks += [
        Check(WARN, "environment", f"{name} has no effect; is the name misspelt?")
        for name in settings.unknown_environment
    ]
    url = cfg.homeassistant.url
    checks.append(
        Check(OK, "home assistant url", f"{url}  [{settings.origin('homeassistant.url')}]")
        if url
        else Check(
            FAIL,
            "home assistant url",
            "not configured",
            "hhe config set homeassistant.url http://homeassistant.local:8123",
        )
    )
    warning = plaintext_warning(url) if url else None
    if warning is not None:
        checks.append(
            Check(
                WARN,
                "plain http",
                warning,
                "hhe config set homeassistant.url https://<host>:8123",
            )
        )
    checks.append(
        Check(OK, "access token", f"stored  [{settings.origin('homeassistant.token')}]")
        if settings.token
        else Check(FAIL, "access token", "not configured", "hhe config set homeassistant.token")
    )
    if (
        settings.credentials_file.is_file()
        and secrets.is_owner_only(settings.credentials_file) is False
    ):
        checks.append(
            Check(
                WARN,
                "credentials permissions",
                f"{settings.credentials_file} is readable by others",
                f"chmod 600 {settings.credentials_file}",
            )
        )
    bundle = cfg.homeassistant.ca_bundle
    if bundle is not None:
        checks.append(
            Check(OK, "ca bundle", str(bundle))
            if bundle.is_file()
            else Check(
                FAIL,
                "ca bundle",
                f"{bundle} does not exist",
                "hhe config set homeassistant.ca_bundle <path-to-ca.pem>",
            )
        )
    formats = cfg.export.formats
    if not formats:
        checks.append(
            Check(
                WARN,
                "output formats",
                "none; runs capture the entity snapshot only",
                "hhe config set export.formats jsonl",
            )
        )
    else:
        names = ", ".join(fmt.value for fmt in FORMAT_ORDER if fmt in formats)
        checks.append(Check(OK, "output formats", names))
        if missing_requirements(formats):
            checks.append(
                Check(
                    FAIL,
                    "pyarrow",
                    "Parquet is enabled but pyarrow is not installed",
                    "pipx inject ha-history-exporter pyarrow",
                )
            )
    return checks


def _existing(path: Path) -> Path:
    """*path* or its nearest existing ancestor."""
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _writable(label: str, path: Path, key: str) -> Check:
    existing = _existing(path)
    if not os.access(existing, os.W_OK):
        return Check(FAIL, label, f"{existing} is not writable", f"hhe config set {key} <path>")
    note = "" if existing == path else "  (created by the first export)"
    return Check(OK, label, f"{path}{note}")


def _directories(settings: ResolvedSettings) -> list[Check]:
    cfg = settings.config
    output = cfg.export.output_dir
    checks = [
        _writable("output directory", output, "export.output_dir"),
        _writable("temporary directory", cfg.storage.temp_dir, "storage.temp_dir"),
    ]
    free_gib = shutil.disk_usage(_existing(output)).free / 1024**3
    checks.append(
        Check(OK, "free disk space", f"{free_gib:.1f} GiB")
        if free_gib >= LOW_DISK_GIB
        else Check(
            WARN,
            "free disk space",
            f"{free_gib:.1f} GiB",
            "Free up space; one exported day can reach several hundred MB.",
        )
    )
    return checks


def _archive(settings: ResolvedSettings) -> list[Check]:
    layout = ArchiveLayout(settings.config.export.output_dir)
    configured = settings.config.export.timezone
    try:
        reader = ArchiveReader.open(layout, configured=configured)
    except HHEError as exc:
        return [Check(FAIL, "archive", exc.summary, _first_command(exc))]
    state = archive_state(reader)
    checks = [_zone(reader)]
    if state.running is not None:
        checks.append(Check(WARN, "archive lock", f"an export is running ({state.running})"))
    if (layout.root / FOREIGN_LOCK).exists():
        checks.append(
            Check(
                WARN,
                "archive lock",
                f"{FOREIGN_LOCK} is left over from an earlier version; HHE ignores it",
                f"Remove {layout.root / FOREIGN_LOCK} once no old version runs.",
            )
        )
    if state.pending_transactions:
        checks.append(
            Check(
                WARN,
                "interrupted transactions",
                f"{state.pending_transactions} day(s); the next export or conversion restores them",
            )
        )
    checks += _days(reader)
    return checks


def _zone(reader: ArchiveReader) -> Check:
    zone = reader.zone
    if reader.descriptor is not None:
        return Check(OK, "archive time zone", reader.descriptor.timezone.key)
    if zone is None:
        return Check(OK, "archive time zone", "new archive; takes Home Assistant's")
    if zone.source is descriptor.ZoneSource.CONFIGURED:
        return Check(OK, "archive time zone", f"new archive; takes {zone.zone.key}")
    return Check(
        OK,
        "archive time zone",
        f"{zone.zone.key}; the next export records it for {zone.adopted_days} existing day(s)",
    )


def _days(reader: ArchiveReader) -> list[Check]:
    stored = reader.artifacts()
    manifests = set(reader.manifest_days())
    days = sorted(manifests | set(stored))
    results = [
        verify_day(
            reader, day, stored.get(day, {}), has_manifest=day in manifests, depth=Depth.PRESENCE
        )
        for day in days
    ]
    checks = []
    invalid = [result.day for result in results if result.invalid is not None]
    if invalid:
        checks.append(
            Check(FAIL, "manifests", f"{len(invalid)} invalid, first {invalid[0]}", "hhe verify")
        )
    for status, level, text in (
        (ArtifactStatus.UNEXPECTED, FAIL, "file(s) that no manifest declares"),
        (ArtifactStatus.MISSING, WARN, "declared file(s) missing"),
    ):
        affected = [r.day for r in results if any(c.status is status for c in r.artifacts)]
        if affected:
            count = sum(c.status is status for r in results for c in r.artifacts)
            checks.append(
                Check(level, "day files", f"{count} {text}, first {affected[0]}", "hhe verify")
            )
    checks.append(
        Check(
            OK,
            "existing exports",
            f"{len(manifests)} day(s), {min(manifests)} to {max(manifests)}",
        )
        if manifests
        else Check(OK, "existing exports", "none yet")
    )
    return checks


def _home_assistant(
    settings: ResolvedSettings, connect: Callable[[ResolvedSettings], HistorySource]
) -> list[Check]:
    if not (settings.config.homeassistant.url and settings.token):
        return [Check(WARN, "home assistant", "not contacted; URL or token missing", "hhe init")]
    try:
        with contextlib.closing(connect(settings)) as source:
            source.check_api()
            instance = source.instance_config()
            count = len(source.states())
    except HHEError as exc:
        return [Check(FAIL, "home assistant", exc.summary, _first_command(exc))]
    checks = [
        Check(
            OK,
            "home assistant",
            f"reachable, version {instance.version}, {count} entities, {instance.time_zone.key}",
        )
    ]
    with contextlib.suppress(HHEError):
        recorded = descriptor.load(ArchiveLayout(settings.config.export.output_dir))
        if recorded is not None and recorded.timezone.key != instance.time_zone.key:
            checks.append(
                Check(
                    WARN,
                    "time zones",
                    f"the archive keeps {recorded.timezone.key}; Home Assistant uses "
                    f"{instance.time_zone.key}",
                )
            )
    return checks
