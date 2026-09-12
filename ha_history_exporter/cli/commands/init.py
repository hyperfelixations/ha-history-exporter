"""The ``init`` command: guided first-time setup.

Asks for the few values HHE cannot guess, verifies them against the running
Home Assistant instance, and writes the user configuration and the credentials
file. Nothing is written before the questions are answered.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from zoneinfo import ZoneInfo

from ... import ha_client
from ...errors import HHEError, Remedy, UsageError
from ...settings import Format, ResolvedSettings, document, format_list, paths, resolve, secrets
from ...settings.resolver import validate_home_assistant_url
from .export import parse_format_list

DEFAULT_URL = "http://homeassistant.local:8123"


def run(args: argparse.Namespace) -> int:
    interactive = not args.non_interactive

    if paths.user_config_file().is_file() and not args.force:
        if not interactive:
            raise UsageError(
                "A configuration already exists.",
                details=f"{paths.user_config_file()} would be overwritten.",
                remedies=(Remedy("Overwrite it deliberately:", "hhe init --force"),),
            )
        print(f"A configuration already exists at {paths.user_config_file()}.")
        if not _confirm("Update it?", default=False):
            print("Nothing was changed.")
            return 0

    current = resolve(require_credentials=False)
    values = document.read_user_values()

    url = _ask_url(args, current, interactive)
    token = _ask_token(args, interactive)
    _verify(url, token, current, interactive)

    output_dir = _ask(
        args.output_dir,
        "Output directory",
        current.config.export.output_dir,
        interactive,
    )
    formats = _ask_formats(args, current, interactive)
    timezone = _ask_timezone(args, current, interactive)

    values["homeassistant.url"] = url
    values["export.output_dir"] = output_dir
    values["export.timezone"] = timezone
    values["export.formats"] = formats

    config_path = document.write_user_values(values)
    credentials_path = secrets.write_token(token)

    print()
    print(f"Wrote {config_path}")
    print(f"Wrote {credentials_path} (access token, not readable by other users)")
    print()
    print("Next:  hhe export")
    return 0


# ── questions ─────────────────────────────────────────────────────────────────

def _ask_url(
    args: argparse.Namespace, current: ResolvedSettings, interactive: bool
) -> str:
    if args.url:
        return validate_home_assistant_url(args.url)
    if not interactive:
        raise _missing("--url")
    default = current.config.homeassistant.url or DEFAULT_URL
    return validate_home_assistant_url(
        _ask(None, "Home Assistant URL", default, interactive)
    )


def _ask_token(args: argparse.Namespace, interactive: bool) -> str:
    token = secrets.read_token()
    if not interactive:
        if not sys.stdin.isatty():
            piped = sys.stdin.read().strip()
            if piped:
                return piped
        if token:
            return token
        raise _missing("a token on standard input or HA_TOKEN")

    prompt = "Long-lived access token (input hidden)"
    if token:
        prompt += " [keep current]"
    entered = getpass.getpass(f"{prompt}: ").strip()
    if entered:
        return entered
    if token:
        return token
    raise UsageError(
        "No access token was entered.",
        details=(
            "Create one in Home Assistant under Profile, Security, "
            "Long-Lived Access Tokens."
        ),
        remedies=(Remedy("Run the setup again:", "hhe init"),),
    )


def _ask_formats(
    args: argparse.Namespace, current: ResolvedSettings, interactive: bool
) -> frozenset[Format]:
    default = format_list(current.config.export.formats)
    raw = args.format or _ask(None, "Output formats", default, interactive)
    selection = parse_format_list(raw)

    if Format.PARQUET in selection:
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            print("  ! Parquet needs pyarrow, which is not installed.")
            print("    Install it with:  pipx inject ha-history-exporter pyarrow")
    return selection


def _ask_timezone(
    args: argparse.Namespace, current: ResolvedSettings, interactive: bool
) -> str:
    value = args.timezone or _ask(
        None, "Timezone", current.config.export.timezone, interactive
    )
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise UsageError(
            f"'{value}' is not a known IANA time zone.",
            details="Use a name such as Europe/Berlin, UTC, or America/New_York.",
        ) from exc
    return value


def _verify(
    url: str, token: str, current: ResolvedSettings, interactive: bool
) -> None:
    print(f"  -> Contacting {url} ...")
    try:
        with ha_client.HomeAssistantClient(
            url=url,
            token=token,
            timeout=current.config.requests.timeout,
            max_retries=0,
            backoff_seconds=current.config.requests.backoff,
        ) as client:
            client.check_api()
            states = client.get_states()
    except HHEError as exc:
        print(f"  ! {exc.summary}")
        if not interactive or not _confirm("Save the configuration anyway?", False):
            raise
        return
    print(f"     OK ({len(states)} entities)")


# ── prompting ─────────────────────────────────────────────────────────────────

def _ask(
    explicit: str | None, label: str, default: str, interactive: bool
) -> str:
    if explicit:
        return explicit
    if not interactive:
        return default
    answer = input(f"{label} [{default}]: ").strip()
    return answer or default


def _confirm(question: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _missing(what: str) -> UsageError:
    return UsageError(
        f"Non-interactive setup needs {what}.",
        details="There is no terminal to ask on, so every value must be given.",
        remedies=(
            Remedy(
                "For example:",
                "hhe init --non-interactive --url http://homeassistant.local:8123",
            ),
        ),
    )
