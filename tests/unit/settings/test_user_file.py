"""``config set`` and ``config unset``: a document is resolved before it is written."""

from __future__ import annotations

from pathlib import Path

import pytest

from ha_history_exporter.errors import ConfigError
from ha_history_exporter.settings import resolve
from ha_history_exporter.settings.paths import ENV_CONFIG_DIR
from ha_history_exporter.settings.registry import KEYS, Key
from ha_history_exporter.settings.user_file import set_value, stored_values, unset_value
from ha_history_exporter.settings.validators import ParseContext
from tests.unit.settings import samples


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    config_dir, cwd = tmp_path / "setup" / "config", tmp_path / "setup" / "cwd"
    config_dir.mkdir(parents=True)
    cwd.mkdir()
    return config_dir, cwd


def _set(dirs: tuple[Path, Path], key_path: str, raw: str) -> object:
    config_dir, cwd = dirs
    return set_value(
        config_dir / "config.toml",
        key_path,
        raw,
        environ={ENV_CONFIG_DIR: str(config_dir)},
        cwd=cwd,
    )


def _effective(dirs: tuple[Path, Path], key: Key) -> object:
    config_dir, cwd = dirs
    config = resolve(environ={ENV_CONFIG_DIR: str(config_dir)}, cwd=cwd).config
    return getattr(getattr(config, key.section), key.name)


@pytest.mark.parametrize("key", KEYS, ids=lambda key: key.path)
def test_every_key_can_be_set_and_is_then_in_effect(dirs: tuple[Path, Path], key: Key) -> None:
    config_dir, cwd = dirs
    raw = samples.valid_text(key)
    stored = _set(dirs, key.path, raw)
    assert stored_values(config_dir / "config.toml") == {key.path: stored}
    expected = key.setting.kind.from_text(raw, ParseContext(base_dir=cwd, environ={}))
    assert _effective(dirs, key) == expected


@pytest.mark.parametrize(
    "key", [key for key in KEYS if samples.invalid_text(key) is not None], ids=lambda key: key.path
)
def test_an_invalid_value_never_reaches_the_file(dirs: tuple[Path, Path], key: Key) -> None:
    path = dirs[0] / "config.toml"
    _set(dirs, "requests.batch_size", "20")
    before = path.read_bytes()
    raw = samples.invalid_text(key)
    assert raw is not None
    with pytest.raises(ConfigError, match=key.path):
        _set(dirs, key.path, raw)
    assert path.read_bytes() == before


def test_a_document_that_would_not_resolve_is_not_written(dirs: tuple[Path, Path]) -> None:
    path = dirs[0] / "config.toml"
    with pytest.raises(ConfigError, match=r"requests.backoff needs at least one wait time"):
        _set(dirs, "requests.backoff", "")
    assert not path.exists()
    _set(dirs, "requests.max_retries", "0")
    _set(dirs, "requests.backoff", "")
    assert stored_values(path) == {"requests.max_retries": 0, "requests.backoff": []}


def test_a_path_from_the_command_line_is_stored_absolute(dirs: tuple[Path, Path]) -> None:
    _, cwd = dirs
    assert _set(dirs, "export.output_dir", "archive") == str(cwd / "archive")


def test_values_of_other_keys_stay_as_written(dirs: tuple[Path, Path]) -> None:
    path = dirs[0] / "config.toml"
    path.write_text('[export]\noutput_dir = "archive"\n', encoding="utf-8")
    _set(dirs, "requests.timeout", "30")
    assert stored_values(path) == {"export.output_dir": "archive", "requests.timeout": 30}


def test_an_empty_value_does_not_unset(dirs: tuple[Path, Path]) -> None:
    with pytest.raises(ConfigError, match="An empty value") as caught:
        _set(dirs, "recorder.purge_keep_days", "")
    assert caught.value.remedies[0].command == "hhe config unset recorder.purge_keep_days"


def test_unset_removes_only_what_is_set(dirs: tuple[Path, Path]) -> None:
    config_dir, _ = dirs
    path = config_dir / "config.toml"
    environ = {ENV_CONFIG_DIR: str(config_dir)}
    assert not unset_value(path, "requests.timeout", environ=environ)
    _set(dirs, "requests.timeout", "30")
    assert unset_value(path, "requests.timeout", environ=environ)
    assert stored_values(path) == {}
