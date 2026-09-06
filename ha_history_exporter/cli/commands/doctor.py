"""The ``doctor`` command: check the installation and report how to fix it.

Every check reports ok, warn, or fail. A failure carries the command that
resolves it. ``--offline`` skips everything that would contact Home Assistant.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from ... import __version__, ha_client
from ...errors import HHEError
from ...settings import AppConfig, ResolvedSettings, paths, resolve, secrets

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARKS = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[fail]"}

LOW_DISK_GIB = 5


@dataclass
class Result:
    status: str
    label: str
    detail: str = ""
    remedy: str = ""


def run(args: argparse.Namespace) -> int:
    results: list[Result] = []
    results.append(
        Result(OK, "HHE version", f"{__version__} on Python {_python_version()}")
    )

    settings = None
    try:
        settings = resolve(
            explicit_config=getattr(args, "config", None), require_credentials=False
        )
        files = ", ".join(str(p) for p in settings.config_files)
        results.append(
            Result(
                OK if files else WARN,
                "configuration",
                files or "no configuration file found; using built-in defaults",
                "" if files else "hhe init",
            )
        )
    except HHEError as exc:
        results.append(
            Result(FAIL, "configuration", exc.summary, _first_command(exc))
        )
        _print(results)
        return 1

    cfg = settings.config
    results.extend(_check_timezone(cfg))
    results.extend(_check_credentials(settings))
    results.extend(_check_output_dir(cfg))
    results.extend(_check_temp_dir(cfg))
    results.extend(_check_formats(cfg))
    results.extend(_check_exports(cfg))

    if not args.offline:
        results.extend(_check_home_assistant(cfg))
    else:
        results.append(
            Result(OK, "home assistant", "skipped (--offline)")
        )

    _print(results)
    return 1 if any(item.status == FAIL for item in results) else 0


# ── individual checks ─────────────────────────────────────────────────────────

def _check_timezone(cfg: AppConfig) -> list[Result]:
    name = cfg.home_assistant.timezone
    try:
        ZoneInfo(name)
    except Exception:
        return [
            Result(
                FAIL,
                "timezone",
                f"'{name}' is not a known IANA time zone",
                "hhe config set home_assistant.timezone Europe/Berlin",
            )
        ]
    return [Result(OK, "timezone", name)]


def _check_credentials(settings: ResolvedSettings) -> list[Result]:
    cfg = settings.config
    results: list[Result] = []

    if cfg.ha_url:
        results.append(
            Result(
                OK,
                "home assistant url",
                f"{cfg.ha_url}  [{settings.origin('homeassistant.url')}]",
            )
        )
    else:
        results.append(
            Result(
                FAIL,
                "home assistant url",
                "not configured",
                "hhe config set homeassistant.url http://homeassistant.local:8123",
            )
        )

    if cfg.ha_token:
        results.append(
            Result(
                OK,
                "access token",
                f"stored  [{settings.origin('homeassistant.token')}]",
            )
        )
    else:
        results.append(
            Result(
                FAIL,
                "access token",
                "not configured",
                "hhe config set homeassistant.token",
            )
        )

    credentials = paths.user_credentials_file()
    if credentials.is_file():
        owner_only = secrets.is_owner_only(credentials)
        if owner_only is False:
            results.append(
                Result(
                    WARN,
                    "credentials permissions",
                    f"{credentials} is readable by others",
                    f"chmod 600 {credentials}",
                )
            )
        elif owner_only is True:
            results.append(Result(OK, "credentials permissions", "owner only"))
    return results


def _check_output_dir(cfg: AppConfig) -> list[Result]:
    path = Path(cfg.export.output_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".hhe-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return [
            Result(
                FAIL,
                "output directory",
                f"{path} is not writable ({exc.strerror or exc})",
                "hhe config set export.output_dir <path>",
            )
        ]

    results = [Result(OK, "output directory", str(path))]
    free_gib = shutil.disk_usage(path).free / 1024**3
    results.append(
        Result(
            WARN if free_gib < LOW_DISK_GIB else OK,
            "free disk space",
            f"{free_gib:.1f} GiB",
            "Free up space; one exported day can reach several hundred MB."
            if free_gib < LOW_DISK_GIB
            else "",
        )
    )
    return results


def _check_temp_dir(cfg: AppConfig) -> list[Result]:
    path = cfg.resolved_temp_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".hhe-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return [
            Result(
                FAIL,
                "temporary directory",
                f"{path} is not writable ({exc.strerror or exc})",
                "hhe config set storage.temp_dir <path>",
            )
        ]
    return [Result(OK, "temporary directory", str(path))]


def _check_formats(cfg: AppConfig) -> list[Result]:
    enabled = [
        name
        for name, on in (
            ("jsonl", cfg.formats.jsonl),
            ("csv", cfg.formats.csv),
            ("parquet", cfg.formats.parquet),
        )
        if on
    ]
    if not enabled:
        return [
            Result(
                WARN,
                "output formats",
                "none enabled; runs capture the entity snapshot only",
                "hhe config set formats.jsonl true",
            )
        ]

    results = [Result(OK, "output formats", ", ".join(enabled))]
    if cfg.formats.parquet:
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            results.append(
                Result(
                    FAIL,
                    "pyarrow",
                    "Parquet output is enabled but pyarrow is not installed",
                    "pipx inject ha-history-exporter pyarrow",
                )
            )
        else:
            results.append(Result(OK, "pyarrow", "installed"))
    return results


def _check_exports(cfg: AppConfig) -> list[Result]:
    root = cfg.daily_export_root
    if not root.exists():
        return [Result(OK, "existing exports", "none yet")]

    manifests = sorted(root.glob("*/*/*.manifest.json"))
    if not manifests:
        return [Result(OK, "existing exports", "none yet")]

    days = [path.name.split(".")[0] for path in manifests]
    return [
        Result(
            OK,
            "existing exports",
            f"{len(manifests)} day(s), {days[0]} to {days[-1]}",
        )
    ]


def _check_home_assistant(cfg: AppConfig) -> list[Result]:
    if not (cfg.ha_url and cfg.ha_token):
        return [
            Result(
                WARN,
                "home assistant",
                "not contacted; credentials are incomplete",
                "hhe init",
            )
        ]
    try:
        with ha_client.HomeAssistantClient(
            url=cfg.ha_url,
            token=cfg.ha_token,
            timeout=cfg.requests.request_timeout_seconds,
            max_retries=0,
            backoff_seconds=cfg.requests.backoff_seconds,
        ) as client:
            client.check_api()
            states = client.get_states()
    except HHEError as exc:
        return [
            Result(FAIL, "home assistant", exc.summary, _first_command(exc))
        ]
    return [
        Result(OK, "home assistant", f"reachable, {len(states)} entities"),
    ]


# ── output ────────────────────────────────────────────────────────────────────

def _print(results: list[Result]) -> None:
    width = max(len(item.label) for item in results)
    for item in results:
        line = f"{_MARKS[item.status]} {item.label:<{width}}"
        if item.detail:
            line += f"  {item.detail}"
        print(line)
        if item.remedy:
            print(f"       {' ' * width}  -> {item.remedy}")

    failures = sum(1 for item in results if item.status == FAIL)
    warnings = sum(1 for item in results if item.status == WARN)
    print()
    if failures:
        print(f"{failures} problem(s) need attention, {warnings} warning(s).")
    elif warnings:
        print(f"No problems, {warnings} warning(s).")
    else:
        print("Everything checks out.")


def _first_command(exc: HHEError) -> str:
    for remedy in exc.remedies:
        if remedy.command:
            return remedy.command
    return ""


def _python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])
