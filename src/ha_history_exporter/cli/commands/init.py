"""``init``: ask for the few values HHE needs, check them against Home Assistant, store them.

Without ``--force`` an existing configuration is updated; with it, replaced.
Without a terminal (``--non-interactive``) nothing is asked: the URL comes
from ``--url`` or ``HHE_URL``, the token from ``HHE_TOKEN``, standard input
or the credentials file. Configuration and token are written together or not
at all. The time zone of Home Assistant is proposed and stored only when
another one is chosen.
"""

from __future__ import annotations

import argparse
import contextlib
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ...app.setup import write_setup
from ...errors import HHEError, Remedy, UsageError
from ...formats import missing_requirements
from ...settings import ResolvedSettings, paths, resolve, secrets, user_file
from ...settings.registry import BY_PATH, TOKEN_ENV
from ...settings.sources import invalid
from ...settings.validators import InvalidValue, ParseContext, TomlValue
from ..logging import CommandLogging
from ..output import say
from ..services import Services

DEFAULT_URL = "http://homeassistant.local:8123"
#: The Recorder retention Home Assistant uses unless configured otherwise.
HA_DEFAULT_KEEP_DAYS = "10"


class _Setup:
    def __init__(self, args: argparse.Namespace, services: Services) -> None:
        self.args = args
        self.services = services
        self.interactive = not args.non_interactive

    def ask(self, label: str, default: str) -> str:
        if not self.interactive:
            return default
        answer = self.services.ask(f"{label} [{default}]: ").strip()
        return answer or default

    def confirm(self, question: str, default: bool) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        answer = self.services.ask(f"{question} {suffix}: ").strip().lower()
        return default if not answer else answer in {"y", "yes"}

    def parse(self, key_path: str, raw: str) -> Any:
        key = BY_PATH[key_path]
        ctx = ParseContext(base_dir=self.services.cwd, environ=self.services.environ)
        try:
            return key.setting.kind.from_text(raw, ctx)
        except InvalidValue as error:
            raise invalid(key, "the setup", error, raw) from None

    def stored(self, key_path: str, raw: str) -> TomlValue:
        return BY_PATH[key_path].setting.kind.to_toml(self.parse(key_path, raw))


def run(args: argparse.Namespace, services: Services, logs: CommandLogging) -> int:
    setup = _Setup(args, services)
    environ = services.environ
    config_file = paths.user_config_file(environ)
    credentials_file = paths.user_credentials_file(environ)

    values: dict[str, TomlValue] = {}
    if config_file.exists() and not args.force:
        if not setup.interactive:
            raise UsageError(
                "A configuration already exists.",
                details=f"{config_file} would be changed.",
                remedies=(Remedy("Replace it deliberately:", "hhe init --force"),),
            )
        say(f"A configuration already exists at {config_file}.")
        if not setup.confirm("Update it?", default=False):
            say("Nothing was changed.")
            return 0
        values = user_file.stored_values(config_file)
    current = resolve(environ=environ, cwd=services.cwd)

    url = _url(setup, current)
    token = _token(setup, credentials_file)
    ha_zone = _verify(setup, url, token)
    values["homeassistant.url"] = url

    output_dir = args.output_dir or setup.ask(
        "Output directory", str(current.config.export.output_dir)
    )
    values["export.output_dir"] = setup.stored("export.output_dir", output_dir)

    formats_text = args.format or setup.ask(
        "Output formats (jsonl, csv, parquet, or none)",
        BY_PATH["export.formats"].setting.kind.display(current.config.export.formats),
    )
    formats = setup.parse("export.formats", formats_text)
    values["export.formats"] = BY_PATH["export.formats"].setting.kind.to_toml(formats)
    if missing_requirements(formats):
        say("  ! Parquet needs pyarrow, which is not installed:")
        say("      pipx inject ha-history-exporter pyarrow")

    _timezone(setup, values, current, ha_zone)
    _retention(setup, values, current)

    write_setup(config_file, values, credentials_file, token, environ=environ)
    say()
    say(f"Wrote {config_file}")
    say(f"Wrote {credentials_file} (the access token, readable by your account only)")
    say()
    say("Next:  hhe export")
    return 0


