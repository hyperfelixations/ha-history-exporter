"""Declarative registry of every configuration key.

The registry is the single source for YAML validation, environment-variable
names, ``config set`` coercion, the generated configuration file, and the key
reference in the README. Every key listed here is honoured; HHE publishes no
setting that has no effect.

Key names mirror the command-line options one to one: ``--batch-size`` is
``requests.batch_size``, ``--timeout`` is ``requests.timeout``. Units belong in
the documentation line, not in the identifier.
"""

from __future__ import annotations

import difflib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from ..errors import ConfigError, Remedy
from . import paths
from .model import (
    FORMAT_ORDER,
    EntitySettings,
    ExportSettings,
    Format,
    HistoryRequestSettings,
    HomeAssistantSettings,
    RecorderSettings,
    RequestSettings,
    StorageSettings,
)

# The configuration model is the single source of every default. Declaring a
# value here as well would let the two drift apart unnoticed; a test compares
# them key by key.
_HOMEASSISTANT = HomeAssistantSettings()
_EXPORT = ExportSettings()
_REQUESTS = RequestSettings()
_HISTORY_REQUEST = HistoryRequestSettings()
_ENTITIES = EntitySettings()
_RECORDER = RecorderSettings()
_STORAGE = StorageSettings()


class KeyType(str, Enum):
    STRING = "string"
    PATH = "path"
    BOOL = "boolean"
    INT = "integer"
    FLOAT = "number"
    OPTIONAL_INT = "integer or null"
    STR_LIST = "list of strings"
    NUM_LIST = "list of numbers"
    FORMAT_LIST = "list of output formats"


class KeyStatus(str, Enum):
    SUPPORTED = "supported"
    SECRET = "secret"  # noqa: S105 - status name, not a secret


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
        raise _fail(field_name, "must be a finite number greater than or equal to 0.")
    return number


