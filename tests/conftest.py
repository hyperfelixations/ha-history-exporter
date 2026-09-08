from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from ha_history_exporter.settings import paths
from tests.helpers import FakeCliClient


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Isolate every test from real credentials, user files, and project files.

    One fixture owns the whole environment so no ordering between autouse
    fixtures can leave a gap: production credentials are removed, the user
    configuration directory is redirected into the test's temporary path, and
    the working directory is moved out of the repository so configuration
    discovery cannot pick up the production export_config.yaml.
    """
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    for name in [n for n in os.environ if n.startswith("HHE_")]:
        monkeypatch.delenv(name, raising=False)

    config_dir = tmp_path / "hhe-config"
    working_dir = tmp_path / "cwd"
    home_dir = tmp_path / "home"
    for directory in (config_dir, working_dir, home_dir):
        directory.mkdir()
    monkeypatch.setenv(paths.ENV_CONFIG_DIR, str(config_dir))
    monkeypatch.chdir(working_dir)

    # The default output directory is derived from the home directory.
    # Without this, a test that exports without an explicit output_dir
    # would write into the real profile.
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home_dir))

    yield config_dir


@pytest.fixture(scope="session", autouse=True)
def real_user_directories_are_never_touched():
    """Fail the session if a test wrote into the real user profile.

    Covers both places a run would otherwise land: the configuration
    directory and the default output directory below the home directory.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv(paths.ENV_CONFIG_DIR, raising=False)
    watched = [paths.user_config_dir(), paths.default_output_dir()]
    monkeypatch.undo()

    def snapshot() -> list[tuple[bool, list[str]]]:
        return [
            (
                directory.exists(),
                sorted(p.name for p in directory.iterdir())
                if directory.exists()
                else [],
            )
            for directory in watched
        ]

    before = snapshot()
    yield
    after = snapshot()

    for directory, was, now in zip(watched, before, after, strict=True):
        assert was == now, f"tests modified the real directory {directory}"


@pytest.fixture(autouse=True)
def reset_fake_cli_client():
    """Restore the shared CLI client stand-in before every test."""
    FakeCliClient.reset()
    yield
    FakeCliClient.reset()


@pytest.fixture(autouse=True)
def restore_root_logger():
    """Keep logging configured by one CLI invocation out of later tests."""
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level

    yield

    for handler in list(root_logger.handlers):
        if handler not in original_handlers:
            root_logger.removeHandler(handler)
            handler.close()
    root_logger.setLevel(original_level)


@pytest.fixture
def synthetic_ha_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install unmistakably synthetic credentials for config-loading tests."""
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")
