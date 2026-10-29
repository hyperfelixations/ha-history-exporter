"""The suite cannot reach the network or a real Home Assistant configuration."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

from ha_history_exporter.settings import paths, registry


def test_socket_access_is_blocked_by_default() -> None:
    with (
        pytest.warns(UserWarning, match="tried to use socket.socket"),
        pytest.raises(SocketBlockedError),
    ):
        socket.socket()


def test_every_hhe_variable_is_removed_or_points_into_the_test(tmp_path: Path) -> None:
    for name in (*registry.BY_ENV, registry.TOKEN_ENV, *registry.OTHER_ENV):
        if name == paths.ENV_CONFIG_DIR:
            assert Path(os.environ[name]).is_relative_to(tmp_path)
        else:
            assert name not in os.environ
