"""The archive's root record ``hhe-archive.json``: its format and time zone.

Every day of an archive is a calendar day in one time zone. The descriptor
records that zone once; it never changes. A new archive takes the configured
``export.timezone``, or else the time zone of Home Assistant. An archive
without a descriptor but with days is adopted: every manifest is read and
must name the same zone. A configured zone that differs from the archive's is
an error, not a silent switch. See internal dev doc § "Archive descriptor".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import __version__, fsio
from ..domain.manifest import decode_manifest, invalid_manifest
from ..domain.window import DayWindow, format_iso
from ..errors import ConfigError, ExportError, Remedy
from .layout import ArchiveLayout

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
CREATED_BY = f"ha-history-exporter {__version__}"
_FIELDS = ("format_version", "timezone", "created_at", "created_by")


@dataclass(frozen=True)
class ArchiveDescriptor:
    timezone: ZoneInfo
    created_at: str
    created_by: str
    format_version: int = FORMAT_VERSION

    def encode(self) -> bytes:
        data = {
            "format_version": self.format_version,
            "timezone": self.timezone.key,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }
        return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class ZoneSource(StrEnum):
    """Where the archive's time zone comes from."""

    RECORDED = "recorded"
    ADOPTED = "adopted"
    CONFIGURED = "configured"
    HOME_ASSISTANT = "home_assistant"


@dataclass(frozen=True)
class ArchiveZone:
    zone: ZoneInfo
    source: ZoneSource
    #: The days whose manifests an adoption read.
    adopted_days: int = 0


def _invalid(layout: ArchiveLayout, reason: str) -> ExportError:
    return ExportError(
        f"{layout.descriptor_file.name} is invalid: {reason}.",
        details=(
            "The file records the time zone of every day in this archive; HHE does "
            "not export into an archive it cannot identify."
        ),
        remedies=(
            Remedy(
                "Restore the file from a backup. Removing it lets the next export "
                "adopt the archive again from its manifests."
            ),
        ),
        context={"output_dir": str(layout.root)},
    )


def load(layout: ArchiveLayout) -> ArchiveDescriptor | None:
    """The descriptor, or None for an archive that has none yet."""
    path = layout.descriptor_file
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise _invalid(
            layout, f"it cannot be read ({exc.strerror or type(exc).__name__})"
        ) from None
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid(layout, "it is not valid JSON") from None
    if not isinstance(data, dict):
        raise _invalid(layout, "the top level must be an object")
    version = data.get("format_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > FORMAT_VERSION:
        raise ExportError(
            "This archive was written by a newer version of HHE.",
            details=(
                f"{path.name} has format version {version}; this version of HHE "
                f"understands version {FORMAT_VERSION}."
            ),
            remedies=(Remedy("Update HHE:", "pipx upgrade ha-history-exporter"),),
            context={"output_dir": str(layout.root)},
        )
    if set(data) != set(_FIELDS) or version != FORMAT_VERSION:
        raise _invalid(layout, "its fields are not the expected ones")
    zone, created_at, created_by = data["timezone"], data["created_at"], data["created_by"]
    if not all(isinstance(value, str) for value in (zone, created_at, created_by)):
        raise _invalid(layout, "its values must be strings")
    try:
        timezone = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise _invalid(layout, f"the time zone {zone!r} is not known on this machine") from None
    return ArchiveDescriptor(timezone, created_at, created_by)


def survey(layout: ArchiveLayout) -> dict[str, list[date]]:
    """The days of every stored manifest by time zone; every manifest must be valid."""
    zones: dict[str, list[date]] = {}
    for day, path in layout.manifest_paths():
        try:
            raw = path.read_bytes()
        except OSError:
            raise invalid_manifest(day, "file cannot be read") from None
        try:
            data = json.loads(raw.decode("utf-8"))
            zone = ZoneInfo(data["timezone"])
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError):
            raise invalid_manifest(day, "it names no time zone") from None
        except (ZoneInfoNotFoundError, ValueError):
            raise invalid_manifest(day, "its time zone is not known on this machine") from None
        decode_manifest(raw, DayWindow.full(day, zone).identity())
        zones.setdefault(zone.key, []).append(day)
    return zones


def determine(
    layout: ArchiveLayout,
    recorded: ArchiveDescriptor | None,
    *,
    configured: ZoneInfo | None,
    home_assistant: ZoneInfo,
) -> ArchiveZone:
    """The archive's time zone and where it comes from; writes nothing."""
    if recorded is not None:
        archive = ArchiveZone(recorded.timezone, ZoneSource.RECORDED)
    else:
        zones = survey(layout)
        if len(zones) > 1:
            listing = ", ".join(f"{key} ({len(days)} days)" for key, days in sorted(zones.items()))
            raise ExportError(
                "The archive holds days of more than one time zone.",
                details=(
                    f"Its manifests name {listing}. One archive holds the calendar "
                    "days of one time zone."
                ),
                remedies=(
                    Remedy(
                        "Move the days of each time zone into an archive of their own, "
                        "or export into a new output directory:",
                        "hhe export --output-dir <new-directory>",
                    ),
                ),
                context={"output_dir": str(layout.root)},
            )
        if zones:
            ((key, days),) = zones.items()
            archive = ArchiveZone(ZoneInfo(key), ZoneSource.ADOPTED, len(days))
        elif configured is not None:
            archive = ArchiveZone(configured, ZoneSource.CONFIGURED)
        else:
            archive = ArchiveZone(home_assistant, ZoneSource.HOME_ASSISTANT)
    if configured is not None and configured.key != archive.zone.key:
        raise ConfigError(
            f"export.timezone is {configured.key}, but this archive's days are in "
            f"{archive.zone.key}.",
            details=(
                "Every day of an archive is a calendar day in the same time zone; "
                "switching zones would export overlapping or missing hours."
            ),
            remedies=(
                Remedy("Follow the archive's time zone:", "hhe config unset export.timezone"),
                Remedy(
                    "Or export into a new archive:",
                    "hhe export --output-dir <new-directory>",
                ),
            ),
            context={"output_dir": str(layout.root)},
        )
    return archive


def establish(
    layout: ArchiveLayout,
    recorded: ArchiveDescriptor | None,
    *,
    configured: ZoneInfo | None,
    home_assistant: ZoneInfo,
    now: datetime,
) -> ArchiveDescriptor:
    """The descriptor, written first when the archive has none; call under the lock."""
    archive = determine(layout, recorded, configured=configured, home_assistant=home_assistant)
    if recorded is not None:
        return recorded
    descriptor = ArchiveDescriptor(
        archive.zone, format_iso(now.astimezone(archive.zone)), CREATED_BY
    )
    fsio.atomic_write(layout.descriptor_file, descriptor.encode())
    if archive.source is ZoneSource.ADOPTED:
        logger.info(
            "Adopted the archive in %s: its %d day(s) all use time zone %s.",
            layout.root,
            archive.adopted_days,
            archive.zone.key,
        )
    else:
        logger.info(
            "Created the archive descriptor with time zone %s (%s).",
            archive.zone.key,
            "export.timezone" if archive.source is ZoneSource.CONFIGURED else "Home Assistant",
        )
    return descriptor
