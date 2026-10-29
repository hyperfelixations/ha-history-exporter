"""The README's key table is the key registry."""

from __future__ import annotations

import dataclasses

import pytest

from ha_history_exporter.settings.registry import KEYS
from tests.repo import tooling

pytestmark = pytest.mark.repo


def test_the_key_table_is_current() -> None:
    tool = tooling.load_tool("readme_keys")
    text = tool.README.read_text(encoding="utf-8")
    assert tool.updated(text) == text, "run python tools/readme_keys.py"


def test_every_default_computed_on_the_machine_is_stated_in_words() -> None:
    computed = {key.path for key in KEYS if key.field.default_factory is not dataclasses.MISSING}
    assert set(tooling.load_tool("readme_keys").MACHINE_DEFAULTS) == computed
