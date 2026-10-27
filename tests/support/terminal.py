"""A terminal, a user and a Home Assistant for driving real commands in-process."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ha_history_exporter.cli.main import main
from ha_history_exporter.cli.services import Services
from ha_history_exporter.domain.records import JsonValue
from ha_history_exporter.settings import ResolvedSettings
from ha_history_exporter.settings.paths import ENV_CONFIG_DIR
from ha_history_exporter.source.protocol import HistorySource
from tests.support.clock import FixedClock
from tests.support.fake_source import FakeHistorySource

NOW = datetime(2026, 7, 30, 3, 30, 5, tzinfo=ZoneInfo("Europe/Berlin"))


class _Stdin(io.StringIO):
    def __init__(self, text: str, tty: bool) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@dataclass
class Terminal:
    """Everything a command sees of the world; answers are consumed in order."""

    root: Path
    source: FakeHistorySource = field(default_factory=FakeHistorySource)
    environ: dict[str, str] = field(default_factory=dict)
    answers: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    stdin: str = ""
    tty: bool = False
    editor: Callable[[list[str]], int] = lambda _command: 0
    default_app: bool = False
    now: datetime = NOW
    connected: list[ResolvedSettings] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    stderr: str = ""

    def __post_init__(self) -> None:
        self.environ = {
            ENV_CONFIG_DIR: str(self.config_dir),
            "HHE_STORAGE_TEMP_DIR": str(self.root / "temp"),
            **self.environ,
        }

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def cwd(self) -> Path:
        path = self.root / "cwd"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def connect(self, settings: ResolvedSettings) -> HistorySource:
        settings.connection()
        self.connected.append(settings)
        return self.source

    def _ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def _ask_secret(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.secrets.pop(0)

    def run(self, *argv: str, prog: str = "hhe") -> int:
        stderr = io.StringIO()
        services = Services(
            environ=self.environ,
            cwd=self.cwd,
            stdin=_Stdin(self.stdin, self.tty),
            stderr=stderr,
            connect=self.connect,
            clock=FixedClock(self.now),
            sleep=lambda _seconds: None,
            ask=self._ask,
            ask_secret=self._ask_secret,
            run_editor=self.editor,
            open_with_default_app=lambda _path: self.default_app,
        )
        try:
            return main(list(argv), prog=prog, services=services)
        finally:
            self.stderr = stderr.getvalue()


def states(entity_ids: Iterable[str]) -> list[dict[str, JsonValue]]:
    return [{"entity_id": entity_id, "state": "on", "attributes": {}} for entity_id in entity_ids]
