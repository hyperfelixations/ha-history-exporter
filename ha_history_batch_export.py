#!/usr/bin/env python3
"""Entry point for the Home Assistant REST History Exporter.

Usage:
    python ha_history_batch_export.py --config export_config.yaml --date yesterday --dry-run
    python ha_history_batch_export.py --config export_config.yaml --date 2026-06-15
    python ha_history_batch_export.py --config export_config.yaml \\
        --start-date 2026-06-15 --end-date 2026-06-21 --resume

Environment variables required:
    HA_URL    e.g. http://homeassistant.local:8123
    HA_TOKEN  Long-Lived Access Token from your HA user profile
"""
import sys

from ha_history_exporter.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
