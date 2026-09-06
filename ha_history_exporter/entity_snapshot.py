"""Entity snapshot: capture and persist the current /api/states entity list.

One snapshot is saved per export run so it is later possible to reconstruct
what entities existed at the time of the export. The snapshot does NOT drive
future exports — the runtime /api/states call always provides the live list.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

from .time_utils import format_iso

logger = logging.getLogger(__name__)


def build_snapshot(states: List[dict], tz: ZoneInfo) -> dict:
    """Build a snapshot dict from the raw /api/states response."""
    now = datetime.now(tz)
    entities = []
    for s in states:
        eid = s.get("entity_id", "")
        domain = eid.split(".")[0] if "." in eid else ""
        attrs = s.get("attributes", {}) or {}
        entities.append(
            {
                "entity_id": eid,
                "domain": domain,
                "state_at_snapshot": s.get("state", ""),
                "friendly_name": attrs.get("friendly_name", ""),
            }
        )
    entities.sort(key=lambda e: e["entity_id"])

    unknown_count = sum(1 for e in entities if e["state_at_snapshot"] == "unknown")
    unavailable_count = sum(
        1 for e in entities if e["state_at_snapshot"] == "unavailable"
    )

    return {
        "created_at": format_iso(now),
        "source": "/api/states",
        "entity_count": len(entities),
        "entity_count_unknown": unknown_count,
        "entity_count_unavailable": unavailable_count,
        "entities": entities,
    }


def extract_entity_ids(
    states: List[dict],
    *,
    include_unknown: bool = True,
    include_unavailable: bool = True,
) -> List[str]:
    """Return a sorted list of entity_ids to request history for.

    Entities whose current state is `unknown` or `unavailable` are included by
    default: they exist in HA and may well have historical state changes worth
    exporting. Excluding them is opt-in through entity_selection.

    The snapshot itself always keeps every entity — the filter only narrows
    which entities history is requested for.
    """
    skipped_states = set()
    if not include_unknown:
        skipped_states.add("unknown")
    if not include_unavailable:
        skipped_states.add("unavailable")

    ids = sorted(
        {
            s["entity_id"]
            for s in states
            if "entity_id" in s and s.get("state") not in skipped_states
        }
    )
    logger.debug("Extracted %d entity IDs from /api/states.", len(ids))
    return ids


def apply_optional_excludes(
    entity_ids: List[str],
    exclude_domains: List[str],
    exclude_patterns: List[str],
) -> tuple[List[str], int]:
    """Apply optional domain and glob excludes.  Exclude always wins.

    Returns:
        (filtered_ids, excluded_count)
    """
    import fnmatch

    domains = set(exclude_domains)
    result = []
    for eid in entity_ids:
        domain = eid.split(".")[0]
        if domain in domains:
            continue
        if any(fnmatch.fnmatch(eid, pat) for pat in exclude_patterns):
            continue
        result.append(eid)

    excluded = len(entity_ids) - len(result)
    return result, excluded


def save_snapshot(snapshot: dict, metadata_dir: Path) -> Path:
    """Write the entity snapshot to metadata_dir/entity_snapshot_<ts>.json."""
    metadata_dir.mkdir(parents=True, exist_ok=True)

    ts = snapshot["created_at"].replace(":", "").replace("+", "").replace("-", "")[:15]
    filename = f"entity_snapshot_{ts}.json"
    path = metadata_dir / filename
    tmp = path.with_suffix(".json.tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    from .writers import atomic_replace
    atomic_replace(tmp, path)

    logger.info(
        "Entity snapshot saved: %s (%d entities).", filename, snapshot["entity_count"]
    )
    return path
