from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ha_history_exporter.errors import ConfigError
from ha_history_exporter.settings.secrets import (
    clear_token,
    is_owner_only,
    read_token,
    write_token,
)

TOKEN = "synthetic-test-token"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "secrets" / "credentials.toml"


def test_a_token_round_trips_and_can_be_cleared(path: Path) -> None:
    assert read_token(path) is None
    write_token(path, TOKEN)
    assert read_token(path) == TOKEN
    write_token(path, "replaced-synthetic-token")
    assert read_token(path) == "replaced-synthetic-token"
    assert clear_token(path)
    assert not clear_token(path)
    assert read_token(path) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_the_file_is_readable_by_the_owner_only(path: Path) -> None:
    write_token(path, TOKEN)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert is_owner_only(path)
    path.chmod(0o644)
    assert not is_owner_only(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows relies on the profile ACL")
def test_the_mode_check_does_not_apply_on_windows(path: Path) -> None:
    write_token(path, TOKEN)
    assert is_owner_only(path) is None


@pytest.mark.parametrize(
    ("document", "token"),
    [("", None), ("[homeassistant]\n", None), ('[homeassistant]\ntoken = "  "\n', None)],
)
def test_an_absent_or_blank_token_counts_as_unset(
    path: Path, document: str, token: str | None
) -> None:
    path.parent.mkdir()
    path.write_text(document, encoding="utf-8")
    assert read_token(path) == token


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (f'[homeassistant]\ntoken = "{TOKEN}\n', r"not valid TOML \(line 2, column"),
        (f'homeassistant = ["{TOKEN}"]\n', "must contain only"),
        (f'[homeassistant]\ntoken = "{TOKEN}"\nurl = "x"\n', "must contain only"),
        (f'token = "{TOKEN}"\n', "must contain only"),
        ("[homeassistant]\ntoken = 42\n", "as a string"),
    ],
)
def test_an_unusable_file_is_reported_without_its_content(
    path: Path, document: str, message: str
) -> None:
    path.parent.mkdir()
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ConfigError, match=message) as caught:
        read_token(path)
    error = caught.value
    assert TOKEN not in f"{error.summary} {error.details} {error.context}"
