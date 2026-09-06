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
    config_dir.mkdir()
    working_dir.mkdir()
    monkeypatch.setenv(paths.ENV_CONFIG_DIR, str(config_dir))
    monkeypatch.chdir(working_dir)
    yield config_dir


@pytest.fixture(scope="session", autouse=True)
def real_user_config_dir_is_never_touched():
    """Fail the session if any test wrote into the real configuration directory."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv(paths.ENV_CONFIG_DIR, raising=False)
    real_dir = paths.user_config_dir()
    monkeypatch.undo()

    existed = real_dir.exists()
    before = sorted(p.name for p in real_dir.iterdir()) if existed else []

    yield

    now_exists = real_dir.exists()
    after = sorted(p.name for p in real_dir.iterdir()) if now_exists else []
    assert (existed, before) == (now_exists, after), (
        f"tests modified the real user configuration directory {real_dir}"
    )


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
