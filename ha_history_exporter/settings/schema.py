"""Declarative registry of every configuration key.

The registry is the single source for YAML validation, environment-variable
names, ``config set`` coercion, the generated example configuration, and the
key reference in the README.

Key status:
  SUPPORTED     the value is honoured
  DEFAULT_ONLY  the option is published but not implemented yet; only the
                documented default is accepted, every other value is rejected
                with a clear message instead of being silently ignored
  SECRET        the value is never echoed back; it lives in the credentials
                file, not in the configuration file
"""

from __future__ import annotations

import difflib
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from ..errors import ConfigError, Remedy
from . import paths


class KeyType(str, Enum):
    STRING = "string"
    PATH = "path"
    BOOL = "boolean"
    INT = "integer"
    FLOAT = "number"
    OPTIONAL_INT = "integer or null"
    STR_LIST = "list of strings"
    NUM_LIST = "list of numbers"


class KeyStatus(str, Enum):
    SUPPORTED = "supported"
    DEFAULT_ONLY = "default-only"
    SECRET = "secret"


# ── value checks ──────────────────────────────────────────────────────────────

def _fail(field_name: str, message: str, *, details: str | None = None) -> ConfigError:
    return ConfigError(
        f"{field_name} {message}",
        details=details,
        remedies=(
            Remedy(
                "Show every effective setting and where it comes from:",
                "hhe config list --origin",
            ),
        ),
    )


def integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(field_name, "must be an integer.")
    return value


def positive_int(value: Any, field_name: str) -> int:
    number = integer(value, field_name)
    if number <= 0:
        raise _fail(field_name, "must be greater than 0.")
    return number


def non_negative_int(value: Any, field_name: str) -> int:
    number = integer(value, field_name)
    if number < 0:
        raise _fail(field_name, "must be greater than or equal to 0.")
    return number


def optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return positive_int(value, field_name)


def non_negative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(field_name, "must be a number.")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise _fail(
            field_name, "must be a finite number greater than or equal to 0."
        )
    return number