def non_negative_number_list(value: Any, field_name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        raise _fail(field_name, "must be a list of numbers.")
    return tuple(
        non_negative_number(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )


def string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise _fail(field_name, "must be a list of strings.")
    return tuple(value)


def boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(field_name, "must be true or false.")
    return value


def text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise _fail(field_name, "must be a string.")
    return value


def format_set(value: Any, field_name: str) -> frozenset[Format]:
    """Accept a YAML list, a comma-separated string, or 'none'.

    ``none`` and the empty list both mean snapshot-only: HHE records the
    current entity states and requests no history at all.
    """
    if isinstance(value, frozenset):
        return value
    if isinstance(value, str):
        items = [part.strip().lower() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = []
        for item in value:
            if not isinstance(item, str):
                raise _fail(field_name, "must list output formats as strings.")
            items.append(item.strip().lower())
    else:
        raise _fail(
            field_name,
            "must be a list of output formats.",
            details=f"Valid formats: {_valid_formats()}, or 'none' for no history.",
        )

    if not items:
        return frozenset()
    if "none" in items:
        if len(items) > 1:
            raise _fail(
                field_name,
                "cannot combine 'none' with another format.",
                details=(
                    "'none' means snapshot-only: no history request and no "
                    "daily manifest."
                ),
            )
        return frozenset()

    selected = set()
    for item in items:
        try:
            selected.add(Format(item))
        except ValueError as exc:
            raise _fail(
                field_name,
                f"does not know the output format {item!r}.",
                details=f"Valid formats: {_valid_formats()}, or 'none'.",
            ) from exc
    return frozenset(selected)


def _valid_formats() -> str:
    return ", ".join(fmt.value for fmt in FORMAT_ORDER)


# ── registry ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Key:
    path: str
    type: KeyType
    default: Any
    doc: str
    validator: Callable[[Any, str], Any] | None = None
    status: KeyStatus = KeyStatus.SUPPORTED
    #: Explicit environment variable, when the generic name would be clumsy.
    env: str | None = None

    @property
    def section(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.path.split(".", 1)[1]

    @property
    def env_var(self) -> str:
        if self.env is not None:
            return self.env
        return "HHE_" + self.path.replace(".", "_").upper()

    def coerce(self, value: Any) -> Any:
        """Validate *value* for this key and return the normalised result."""
        if self.validator is None:
            return value
        return self.validator(value, self.path)


KEYS: tuple[Key, ...] = (
    Key(
        "homeassistant.url",
        KeyType.STRING,
        _HOMEASSISTANT.url,
        "Home Assistant base URL, for example http://homeassistant.local:8123.",
        text,
        env="HHE_URL",
    ),
    Key(
        "homeassistant.token",
        KeyType.STRING,
        "",
        "Long-lived access token; stored in the credentials file only.",
        text,
        KeyStatus.SECRET,
        env="HHE_TOKEN",
    ),
    Key(
        "export.output_dir",
        KeyType.PATH,
        None,  # resolved at load time to paths.default_output_dir()
        "Directory that receives exports, metadata, and logs.",
        text,
    ),
    Key(
        "export.timezone",
        KeyType.STRING,
        _EXPORT.timezone,
        "IANA time zone that defines local calendar days.",
        text,
    ),
    Key(
        "export.formats",
        KeyType.FORMAT_LIST,
        [fmt.value for fmt in FORMAT_ORDER if fmt in _EXPORT.formats],
        "Output formats: any of jsonl, csv, parquet - or none for snapshot only.",
        format_set,
    ),
    Key(
        "requests.batch_size",
        KeyType.INT,
        _REQUESTS.batch_size,
        "Entities per history request.",
        positive_int,
    ),
    Key(
        "requests.sleep_between_requests",
        KeyType.FLOAT,
        _REQUESTS.sleep_between_requests,
        "Pause between history requests, in seconds.",
        non_negative_number,
    ),
    Key(
        "requests.sleep_between_days",
        KeyType.FLOAT,
        _REQUESTS.sleep_between_days,
        "Pause between exported days, in seconds.",
        non_negative_number,
    ),
    Key(
        "requests.timeout",
        KeyType.INT,
        _REQUESTS.timeout,
        "HTTP timeout per request, in seconds.",
        positive_int,
    ),
    Key(
        "requests.max_retries",
        KeyType.INT,
        _REQUESTS.max_retries,
        "Retries on timeouts and 5xx responses.",
        non_negative_int,
    ),
    Key(
        "requests.backoff",
        KeyType.NUM_LIST,
        list(_REQUESTS.backoff),
        "Wait times between retries, in seconds.",
        non_negative_number_list,
    ),
    Key(
        "history_request.minimal_response",
        KeyType.BOOL,
        _HISTORY_REQUEST.minimal_response,
        "Ask Home Assistant for a reduced history payload.",
        boolean,
    ),
    Key(
        "history_request.no_attributes",
        KeyType.BOOL,
        _HISTORY_REQUEST.no_attributes,
        "Ask Home Assistant to omit entity attributes.",
        boolean,
    ),
    Key(
        "history_request.significant_changes_only",
        KeyType.BOOL,
        _HISTORY_REQUEST.significant_changes_only,
        "Ask Home Assistant for significant changes only.",
        boolean,
    ),
    Key(
        "entities.include_unknown",
        KeyType.BOOL,
        _ENTITIES.include_unknown,
        "Request history for entities currently in state unknown.",
        boolean,
    ),
    Key(
        "entities.include_unavailable",
        KeyType.BOOL,
        _ENTITIES.include_unavailable,
        "Request history for entities currently in state unavailable.",
        boolean,
    ),
    Key(
        "entities.exclude_domains",
        KeyType.STR_LIST,
        list(_ENTITIES.exclude_domains),
        "Entity domains to exclude, for example update or button.",
        string_list,
    ),
    Key(
        "entities.exclude_patterns",
        KeyType.STR_LIST,
        list(_ENTITIES.exclude_patterns),
        "Glob patterns for entity IDs to exclude, for example sensor.*_debug.",
        string_list,
    ),
    Key(
        "recorder.purge_keep_days",
        KeyType.OPTIONAL_INT,
        _RECORDER.purge_keep_days,
        "Recorder retention in days; HHE warns before requesting older days.",
        optional_positive_int,
    ),
    Key(
        "storage.temp_dir",
        KeyType.PATH,
        None,  # resolved at load time to paths.default_temp_root()
        "Working directory for temporary export files.",
        text,
    ),
    Key(
        "storage.locked_file_retries",
        KeyType.INT,
        _STORAGE.locked_file_retries,
        "Retries when another program holds a lock on an output file.",
        non_negative_int,
    ),
    Key(
        "storage.locked_file_retry_sleep",
        KeyType.FLOAT,
        _STORAGE.locked_file_retry_sleep,
        "Pause between file-lock retries, in seconds.",
        non_negative_number,
    ),
)

BY_PATH: dict[str, Key] = {key.path: key for key in KEYS}
BY_ENV_VAR: dict[str, Key] = {key.env_var: key for key in KEYS}
SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(key.section for key in KEYS))
KNOWN_SECTIONS = frozenset(SECTIONS)

#: Legacy environment variables kept because they are widely configured
#: already. They take precedence over the HHE_ spellings.
LEGACY_ENV_VARS: dict[str, str] = {
    "HA_URL": "homeassistant.url",
    "HA_TOKEN": "homeassistant.token",
}

#: Defaults that depend on the platform and are therefore resolved at load time.
_DEFERRED_DEFAULTS = {
    "export.output_dir": paths.default_output_dir,
    "storage.temp_dir": paths.default_temp_root,
}


def default_value(key: Key) -> Any:
    """Return the effective default, resolving platform-dependent paths."""
    deferred = _DEFERRED_DEFAULTS.get(key.path)
    if deferred is not None:
        return str(deferred())
    return key.default


def defaults() -> dict[str, Any]:
    return {key.path: default_value(key) for key in KEYS}


def public_keys() -> tuple[Key, ...]:
    """Keys that belong in a configuration file; secrets never do."""
    return tuple(key for key in KEYS if key.status is not KeyStatus.SECRET)


def suggest(unknown: str, candidates: Iterable[str] | None = None) -> str | None:
    """Closest known key for a typo, or None when nothing is close enough."""
    pool = list(candidates) if candidates is not None else list(BY_PATH)
    matches = difflib.get_close_matches(unknown, pool, n=1, cutoff=0.6)
    return matches[0] if matches else None
