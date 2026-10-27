from __future__ import annotations

from pathlib import Path

import platformdirs
import pytest

from ha_history_exporter.settings import paths


def test_the_configuration_directory_follows_the_override(tmp_path: Path) -> None:
    environ = {paths.ENV_CONFIG_DIR: str(tmp_path / "hhe")}
    assert paths.user_config_file(environ) == tmp_path / "hhe" / "config.toml"
    assert paths.user_credentials_file(environ) == tmp_path / "hhe" / "credentials.toml"


def test_without_override_the_directory_is_the_platform_one() -> None:
    directory = paths.user_config_dir({})
    assert directory.is_absolute()
    assert directory.name == paths.APP_NAME


def test_a_relative_platform_answer_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(platformdirs, "user_config_dir", lambda *_a, **_k: "relative")
    environ = {"APPDATA": str(tmp_path / "roaming")}
    assert paths.user_config_dir(environ) == tmp_path / "roaming" / paths.APP_NAME
    assert "not absolute" in caplog.text
    assert paths.user_config_dir({}) == Path.home() / ".config" / paths.APP_NAME
