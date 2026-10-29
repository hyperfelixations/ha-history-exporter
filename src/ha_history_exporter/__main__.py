"""Entry point for ``python -m ha_history_exporter``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(prog="python -m ha_history_exporter"))
