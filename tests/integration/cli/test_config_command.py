"""``hhe config``: effective values, checked writes, the token, the editor."""

from __future__ import annotations

from pathlib import Path

import pytest

from ha_history_exporter.cli.commands.config import editor_command
from tests.support.terminal import Terminal

TOKEN = "synthetic-test-token"


@pytest.fixture
def terminal(tmp_path: Path) -> Terminal:
    return Terminal(tmp_path / "t")


def config_file(terminal: Terminal) -> Path:
    return terminal.config_dir / "config.toml"


def test_a_value_set_is_the_value_read(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert terminal.run("config", "set", "requests.batch_size", "20") == 0
    assert terminal.run("config", "get", "requests.batch_size") == 0
    assert capsys.readouterr().out.splitlines()[-1] == "20"
    assert "batch_size = 20" in config_file(terminal).read_text(encoding="utf-8")


def test_reads_report_the_environment_and_never_the_token(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal.environ.update({"HHE_REQUESTS_TIMEOUT": "30", "HHE_TOKEN": TOKEN})
    assert terminal.run("config", "list", "--origin") == 0
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert "homeassistant.token" in output
    assert "<set>  [env:HHE_TOKEN]" in output
    assert "requests.timeout" in output
    assert "30  [env:HHE_REQUESTS_TIMEOUT]" in output


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        (
            "homeassistant.url",
            "ftp://home-assistant.invalid",
            "not a valid Home Assistant base URL",
        ),
        ("export.timezone", "Mars/Olympus", "not a known IANA time zone"),
        ("requests.batch_size", "zero", "at least 1"),
        ("requests.batchsize", "3", "closest key is 'requests.batch_size'"),
    ],
)
def test_an_invalid_value_is_refused_and_the_file_unchanged(
    terminal: Terminal, key: str, value: str, message: str
) -> None:
    terminal.run("config", "set", "requests.timeout", "30")
    before = config_file(terminal).read_bytes()
    assert terminal.run("config", "set", key, value) == 2
    assert message in " ".join(terminal.stderr.split())
    assert config_file(terminal).read_bytes() == before


def test_an_environment_value_is_never_written_to_the_file(terminal: Terminal) -> None:
    terminal.environ["HHE_REQUESTS_TIMEOUT"] = "30"
    terminal.run("config", "set", "requests.batch_size", "20")
    assert "timeout = 30" not in config_file(terminal).read_text(encoding="utf-8")


def test_unset_falls_back_and_says_from_where(
    terminal: Terminal, capsys: pytest.CaptureFixture[str]
) -> None:
    terminal.run("config", "set", "requests.batch_size", "20")
    assert terminal.run("config", "unset", "requests.batch_size") == 0
    assert capsys.readouterr().out.splitlines()[-1] == "requests.batch_size = 15  [default]"
    assert terminal.run("config", "unset", "requests.batch_size") == 0


def test_the_token_is_read_hidden_or_piped_never_from_the_command_line(
    terminal: Terminal,
) -> None:
    credentials = terminal.config_dir / "credentials.toml"
    assert terminal.run("config", "set", "homeassistant.token", TOKEN) == 2
    assert TOKEN not in terminal.stderr
    assert not credentials.exists()
    assert terminal.run("config", "set", "homeassistant.token") == 2
    assert "interactive terminal" in terminal.stderr
    terminal.tty, terminal.secrets = True, [TOKEN]
    assert terminal.run("config", "set", "homeassistant.token") == 0
    assert TOKEN in credentials.read_text(encoding="utf-8")
    terminal.tty, terminal.stdin = False, "piped-synthetic-token\n"
    assert terminal.run("config", "set", "homeassistant.token", "--stdin") == 0
    assert "piped-synthetic-token" in credentials.read_text(encoding="utf-8")
    assert terminal.run("config", "unset", "homeassistant.token") == 0
    assert not credentials.exists()


def test_writes_refuse_a_named_file(terminal: Terminal) -> None:
    named = terminal.cwd / "named.toml"
    named.write_text("", encoding="utf-8")
    assert terminal.run("config", "--config", str(named), "set", "requests.timeout", "3") == 2
    assert named.read_text(encoding="utf-8") == ""


def test_path_lists_every_location(terminal: Terminal, capsys: pytest.CaptureFixture[str]) -> None:
    assert terminal.run("config", "path") == 0
    output = capsys.readouterr().out
    for label in (
        "configuration file",
        "credentials file",
        "output directory",
        "archive record",
        "log directory",
        "temporary directory",
    ):
        assert label in output


def test_edit_opens_the_editor_and_checks_the_result(terminal: Terminal) -> None:
    commands: list[list[str]] = []

    def editor(command: list[str]) -> int:
        commands.append(command)
        Path(command[-1]).write_text("[requests]\nbatch_size = 0\n", encoding="utf-8")
        return 0

    terminal.environ["EDITOR"] = "synthetic-editor --wait"
    terminal.editor = editor
    assert terminal.run("config", "edit") == 2
    assert commands == [["synthetic-editor", "--wait", str(config_file(terminal))]]
    assert "requests.batch_size from config.toml, line 2" in terminal.stderr
    assert config_file(terminal).read_text(encoding="utf-8") == "[requests]\nbatch_size = 0\n"


def test_an_editor_that_fails_is_reported(terminal: Terminal) -> None:
    terminal.environ["VISUAL"] = "synthetic-editor"
    terminal.editor = lambda _command: 3
    assert terminal.run("config", "edit") == 2
    assert "The editor ended with exit code 3." in terminal.stderr


def test_without_an_editor_the_default_application_or_a_hint(terminal: Terminal) -> None:
    assert terminal.run("config", "edit") == 2
    assert "EDITOR" in terminal.stderr
    terminal.default_app = True
    assert terminal.run("config", "edit") == 0


@pytest.mark.parametrize(
    ("editor", "windows", "command"),
    [
        ("code --wait", False, ["code", "--wait"]),
        ("'/opt/my editor/ed' -n", False, ["/opt/my editor/ed", "-n"]),
        (
            '"C:\\Program Files\\Ed\\ed.exe" --wait',
            True,
            ["C:\\Program Files\\Ed\\ed.exe", "--wait"],
        ),
        ("notepad", True, ["notepad"]),
    ],
)
def test_an_editor_with_spaces_in_its_path_is_split_correctly(
    editor: str, windows: bool, command: list[str]
) -> None:
    assert editor_command(editor, windows=windows) == command