def _url(setup: _Setup, current: ResolvedSettings) -> str:
    configured = current.config.homeassistant.url
    raw = setup.args.url
    if raw is None:
        if not setup.interactive:
            if configured is None:
                raise _missing("--url or HHE_URL")
            raw = configured
        else:
            raw = setup.ask("Home Assistant URL", configured or DEFAULT_URL)
    return str(setup.parse("homeassistant.url", raw))


def _token(setup: _Setup, credentials_file: Path) -> str:
    services = setup.services
    stored = secrets.read_token(credentials_file)
    if not setup.interactive:
        if from_env := services.environ.get(TOKEN_ENV, "").strip():
            return from_env
        if not services.stdin.isatty() and (piped := services.stdin.read().strip()):
            return piped
        if stored:
            return stored
        raise _missing("a token in HHE_TOKEN or on standard input")
    prompt = "Long-lived access token (input hidden)"
    if stored:
        prompt += " [keep the stored one]"
    entered = services.ask_secret(f"{prompt}: ").strip()
    if entered:
        return entered
    if stored:
        return stored
    raise UsageError(
        "No access token was entered.",
        details="Create one in Home Assistant under Profile, Security, Long-lived access tokens.",
        remedies=(Remedy("Run the setup again:", "hhe init"),),
    )


def _verify(setup: _Setup, url: str, token: str) -> ZoneInfo | None:
    """Home Assistant's time zone, after proving that URL and token work."""
    services = setup.services
    probe_environ = {**services.environ, "HHE_URL": url, TOKEN_ENV: token}
    say(f"  -> Contacting {url} ...")
    try:
        probe = resolve(environ=probe_environ, cwd=services.cwd)
        with contextlib.closing(services.connect(probe)) as source:
            source.check_api()
            instance = source.instance_config()
            count = len(source.states())
    except HHEError as exc:
        say(f"  ! {exc.summary}")
        if setup.interactive and setup.confirm("Save the configuration anyway?", default=False):
            return None
        raise
    say(f"     OK: Home Assistant {instance.version}, {count} entities, {instance.time_zone.key}")
    return instance.time_zone


def _timezone(
    setup: _Setup,
    values: dict[str, TomlValue],
    current: ResolvedSettings,
    ha_zone: ZoneInfo | None,
) -> None:
    if setup.args.timezone is not None:
        values["export.timezone"] = setup.stored("export.timezone", setup.args.timezone)
        return
    if not setup.interactive:
        return
    configured = current.config.export.timezone
    proposed = configured or ha_zone
    label = "Time zone of the exported days"
    if proposed is None:
        answer = setup.ask(label + " (empty: Home Assistant's)", "").strip()
    else:
        origin = "configured" if configured is not None else "Home Assistant's"
        answer = setup.ask(f"{label} ({origin})", proposed.key)
    if not answer or (ha_zone is not None and answer == ha_zone.key):
        values.pop("export.timezone", None)
    else:
        values["export.timezone"] = setup.stored("export.timezone", answer)


def _retention(setup: _Setup, values: dict[str, TomlValue], current: ResolvedSettings) -> None:
    raw = setup.args.purge_keep_days
    if raw is None:
        if not setup.interactive:
            return
        configured = current.config.recorder.purge_keep_days
        raw = setup.ask(
            "Recorder retention of Home Assistant in days (purge_keep_days)",
            str(configured) if configured is not None else HA_DEFAULT_KEEP_DAYS,
        )
    values["recorder.purge_keep_days"] = setup.stored("recorder.purge_keep_days", raw)


def _missing(what: str) -> UsageError:
    return UsageError(
        f"A setup without questions needs {what}.",
        details="There is no terminal to ask on, so every value comes from options or variables.",
        remedies=(
            Remedy(
                "For example, with the token in HHE_TOKEN:",
                "hhe init --non-interactive --url http://homeassistant.local:8123",
            ),
        ),
    )
