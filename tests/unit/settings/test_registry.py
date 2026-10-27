from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ha_history_exporter.domain.formats import Format
from ha_history_exporter.settings.model import Config, RequestSettings
from ha_history_exporter.settings.registry import (
    BY_ENV,
    BY_OPTION,
    BY_PATH,
    KEYS,
    SECTIONS,
    TOKEN_ENV,
    TOKEN_KEY,
    build_config,
    suggest,
    unknown_environment,
)
from ha_history_exporter.settings.validators import ParseContext
from tests.unit.settings import samples


def test_the_registry_has_one_key_per_section_field() -> None:
    assert tuple(field.name for field in dataclasses.fields(Config)) == SECTIONS
    assert len(BY_PATH) == len(KEYS)
    assert all(key.path.count(".") == 1 for key in KEYS)
    assert TOKEN_KEY not in BY_PATH


def test_every_key_is_documented_with_a_unique_variable() -> None:
    assert all(key.doc.endswith(".") for key in KEYS)
    assert len(BY_ENV) == len(KEYS)
    assert all(name.startswith("HHE_") and name.isupper() for name in BY_ENV)
    assert TOKEN_ENV not in BY_ENV
    assert BY_ENV["HHE_URL"].path == "homeassistant.url"
    assert BY_ENV["HHE_REQUESTS_BATCH_SIZE"].path == "requests.batch_size"


def test_options_override_the_keys_they_name() -> None:
    assert all(option.startswith("--") for option in BY_OPTION)
    assert {key.path for key in BY_OPTION.values()} >= {
        "export.output_dir",
        "export.timezone",
        "export.formats",
        "requests.batch_size",
        "requests.timeout",
        "requests.max_retries",
    }


def test_every_default_survives_its_own_kind(tmp_path: Path) -> None:
    ctx = ParseContext(base_dir=tmp_path, environ={})
    for key in KEYS:
        default = key.default()
        if default is None:
            assert key.setting.example is not None, key.path
            continue
        kind = key.setting.kind
        assert kind.from_toml(kind.to_toml(default), ctx) == default, key.path


def test_a_configuration_is_rebuilt_from_its_values() -> None:
    config = Config()
    values = {key.path: getattr(getattr(config, key.section), key.name) for key in KEYS}
    assert build_config(values) == config


def test_the_defaults_are_the_proven_ones() -> None:
    config = Config()
    assert config.requests == RequestSettings(15, 0.7, 5.0, 120, 3, (2.0, 5.0, 15.0))
    assert config.export.formats == {Format.JSONL}
    assert config.export.timezone is None
    assert config.homeassistant.url is None
    assert config.recorder.purge_keep_days is None
    assert config.history_request.skip_initial_state
    assert not config.history_request.minimal_response
    assert config.export.output_dir == Path.home() / "ha-history-exports"


def test_a_configuration_cannot_be_changed() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Config().requests.batch_size = 3  # type: ignore[misc]


def test_every_kind_has_test_samples() -> None:
    assert samples.kinds_in_use() <= samples.covered()


def test_typos_find_their_key() -> None:
    assert suggest("requests.batchsize") == "requests.batch_size"
    assert suggest("homeassistant.tokn") == TOKEN_KEY
    assert suggest("completely.different") is None


def test_unknown_hhe_variables_are_reported_by_name() -> None:
    environ = {
        "HHE_URL": "x",
        "HHE_TOKEN": "x",
        "HHE_CONFIG_DIR": "x",
        "HHE_NO_COLOR": "1",
        "HHE_EXPORT_OUTPUTDIR": "x",
        "PATH": "x",
    }
    assert unknown_environment(environ) == ("HHE_EXPORT_OUTPUTDIR",)
