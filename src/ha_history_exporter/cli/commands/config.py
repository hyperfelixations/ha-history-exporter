"""``config``: read the effective configuration, change the user configuration file.

Reads report the value a run would use. Writes change only the user
configuration file - never a file named by ``--config`` - and resolve the new
document before it replaces the old one. The token is never echoed.
"""

from __future__ import annotations

import argparse
import os
import shlex

from ...archive.layout import ArchiveLayout
from ...errors import ConfigError, Remedy, UsageError, env_assignment, stdin_from_file
from ...settings import ResolvedSettings, paths, resolve, secrets, user_file
from ...settings.registry import BY_PATH, KEYS, TOKEN_KEY, Key, suggest
from ..logging import CommandLogging
from ..output import say
from ..services import Services

_SECRET_SHOWN = {True: "<set>", False: "<not set>"}


def run(args: argparse.Namespace, services: Services, logs: CommandLogging) -> int:
    actions = {
        "get": _get,
        "set": _set,
        "unset": _unset,
        "list": _list,
        "path": _path,
        "edit": _edit,
    }
    return actions[args.action](args, services)


def _resolved(args: argparse.Namespace, services: Services) -> ResolvedSettings:
    return resolve(environ=services.environ, cwd=services.cwd, explicit_config=args.config)


def _key(key_path: str) -> Key | None:
    """The key named *key_path*; None for the token."""
    if key_path == TOKEN_KEY:
        return None
    key = BY_PATH.get(key_path)
    if key is None:
        details = f"'{key_path}' is not a configuration key."
        if (close := suggest(key_path)) is not None:
            details += f" The closest key is '{close}'."
        raise ConfigError(
            f"Unknown configuration key '{key_path}'.",
            details=details,
            remedies=(Remedy("List every key:", "hhe config list"),),
        )
    return key


def _shown(settings: ResolvedSettings, key: Key | None) -> str:
    if key is None:
        return _SECRET_SHOWN[settings.token is not None]
    value = getattr(getattr(settings.config, key.section), key.name)
    return str(key.setting.kind.display(value))


def _get(args: argparse.Namespace, services: Services) -> int:
    key = _key(args.key)
    say(_shown(_resolved(args, services), key))
    return 0


def _list(args: argparse.Namespace, services: Services) -> int:
    settings = _resolved(args, services)
    rows = [(key.path, _shown(settings, key)) for key in KEYS]
    rows.append((TOKEN_KEY, _shown(settings, None)))
    width = max(len(path) for path, _ in rows)
    for path, shown in rows:
        line = f"{path:<{width}}  {shown}"
        if args.origin:
            line += f"  [{settings.origin(path)}]"
        say(line)
    return 0


def _path(args: argparse.Namespace, services: Services) -> int:
    settings = _resolved(args, services)
    layout = ArchiveLayout(settings.config.export.output_dir)
    entries = [
        ("configuration file", paths.user_config_file(services.environ)),
        ("credentials file", settings.credentials_file),
        ("output directory", layout.root),
        ("archive record", layout.descriptor_file),
        ("log directory", layout.logs_dir),
        ("temporary directory", settings.config.storage.temp_dir),
    ]
    if settings.config_file is not None:
        entries.append(("in use", settings.config_file))
    width = max(len(label) for label, _ in entries)
    for label, path in entries:
        marker = "" if path.exists() else "  (does not exist yet)"
        say(f"{label:<{width}}  {path}{marker}")
    return 0


def _refuse_named_file(args: argparse.Namespace) -> None:
    if args.config:
        raise UsageError(
            "config set, unset and edit change the user configuration only.",
            details="A file named with --config belongs to whoever named it; HHE never writes it.",
            remedies=(
                Remedy("Edit that file directly, or leave out --config:", "hhe config edit"),
            ),
        )


def _set(args: argparse.Namespace, services: Services) -> int:
    _refuse_named_file(args)
    key = _key(args.key)
    if key is None:
        return _set_token(args, services)
    if args.stdin:
        raise UsageError("--stdin is for the access token only.")
    if args.value is None:
        raise UsageError(
            f"{key.path} needs a value.",
            details=f"{key.doc} Expected: {key.setting.kind.type_name}.",
            remedies=(Remedy("Give the value:", f"hhe config set {key.path} <value>"),),
        )
    path = paths.user_config_file(services.environ)
    user_file.set_value(path, key.path, args.value, environ=services.environ, cwd=services.cwd)
    say(f"{key.path} = {_shown(_resolved(args, services), key)}  ({path})")
    return 0


def _set_token(args: argparse.Namespace, services: Services) -> int:
    command = "hhe config set homeassistant.token"
    if args.value is not None:
        raise UsageError(
            "The access token is not given on the command line.",
            details=(
                "Command lines stay in the shell history and are visible to other "
                "processes. The value was neither shown nor stored."
            ),
            remedies=(
                Remedy("Enter it at the hidden prompt:", command),
                Remedy(
                    "Or pipe it in from a file:",
                    stdin_from_file("<token-file>", f"{command} --stdin"),
                ),
            ),
        )
    if args.stdin:
        token = services.stdin.read().strip()
    elif services.stdin.isatty():
        token = services.ask_secret("Long-lived access token (input hidden): ").strip()
    else:
        raise UsageError(
            "A hidden prompt needs an interactive terminal.",
            remedies=(
                Remedy(
                    "Pipe the token in explicitly:",
                    stdin_from_file("<token-file>", f"{command} --stdin"),
                ),
            ),
        )
    if not token:
        raise UsageError("No token was entered; nothing was stored.")
    path = paths.user_credentials_file(services.environ)
    secrets.write_token(path, token)
    say(f"Stored the access token in {path}")
    return 0


def _unset(args: argparse.Namespace, services: Services) -> int:
    _refuse_named_file(args)
    key = _key(args.key)
    if key is None:
        removed = secrets.clear_token(paths.user_credentials_file(services.environ))
        say("Removed the stored access token." if removed else "No access token was stored.")
        return 0
    path = paths.user_config_file(services.environ)
    if not user_file.unset_value(path, key.path, environ=services.environ):
        say(f"{key.path} is not set in {path}.")
    settings = _resolved(args, services)
    say(f"{key.path} = {_shown(settings, key)}  [{settings.origin(key.path)}]")
    return 0


def editor_command(editor: str, *, windows: bool = os.name == "nt") -> list[str]:
    """The program and arguments of ``VISUAL``/``EDITOR``, quoted paths included."""
    if not windows:
        return shlex.split(editor)
    return [
        token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token
        for token in shlex.split(editor, posix=False)
    ]


def _edit(args: argparse.Namespace, services: Services) -> int:
    _refuse_named_file(args)
    path = paths.user_config_file(services.environ)
    if not path.exists():
        user_file.write(path, {}, environ=services.environ)
    editor = services.environ.get("VISUAL") or services.environ.get("EDITOR")
    if not editor:
        if services.open_with_default_app(path):
            say(f"Opened {path}. Check it afterwards with: hhe config list")
            return 0
        raise UsageError(
            "No editor is configured.",
            details="Set VISUAL or EDITOR to the editor to use.",
            remedies=(Remedy("For example:", env_assignment("EDITOR", "nano")),),
        )
    command = [*editor_command(editor), str(path)]
    status = services.run_editor(command)
    if status != 0:
        raise UsageError(
            f"The editor ended with exit code {status}.",
            details=f"{path} is kept as it is; it was not checked.",
        )
    user_file.check(path, environ=services.environ)
    say(f"{path} is valid.")
    return 0
