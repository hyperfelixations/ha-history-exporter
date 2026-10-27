"""What every command shares: a named configuration file and the report of a defect."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.support.fake_source import FakeHistorySource
from tests.support.terminal import Terminal, states


@pytest.fixture
def terminal(tmp_path: Path) -> Terminal:
    terminal = Terminal(
        tmp_path / "t",
        environ={"HHE_URL": "http://homeassistant.local:8123", "HHE_TOKEN": "synthetic-token"},
    )
    terminal.source = FakeHistorySource(states=states(["sensor.a"]))
    return terminal


@pytest.mark.parametrize(
    "argv",
    [
        ("export", "--dry-run"),
        ("convert", "--format", "csv"),
        ("verify",),
        ("status",),
        ("doctor", "--offline"),
        ("config", "list"),
        ("config", "get", "export.output_dir"),
    ],
    ids=lambda argv: " ".join(argv),
)
def test_every_command_reads_a_named_configuration_file(
    terminal: Terminal, argv: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    named = terminal.cwd / "named.toml"
    named.write_text('[export]\noutput_dir = "named-archive"\n', encoding="utf-8")
    command, *rest = argv
    assert terminal.run(command, "--config", "named.toml", *rest) == 0, terminal.stderr
    if argv[:2] == ("config", "get"):
        assert capsys.readouterr().out.strip() == str(terminal.cwd / "named-archive")


class _Defective(FakeHistorySource):
    def check_api(self) -> None:
        raise ZeroDivisionError("synthetic defect")


def test_a_defect_without_a_run_log_leaves_its_traceback_in_a_crash_log(
    terminal: Terminal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "system-temp"))
    terminal.source = _Defective()

    assert terminal.run("doctor") == 70

    (crash,) = (tmp_path / "system-temp" / "ha-history-exporter").glob("crash-*.log")
    assert "ZeroDivisionError: synthetic defect" in crash.read_text(encoding="utf-8")
    assert "Traceback" not in terminal.stderr
    assert f"Log file: {crash}" in terminal.stderr
    assert "Its traceback is in the log file named below" in terminal.stderr