def non_negative_number_list(value: Any, field_name: str) -> list[float]:
    if not isinstance(value, list):
        raise _fail(field_name, "must be a list of numbers.")
    return [
        non_negative_number(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    ]


def string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise _fail(field_name, "must be a list of strings.")
    return list(value)


def boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(field_name, "must be true or false.")
    return value


def text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise _fail(field_name, "must be a string.")
    return value


# ── registry ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Key:
    path: str
    type: KeyType
    default: Any
    doc: str
    validator: Callable[[Any, str], Any] | None = None
    status: KeyStatus = KeyStatus.SUPPORTED

    @property
    def section(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.path.split(".", 1)[1]

    @property
    def env_var(self) -> str:
        return "HHE_" + self.path.replace(".", "_").upper()

    def coerce(self, value: Any) -> Any:
        """Validate *value* for this key and return the normalised result."""
        if self.status is KeyStatus.DEFAULT_ONLY and value != self.default:
            raise ConfigError(
                f"{self.path} does not support the value {value!r} yet.",
                details=(
                    f"{self.doc} Only the documented default "
                    f"{self.default!r} is accepted today; the option is "
                    "published but not implemented."
                ),
                remedies=(
                    Remedy(
                        f"Remove the key, or set it back to its default: "
                        f"{self.path} = {self.default!r}"
                    ),
                ),
            )
        if self.validator is None:
            return value
        return self.validator(value, self.path)


KEYS: tuple[Key, ...] = (
    Key(
        "home_assistant.url_env",
        KeyType.STRING,
        "HA_URL",
        "Environment variable holding the Home Assistant base URL.",
        text,
    ),
    Key(
        "home_assistant.token_env",
        KeyType.STRING,
        "HA_TOKEN",
        "Environment variable holding the long-lived access token.",
        text,
    ),
    Key(
        "home_assistant.timezone",
        KeyType.STRING,
        "Europe/Berlin",
        "IANA time zone that defines local calendar days.",
        text,
    ),
    Key(
        "homeassistant.url",
        KeyType.STRING,
        "",
        "Home Assistant base URL, for example http://homeassistant.local:8123.",
        text,
    ),
    Key(
        "homeassistant.token",
        KeyType.STRING,
        "",
        "Long-lived access token; stored in the credentials file only.",
        text,
        KeyStatus.SECRET,
    ),
    Key(
        "export.output_dir",
        KeyType.PATH,
        None,  # resolved at load time to paths.default_output_dir()
        "Directory that receives exports, metadata, and logs.",
        text,
    ),
    Key(
        "export.mode",
        KeyType.STRING,
        "all_current_entities",
        "Entity selection mode.",
        text,
        KeyStatus.DEFAULT_ONLY,
    ),
    Key(
        "export.include_current_day",
        KeyType.BOOL,
        False,
        "Whether the still incomplete current day may be exported.",
        boolean,
        KeyStatus.DEFAULT_ONLY,
    ),
    Key(
        "export.resume",
        KeyType.BOOL,
        True,
        "Skip days that already have a successful manifest.",
        boolean,
    ),
    Key(
        "export.force",
        KeyType.BOOL,
        False,
        "Re-export days that already have a successful manifest.",
        boolean,
    ),
    Key(
        "requests.batch_size_entities",
        KeyType.INT,
        5,
        "Entities per history request.",
        positive_int,
    ),
    Key(
        "requests.sleep_between_requests_seconds",
        KeyType.FLOAT,
        1.0,
        "Pause between history requests, in seconds.",
        non_negative_number,
    ),
    Key(
        "requests.sleep_between_days_seconds",
        KeyType.FLOAT,
        5.0,
        "Pause between exported days, in seconds.",
        non_negative_number,
    ),
    Key(
        "requests.request_timeout_seconds",
        KeyType.INT,
        120,
        "HTTP timeout per request, in seconds.",
        positive_int,
    ),
    Key(
        "requests.max_retries",
        KeyType.INT,
        3,
        "Retries on timeouts and 5xx responses.",
        non_negative_int,
    ),
    Key(
        "requests.backoff_seconds",
        KeyType.NUM_LIST,
        [2, 5, 15],
        "Wait times between retries, in seconds.",
        non_negative_number_list,
    ),
    Key("formats.jsonl", KeyType.BOOL, True, "Write JSONL output.", boolean),
    Key("formats.csv", KeyType.BOOL, False, "Write CSV output.", boolean),
    Key(
        "formats.parquet",
        KeyType.BOOL,
        False,
        "Write Parquet output; requires pyarrow.",
        boolean,
    ),
    Key(
        "storage.use_temp_dir",
        KeyType.BOOL,
        True,
        "Stream into a temporary directory before finalizing.",
        boolean,
        KeyStatus.DEFAULT_ONLY,
    ),
    Key(
        "storage.temp_dir",
        KeyType.PATH,
        r"%LOCALAPPDATA%\ha_history_export_tmp",
        "Working directory for temporary export files.",
        text,
    ),
    Key(
        "storage.cloud_storage_retry_count",
        KeyType.INT,
        5,
        "Retries when a synchronisation client holds a file lock.",
        non_negative_int,
    ),
    Key(
        "storage.cloud_storage_retry_sleep_seconds",
        KeyType.FLOAT,
        2.0,
        "Pause between file-lock retries, in seconds.",
        non_negative_number,
    ),
    Key(
        "history_request.minimal_response",
        KeyType.BOOL,
        False,
        "Ask Home Assistant for a reduced history payload.",
        boolean,
    ),
    Key(
        "history_request.no_attributes",
        KeyType.BOOL,
        False,
        "Ask Home Assistant to omit entity attributes.",
        boolean,
    ),
    Key(
        "history_request.significant_changes_only",
        KeyType.BOOL,
        False,
        "Ask Home Assistant for significant changes only.",
        boolean,
    ),
    Key(
        "recorder.export_long_term_statistics",
        KeyType.BOOL,
        False,
        "Long-term statistics export; the REST history endpoint cannot serve it.",
        boolean,
        KeyStatus.DEFAULT_ONLY,
    ),
    Key(
        "recorder.expected_purge_keep_days",
        KeyType.OPTIONAL_INT,
        None,
        "Recorder retention in days; older days are warned about.",
        optional_positive_int,
    ),
    Key(
        "entity_selection.source",
        KeyType.STRING,
        "api_states_runtime",
        "Where the entity list comes from.",
        text,
        KeyStatus.DEFAULT_ONLY,
    ),
    Key(
        "entity_selection.include_unknown",
        KeyType.BOOL,
        True,
        "Request history for entities currently in state unknown.",
        boolean,
    ),
    Key(
        "entity_selection.include_unavailable",
        KeyType.BOOL,
        True,
        "Request history for entities currently in state unavailable.",
        boolean,
    ),
    Key(
        "entity_selection.include_deleted_from_previous_runs",
        KeyType.BOOL,
        False,
        "Add entities that only earlier snapshots contain.",
        boolean,
        KeyStatus.DEFAULT_ONLY,
    ),
    Key(
        "entity_selection.optional_exclude_patterns",
        KeyType.STR_LIST,
        [],
        "Glob patterns for entity IDs to exclude.",
        string_list,
    ),
    Key(
        "entity_selection.optional_exclude_domains",
        KeyType.STR_LIST,
        [],
        "Entity domains to exclude.",
        string_list,
    ),
)

BY_PATH: dict[str, Key] = {key.path: key for key in KEYS}
BY_ENV_VAR: dict[str, Key] = {key.env_var: key for key in KEYS}
SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(key.section for key in KEYS))

#: Sections accepted in configuration files. ``homeassistant`` is the canonical
#: spelling; ``home_assistant`` remains valid for existing files.
KNOWN_SECTIONS = frozenset(SECTIONS)


def default_value(key: Key) -> Any:
    """Return the effective default, resolving the deferred output directory."""
    if key.path == "export.output_dir":
        return str(paths.default_output_dir())
    return key.default


def defaults() -> dict[str, Any]:
    return {key.path: default_value(key) for key in KEYS}


def supported_keys() -> tuple[Key, ...]:
    return tuple(key for key in KEYS if key.status is KeyStatus.SUPPORTED)


def suggest(unknown: str, candidates: Iterable[str] | None = None) -> str | None:
    """Closest known key for a typo, or None when nothing is close enough."""
    pool = list(candidates) if candidates is not None else list(BY_PATH)
    matches = difflib.get_close_matches(unknown, pool, n=1, cutoff=0.7)
    return matches[0] if matches else None
