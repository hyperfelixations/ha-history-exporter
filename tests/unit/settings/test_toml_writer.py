from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ha_history_exporter.settings.registry import KEYS, SECTIONS
from ha_history_exporter.settings.sources import document_values
from ha_history_exporter.settings.toml_writer import (
    encode_string,
    encode_value,
    render_config,
    render_credentials,
)
from ha_history_exporter.settings.validators import ParseContext, TomlValue
from tests.unit.settings import samples

_TEXT = st.text(st.characters(exclude_categories=["Cs"]))


@given(_TEXT)
def test_every_string_round_trips(value: str) -> None:
    assert tomllib.loads(f"x = {encode_string(value)}")["x"] == value


def test_windows_paths_are_written_as_literal_strings() -> None:
    assert encode_string("D:\\archive\\exports") == "'D:\\archive\\exports'"
    assert encode_string("it's\\here") == '"it\'s\\\\here"'
    assert encode_string("a\x7fb") == '"a\\u007Fb"'


_SCALARS = (
    st.booleans()
    | st.integers(-(2**63), 2**63 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | _TEXT
)


@given(st.lists(_SCALARS, max_size=5) | _SCALARS)
def test_every_value_round_trips(value: TomlValue) -> None:
    assert tomllib.loads(f"x = {encode_value(value)}")["x"] == value


def test_a_value_that_toml_cannot_hold_is_refused() -> None:
    with pytest.raises(ValueError, match="finite"):
        encode_value(float("inf"))


def test_the_empty_document_documents_every_key() -> None:
    text = render_config({})
    assert tomllib.loads(text) == {section: {} for section in SECTIONS}
    for key in KEYS:
        assert f"# {key.name} = " in text, key.path


def test_a_rendered_document_reads_back_every_value(tmp_path: Path) -> None:
    ctx = ParseContext(base_dir=tmp_path, environ={})
    values: dict[str, TomlValue] = {
        key.path: key.setting.kind.to_toml(key.setting.kind.from_text(samples.valid_text(key), ctx))
        for key in KEYS
    }
    text = render_config(values)
    path = tmp_path / "config.toml"
    assert document_values(tomllib.loads(text), path, text) == values
    assert "# " + "url = " not in text


def test_only_registered_keys_are_rendered() -> None:
    with pytest.raises(ValueError, match="not configuration keys"):
        render_config({"homeassistant.token": "x"})


def test_the_credentials_document_holds_only_the_token() -> None:
    assert tomllib.loads(render_credentials('synthetic"token')) == {
        "homeassistant": {"token": 'synthetic"token'}
    }
