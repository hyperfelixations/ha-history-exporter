"""The key registry, derived from the fields of :class:`~.model.Config`.

A key is ``<section>.<name>``: the section is a field of ``Config``, the name
a field of that section. Environment variables are ``HHE_<SECTION>_<NAME>``
unless the key declares a shorter one. The access token is the one key outside
``Config``; it has no default and is never written to the configuration file.
See internal dev doc § "Configuration registry".
"""

from __future__ import annotations

import dataclasses
import difflib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, get_type_hints

from . import paths
from .model import SETTING, Config, Setting

TOKEN_KEY = "homeassistant.token"  # noqa: S105 - key path, not a secret
TOKEN_ENV = "HHE_TOKEN"  # noqa: S105 - variable name, not a secret
#: Documentation of the token key, which has no field in Config.
TOKEN_DOC = "Long-lived access token of Home Assistant; kept in the credentials file."  # noqa: S105

#: HHE_* variables that configure something other than a key.
OTHER_ENV = frozenset({paths.ENV_CONFIG_DIR, "HHE_NO_COLOR"})


@dataclass(frozen=True)
class Key:
    section: str
    name: str
    setting: Setting
    field: dataclasses.Field[Any]

    @property
    def path(self) -> str:
        return f"{self.section}.{self.name}"

    @property
    def doc(self) -> str:
        return self.setting.doc

    @property
    def env_var(self) -> str:
        return self.setting.env or f"HHE_{self.section}_{self.name}".upper()

    def default(self) -> Any:
        if self.field.default_factory is not dataclasses.MISSING:
            return self.field.default_factory()
        return self.field.default


def _section_types() -> dict[str, type[Any]]:
    hints = get_type_hints(Config)
    return {section.name: hints[section.name] for section in dataclasses.fields(Config)}


def _keys() -> tuple[Key, ...]:
    keys = []
    for section, section_type in _section_types().items():
        for member in dataclasses.fields(section_type):
            keys.append(Key(section, member.name, member.metadata[SETTING], member))
    return tuple(keys)


KEYS: tuple[Key, ...] = _keys()
BY_PATH: Mapping[str, Key] = {key.path: key for key in KEYS}
BY_ENV: Mapping[str, Key] = {key.env_var: key for key in KEYS}
SECTIONS: tuple[str, ...] = tuple(_section_types())
BY_OPTION: Mapping[str, Key] = {key.setting.cli: key for key in KEYS if key.setting.cli}


def build_config(values: Mapping[str, Any]) -> Config:
    """``Config`` from a value for every key path."""
    sections = {
        section: section_type(
            **{
                member.name: values[f"{section}.{member.name}"]
                for member in dataclasses.fields(section_type)
            }
        )
        for section, section_type in _section_types().items()
    }
    return Config(**sections)


def values_of(config: Config) -> dict[str, Any]:
    """The value of every key path in *config*."""
    return {key.path: getattr(getattr(config, key.section), key.name) for key in KEYS}


def suggest(unknown: str) -> str | None:
    """The closest known key path for a typo."""
    matches = difflib.get_close_matches(unknown, [*BY_PATH, TOKEN_KEY], n=1, cutoff=0.6)
    return matches[0] if matches else None


def unknown_environment(environ: Mapping[str, str]) -> tuple[str, ...]:
    """HHE_* variables that configure nothing, typically misspelt key names."""
    known = {*BY_ENV, TOKEN_ENV, *OTHER_ENV}
    return tuple(sorted(name for name in environ if name.startswith("HHE_") and name not in known))
