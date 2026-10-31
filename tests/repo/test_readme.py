"""The README is what a reader can copy: its key table, commands and links hold."""

from __future__ import annotations

import dataclasses
import re
import shlex
from collections import Counter

import pytest

from ha_history_exporter.cli.parser import build_parser
from ha_history_exporter.settings.registry import BY_PATH, KEYS, TOKEN_KEY
from tests.repo import tooling
from tests.support.commands import PLACEHOLDER, descend, strip_program

pytestmark = pytest.mark.repo

TEXT = (tooling.ROOT / "README.md").read_text(encoding="utf-8")
FENCE = re.compile(r"^```(\S*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
#: A fence holding terminal output or a file listing rather than something to run.
LISTING = "text"
PROSE = FENCE.sub("", TEXT)
PROMPT = re.compile(r"^\s*(?:\$|PS\b[^>\n]*>)\s", re.MULTILINE)
#: What ends the command on a line: a comment, a pipe or a redirection.
COMMAND_END = re.compile(r"\s[#|<]\s")
IMAGE = re.compile(r"!\[|<img\b", re.IGNORECASE)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
ANCHOR = re.compile(r"\]\(#([^)]+)\)")


def documented_commands() -> list[str]:
    fenced = [
        line.strip()
        for language, body in FENCE.findall(TEXT)
        if language != LISTING
        for line in body.splitlines()
    ]
    lines = [*fenced, *re.findall(r"`(hhe\s[^`]+)`", PROSE)]
    commands = {COMMAND_END.split(line)[0].strip() for line in lines if line.startswith("hhe ")}
    return sorted(commands)


COMMANDS = documented_commands()


def slug(heading: str) -> str:
    """GitHub's anchor of a heading."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"[`*]", "", text).lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors() -> set[str]:
    seen: Counter[str] = Counter()
    found = set()
    for heading in HEADING.findall(PROSE):
        name = slug(heading)
        found.add(f"{name}-{seen[name]}" if seen[name] else name)
        seen[name] += 1
    return found


def test_the_key_table_is_current() -> None:
    tool = tooling.load_tool("readme_keys")
    text = tool.README.read_text(encoding="utf-8")
    assert tool.updated(text) == text, "run python tools/readme_keys.py"


def test_every_default_computed_on_the_machine_is_stated_in_words() -> None:
    computed = {key.path for key in KEYS if key.field.default_factory is not dataclasses.MISSING}
    assert set(tooling.load_tool("readme_keys").MACHINE_DEFAULTS) == computed


def test_no_code_block_shows_a_prompt_a_reader_would_copy() -> None:
    assert [body for _, body in FENCE.findall(TEXT) if PROMPT.search(body)] == []


def test_the_prompt_check_knows_powershell_variables() -> None:
    assert PROMPT.search("$ hhe export")
    assert PROMPT.search("PS C:\\> hhe export")
    assert not PROMPT.search("$env:HHE_URL = 'x'")
    assert not PROMPT.search("$HOME\\ha-history-exports")


def test_the_readme_documents_commands_at_all() -> None:
    """A collector that silently finds nothing would pass every check below."""
    assert len(COMMANDS) >= 20


@pytest.mark.parametrize("command", COMMANDS)
def test_a_documented_command_is_one_hhe_accepts(command: str) -> None:
    tokens = strip_program(shlex.split(command))
    if len(tokens) == 1 and not tokens[0].startswith("-"):
        assert descend(tokens)[0], command  # text names a command rather than running it
        return
    if tokens[-1] in {"--help", "--version"}:
        with pytest.raises(SystemExit) as ended:
            build_parser().parse_args(tokens)
        assert ended.value.code == 0, command
        return
    key = getattr(build_parser().parse_args(tokens), "key", None)
    if key is not None and not key.startswith(PLACEHOLDER):
        assert key in {*BY_PATH, TOKEN_KEY}, command


def test_every_link_within_the_page_has_its_heading() -> None:
    assert set(ANCHOR.findall(PROSE)) - anchors() == set()


def test_the_anchors_follow_github() -> None:
    assert slug("Automating daily exports") == "automating-daily-exports"
    assert slug("`hhe` is not recognized") == "hhe-is-not-recognized"
    assert slug("What you need?") == "what-you-need"


def test_images_stand_above_the_first_section_only() -> None:
    head, _, sections = PROSE.partition("\n## ")
    assert IMAGE.search(head)
    assert not IMAGE.search(sections)
