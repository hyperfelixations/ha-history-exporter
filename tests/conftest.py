from __future__ import annotations

import importlib.util
import logging
from collections.abc import Iterator
from importlib.machinery import ModuleSpec
from pathlib import Path

import pytest

from ha_history_exporter.settings import paths, registry

_HOST_CONFIGURATION_ENV_VARS = (*registry.BY_ENV, registry.TOKEN_ENV, *registry.OTHER_ENV)


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
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
def restore_root_logger() -> Iterator[None]:
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
def without_pyarrow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the optional pyarrow package look uninstalled."""
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
        if name == "pyarrow" or name.startswith("pyarrow."):
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


@pytest.fixture
def synthetic_ha_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install unmistakably synthetic credentials for config-loading tests."""
    monkeypatch.setenv("HHE_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HHE_TOKEN", "synthetic-test-token")
