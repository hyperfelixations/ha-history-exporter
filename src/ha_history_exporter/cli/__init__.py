"""The command line: ``hhe``, ``ha-history-exporter`` and ``python -m ha_history_exporter``."""

from __future__ import annotations

from .main import main


def hhe() -> int:
    return main(prog="hhe")


def ha_history_exporter() -> int:
    return main(prog="ha-history-exporter")


__all__ = ["ha_history_exporter", "hhe", "main"]
