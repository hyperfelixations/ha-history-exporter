"""Manifests as stored in the archive."""

from __future__ import annotations

from ..domain.manifest import StoredManifest, decode_manifest
from ..domain.window import DayIdentity
from ..errors import ExportError, Remedy
from .layout import ArchiveLayout


def load_manifest(layout: ArchiveLayout, identity: DayIdentity) -> StoredManifest | None:
    """The day's manifest, or None when the day has none.

    An unreadable or invalid manifest is an error, never "missing": the
    manifest is the durable record of the day.
    """
    path = layout.manifest_file(identity.day)
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ExportError(
            f"Cannot read the manifest for {identity.day}.",
            details=(
                "The manifest is the durable record for this day, so HHE will "
                "not treat an unreadable file as a missing export."
            ),
            remedies=(Remedy("Check the export directory permissions.", "hhe doctor"),),
            context={"day": str(identity.day)},
        ) from exc
    return decode_manifest(raw, identity)
