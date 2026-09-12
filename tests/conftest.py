from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ha_history_exporter.settings import paths, schema
from tests.helpers import FakeCliClient

_HOST_CONFIGURATION_ENV_VARS = tuple(
    dict.fromkeys(
        [
            *(key.env_var for key in schema.KEYS),
            *schema.LEGACY_ENV_VARS,
            paths.ENV_CONFIG_DIR,
        ]
    )
)


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Isolate every test from real credentials, user files, and project files.

    One fixture owns the whole environment so no ordering between autouse
    fixtures can leave a gap: every declared HHE setting is removed by its
    public name, the user configuration directory is redirected into the
    test's temporary path, and the working directory is moved away from the
    checkout. The fixture never inventories the host environment or examines
    a real user directory.
    """
    for name in _HOST_CONFIGURATION_ENV_VARS:
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
