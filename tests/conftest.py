from __future__ import annotations

import logging

import pytest

from tests.helpers import FakeCliClient


@pytest.fixture(autouse=True)
def remove_production_environment(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests cannot inherit credentials for a real HA instance."""
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    yield


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
