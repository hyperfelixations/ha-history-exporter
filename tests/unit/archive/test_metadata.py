from __future__ import annotations

from pathlib import Path

from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.metadata import append_run_log


def test_the_run_log_gains_one_lf_terminated_line_per_entry(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    append_run_log(layout, {"day": "2026-07-28", "note": "Größe"})
    append_run_log(layout, {"day": "2026-07-29"})
    assert layout.run_log.read_bytes() == (
        '{"day": "2026-07-28", "note": "Größe"}\n{"day": "2026-07-29"}\n'.encode()
    )


def test_a_truncated_last_line_is_terminated_before_appending(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    layout.run_log.parent.mkdir(parents=True)
    layout.run_log.write_bytes(b'{"day": "2026-07-27"}\r\n{"day": "2026-07-2')
    append_run_log(layout, {"day": "2026-07-28"})
    assert layout.run_log.read_bytes().splitlines()[-2:] == [
        b'{"day": "2026-07-2',
        b'{"day": "2026-07-28"}',
    ]


def test_an_empty_run_log_is_extended_without_a_blank_line(tmp_path: Path) -> None:
    layout = ArchiveLayout(tmp_path / "output")
    layout.run_log.parent.mkdir(parents=True)
    layout.run_log.write_bytes(b"")
    append_run_log(layout, {"day": "2026-07-28"})
    assert layout.run_log.read_bytes() == b'{"day": "2026-07-28"}\n'
