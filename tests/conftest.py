from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def remove_production_environment(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests cannot inherit credentials for a real HA instance."""
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    yield


@pytest.fixture
def synthetic_ha_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install unmistakably synthetic credentials for config-loading tests."""
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")
