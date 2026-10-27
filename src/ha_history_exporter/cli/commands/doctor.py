"""``doctor``: check installation, configuration and archive; change nothing.

Every check reports ok, warn or fail, a failure with the command that
resolves it. Nothing is created, locked or recovered: a directory that does
not exist yet is judged by its nearest existing parent. ``--offline`` skips
every check that contacts Home Assistant.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import platform
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

from ... import __version__
from ...archive import descriptor
from ...archive.layout import ArchiveLayout
from ...archive.lock import probe
from ...domain.formats import Format
from ...errors import HHEError
from ...formats import missing_requirements
from ...settings import ResolvedSettings, resolve, secrets
from ..logging import CommandLogging
from ..output import Check, CheckStatus, print_checks
from ..services import Services

OK, WARN, FAIL = CheckStatus.OK, CheckStatus.WARN, CheckStatus.FAIL
LOW_DISK_GIB = 5
#: The lock file of earlier versions; HHE no longer uses it.
FOREIGN_LOCK = ".hhe.lock"


def run(args: argparse.Namespace, services: Services, logs: CommandLogging) -> int:
    checks = [Check(OK, "HHE version", f"{__version__} on Python {platform.python_version()}")]
    try:
        settings = resolve(environ=services.environ, cwd=services.cwd, explicit_config=args.config)
    except HHEError as exc:
        checks.append(Check(FAIL, "configuration", exc.summary, _first_command(exc)))
        print_checks(checks)
        return 1
    checks += _configuration(settings)
    checks += _directories(settings)
    checks += _archive(settings)
    if args.offline:
        checks.append(Check(OK, "home assistant", "not contacted (--offline)"))
    else:
        checks += _home_assistant(settings, services)
    print_checks(checks)
    return 1 if any(check.status is FAIL for check in checks) else 0


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
        names = ", ".join(fmt.value for fmt in Format if fmt in formats)
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
    checks: list[Check] = []
    try:
        recorded = descriptor.load(layout)
        if recorded is not None:
            descriptor.determine(
                layout, recorded, configured=configured, home_assistant=recorded.timezone
            )
            checks.append(Check(OK, "archive time zone", recorded.timezone.key))
        else:
            zones = descriptor.survey(layout)
            if not zones:
                origin = configured.key if configured else "Home Assistant's"
                checks.append(Check(OK, "archive time zone", f"new archive; takes {origin}"))
            else:
                # Home Assistant's zone plays no part when an archive is adopted.
                zone = descriptor.determine(
                    layout, None, configured=configured, home_assistant=ZoneInfo(next(iter(zones)))
                )
                checks.append(
                    Check(
                        OK,
                        "archive time zone",
                        f"{zone.zone.key}; the next export records it for "
                        f"{zone.adopted_days} existing day(s)",
                    )
                )
    except HHEError as exc:
        checks.append(Check(FAIL, "archive", exc.summary, _first_command(exc)))
    lock = probe(layout.lock_file)
    if lock.held:
        holder = (lock.holder or "unknown holder").replace("\n", ", ")
        checks.append(Check(WARN, "archive lock", f"an export is running ({holder})"))
    if (layout.root / FOREIGN_LOCK).exists():
        checks.append(
            Check(
                WARN,
                "archive lock",
                f"{FOREIGN_LOCK} is left over from an earlier version; HHE ignores it",
                f"Remove {layout.root / FOREIGN_LOCK} once no old version runs.",
            )
        )
    days = [day for day, _ in layout.manifest_paths()]
    checks.append(
        Check(OK, "existing exports", f"{len(days)} day(s), {days[0]} to {days[-1]}")
        if days
        else Check(OK, "existing exports", "none yet")
    )
    return checks


def _home_assistant(settings: ResolvedSettings, services: Services) -> list[Check]:
    if not (settings.config.homeassistant.url and settings.token):
        return [Check(WARN, "home assistant", "not contacted; URL or token missing", "hhe init")]
    try:
        with contextlib.closing(services.connect(settings)) as source:
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
