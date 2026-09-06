"""Entry point for ``python -m ha_history_exporter``."""

import sys

from .cli import main

if __name__ == "__main__":
    # Without this, argparse's default prog would be "__main__.py" (from
    # sys.argv[0]) instead of the actual command name shown in --help.
    sys.argv[0] = "ha-history-exporter"
    raise SystemExit(main())
