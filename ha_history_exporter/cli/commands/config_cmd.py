"""The ``config`` command: inspect and change the user configuration.

Reads report the *effective* value, so a user sees what a run would actually
use. Writes only ever touch the user configuration file, never a project file
and never the file named by ``--config`` — an explicitly chosen file belongs
to the caller.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

from ...errors import ConfigError, Remedy, UsageError
from ...settings import ResolvedSettings, document, paths, resolve, schema, secrets
from ...settings.model import FORMAT_ORDER
from ...settings.schema import Key, KeyStatus

TOKEN_KEY = "homeassistant.token"  # noqa: S105 - key path, not a secret


def run(args: argparse.Namespace) -> int:
    actions = {
        "get": _get,
        "set": _set,
        "unset": _unset,
        "list": _list,
        "path": _path,
        "edit": _edit,
    }
    return actions[args.config_action](args)


# ── read ──────────────────────────────────────────────────────────────────────

def _effective(args: argparse.Namespace) -> ResolvedSettings:
    return resolve(
        explicit_config=getattr(args, "config", None), require_credentials=False
    )


def _get(args: argparse.Namespace) -> int:
    key = _known_key(args.key)
    settings = _effective(args)
    print(_display_value(settings, key))
    return 0


def _list(args: argparse.Namespace) -> int:
    settings = _effective(args)
    width = max(len(key.path) for key in schema.KEYS)
    for key in schema.KEYS:
        value = _display_value(settings, key)
        line = f"{key.path:<{width}}  {value}"
        if args.origin:
            line += f"  [{settings.origin(key.path)}]"
        print(line)
    return 0


def _path(args: argparse.Namespace) -> int:
    entries = [
        ("configuration file", paths.user_config_file()),
        ("credentials file", paths.user_credentials_file()),
    ]
    settings = _effective(args)
    entries.extend(
        [
            ("output directory", Path(settings.config.export.output_dir)),
            ("log directory", settings.config.layout.logs_dir),
            ("temporary directory", settings.config.resolved_temp_dir),
        ]
    )
    if settings.config_file is not None:
        entries.append(("in use", settings.config_file))

    width = max(len(label) for label, _ in entries)
    for label, path in entries:
        marker = "" if path.exists() else "  (does not exist yet)"
        print(f"{label:<{width}}  {path}{marker}")
    return 0


# ── write ─────────────────────────────────────────────────────────────────────

def _set(args: argparse.Namespace) -> int:
    key = _known_key(args.key)
    _refuse_explicit_config(args)

    if key.status is KeyStatus.SECRET:
        token = args.value if args.value is not None else _prompt_secret()
        if not token.strip():
            raise UsageError(
                "No token was entered; nothing was stored.",
                remedies=(
                    Remedy("Try again:", "hhe config set homeassistant.token"),
                ),
            )
        path = secrets.write_token(token.strip())
        print(f"Stored the access token in {path}")
        return 0

    if args.value is None:
        raise UsageError(
            f"A value is required for {key.path}.",
            details=f"{key.doc} Expected type: {key.type.value}.",
            remedies=(
                Remedy("Provide the value:", f"hhe config set {key.path} <value>"),
            ),
        )

    values = document.read_user_values()
    values[key.path] = document.parse_value(key.path, args.value)
    path = document.write_user_values(values)
    print(f"{key.path} = {render_value(values[key.path])}  ({path})")
    return 0


def _unset(args: argparse.Namespace) -> int:
    key = _known_key(args.key)
    _refuse_explicit_config(args)

    if key.status is KeyStatus.SECRET:
        removed = secrets.clear_token()
        print("Removed the stored access token." if removed else "No token was stored.")
        return 0

    values = document.read_user_values()
    if key.path not in values:
        print(f"{key.path} is not set in the user configuration.")
    else:
        del values[key.path]
        document.write_user_values(values)

    settings = _effective(args)
    print(
        f"{key.path} = {_display_value(settings, key)}  "
        f"[{settings.origin(key.path)}]"
    )
    return 0


def _edit(args: argparse.Namespace) -> int:
    _refuse_explicit_config(args)
    path = paths.user_config_file()
    if not path.is_file():
        document.write_user_values(document.read_user_values())

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    start_file = getattr(os, "startfile", None)  # Windows only
    if editor:
        subprocess.call([*editor.split(), str(path)])  # noqa: S603 - the user's own editor
    elif start_file is not None:
        start_file(str(path))
        print(f"Opened {path} in the default editor.")
        return 0
    else:
        raise UsageError(
            "No editor configured.",
            details="Set VISUAL or EDITOR to the editor you want to use.",
            remedies=(Remedy("For example:", 'export EDITOR="nano"'),),
        )

    document.validate_document(path)
    print(f"{path} is valid.")
    return 0


# ── helpers ───────────────────────────────────────────────────────────────────

def _known_key(key_path: str) -> Key:
    key = schema.BY_PATH.get(key_path)
    if key is None:
        suggestion = schema.suggest(key_path)
        details = f"'{key_path}' is not a configuration key HHE knows."
        if suggestion:
            details += f" The closest known key is '{suggestion}'."
        raise ConfigError(
            f"Unknown configuration key '{key_path}'.",
            details=details,
            remedies=(Remedy("List every supported key:", "hhe config list"),),
        )
    return key


def render_value(value: object) -> str:
    """Render one configured value the way a user would type it.

    Internal shapes - a format set, a tuple of numbers - must never reach the
    terminal as their Python repr.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, frozenset):
        return ",".join(fmt.value for fmt in FORMAT_ORDER if fmt in value) or "none"
    if isinstance(value, (tuple, list)):
        return ",".join(str(item) for item in value)
    return str(value)


def _display_value(settings: ResolvedSettings, key: Key) -> str:
    """Never echo a secret; report only whether one is stored."""
    if key.status is KeyStatus.SECRET:
        return str(settings.values.get(key.path, "<not set>"))
    return render_value(settings.values.get(key.path, ""))


def _refuse_explicit_config(args: argparse.Namespace) -> None:
    if getattr(args, "config", None):
        raise UsageError(
            "config set, unset, and edit always write the user configuration.",
            details=(
                "A file passed with --config belongs to the caller and is "
                "never rewritten by HHE."
            ),
            remedies=(
                Remedy("Edit that file directly, or drop --config:", "hhe config edit"),
            ),
        )


def _prompt_secret() -> str:
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data:
            return data
        raise UsageError(
            "No token was provided.",
            details="Pass the value as an argument or pipe it on standard input.",
            remedies=(
                Remedy(
                    "For example:",
                    "hhe config set homeassistant.token <token>",
                ),
            ),
        )
    return getpass.getpass("Long-lived access token (input hidden): ")
