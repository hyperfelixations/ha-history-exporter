from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter.domain.formats import Format
from ha_history_exporter.settings.validators import (
    Bool,
    Formats,
    HttpUrl,
    Integer,
    InvalidValue,
    Names,
    OptionalValue,
    ParseContext,
    PathValue,
    Seconds,
    SecondsList,
    Zone,
    expand_path,
)


def context(tmp_path: Path, **environ: str) -> ParseContext:
    return ParseContext(base_dir=tmp_path / "base", environ=environ)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://homeassistant.local:8123", "http://homeassistant.local:8123"),
        ("https://ha.example.invalid/", "https://ha.example.invalid"),
        ("  http://192.0.2.10:8123  ", "http://192.0.2.10:8123"),
        ("http://[2001:db8::1]:8123", "http://[2001:db8::1]:8123"),
    ],
)
def test_base_urls_are_normalised(tmp_path: Path, raw: str, expected: str) -> None:
    assert HttpUrl().from_text(raw, context(tmp_path)) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://ha.invalid",
        "homeassistant.local:8123",
        "http://" + "user:secret-value@" + "ha.invalid",
        "http://ha.invalid/api",
        "http://ha.invalid/?" + "token=secret-value",
        "http://ha.invalid/#secret-value",
        "http://ha.invalid:99999",
        "http://",
    ],
)
def test_invalid_urls_are_rejected_without_being_quoted(tmp_path: Path, raw: str) -> None:
    with pytest.raises(InvalidValue) as caught:
        HttpUrl().from_text(raw, context(tmp_path))
    assert "secret-value" not in f"{caught.value.problem} {caught.value.details}"
    assert not HttpUrl().echo


def test_a_url_in_a_document_must_be_a_string(tmp_path: Path) -> None:
    with pytest.raises(InvalidValue, match="must be a string, not an integer"):
        HttpUrl().from_toml(8123, context(tmp_path))


def test_time_zones_are_iana_names(tmp_path: Path) -> None:
    assert Zone().from_toml("Europe/Berlin", context(tmp_path)) == ZoneInfo("Europe/Berlin")
    assert Zone().to_toml(ZoneInfo("UTC")) == "UTC"
    for raw in ("Mars/Olympus", "", "../etc/passwd"):
        with pytest.raises(InvalidValue, match="IANA time zone"):
            Zone().from_text(raw, context(tmp_path))


def test_relative_paths_resolve_against_the_base_directory(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    assert expand_path("exports/../archive", ctx) == tmp_path / "base" / "archive"
    assert expand_path(str(tmp_path / "abs"), ctx) == tmp_path / "abs"


def test_the_home_directory_is_expanded(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    assert expand_path("~", ctx) == Path.home()
    assert expand_path("~/exports", ctx) == Path.home() / "exports"
    with pytest.raises(InvalidValue, match="own home directory"):
        expand_path("~someone/exports", ctx)


def test_environment_variables_are_expanded_from_the_given_environment(tmp_path: Path) -> None:
    ctx = context(tmp_path, ARCHIVE=str(tmp_path / "archive"))
    assert expand_path("$ARCHIVE/ha", ctx) == tmp_path / "archive" / "ha"
    assert expand_path("${ARCHIVE}/ha", ctx) == tmp_path / "archive" / "ha"
    with pytest.raises(InvalidValue, match="undefined environment variable MISSING"):
        expand_path("$MISSING/ha", ctx)


@pytest.mark.skipif(os.name != "nt", reason="%NAME% is Windows notation")
def test_windows_variables_are_expanded(tmp_path: Path) -> None:
    ctx = context(tmp_path, ARCHIVE=str(tmp_path / "archive"))
    assert expand_path("%ARCHIVE%\\ha", ctx) == tmp_path / "archive" / "ha"
    with pytest.raises(InvalidValue, match="MISSING"):
        expand_path("%MISSING%\\ha", ctx)


def test_an_empty_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidValue, match="must not be empty"):
        PathValue().from_text("  ", context(tmp_path))


@pytest.mark.parametrize(
    ("raw", "expected"), [("true", True), ("ON", True), ("0", False), ("no", False)]
)
def test_booleans_as_text(tmp_path: Path, raw: str, expected: bool) -> None:
    assert Bool().from_text(raw, context(tmp_path)) is expected


@pytest.mark.parametrize("value", ["true", 1, None])
def test_booleans_in_a_document_must_be_booleans(tmp_path: Path, value: Any) -> None:
    with pytest.raises(InvalidValue, match="must be true or false"):
        Bool().from_toml(value, context(tmp_path))


def test_integers_respect_their_minimum(tmp_path: Path) -> None:
    kind = Integer(1)
    assert kind.from_text(" 15 ", context(tmp_path)) == 15
    for value in (0, True, 1.5, "15"):
        with pytest.raises(InvalidValue):
            kind.from_toml(value, context(tmp_path))
    with pytest.raises(InvalidValue, match="at least 1"):
        kind.from_text("zero", context(tmp_path))


def test_seconds_are_finite_and_not_negative(tmp_path: Path) -> None:
    assert Seconds().from_toml(2, context(tmp_path)) == 2.0
    assert Seconds().to_toml(2.0) == 2
    assert Seconds().to_toml(0.7) == 0.7
    for raw in ("-0.1", "inf", "nan", "soon"):
        with pytest.raises(InvalidValue):
            Seconds().from_text(raw, context(tmp_path))


def test_lists_as_text_are_comma_separated(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    assert SecondsList().from_text("2, 5,15", ctx) == (2.0, 5.0, 15.0)
    assert SecondsList().from_text("", ctx) == ()
    assert Names().from_text(" update , button,", ctx) == ("update", "button")
    with pytest.raises(InvalidValue):
        Names().from_toml(["update", ""], ctx)
    with pytest.raises(InvalidValue):
        SecondsList().from_toml("2,5", ctx)


def test_formats_in_a_document_and_as_text(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    assert Formats().from_toml(["parquet", "jsonl"], ctx) == {Format.JSONL, Format.PARQUET}
    assert Formats().from_toml([], ctx) == frozenset()
    assert Formats().from_text("none", ctx) == frozenset()
    assert Formats().to_toml(frozenset({Format.PARQUET, Format.JSONL})) == ["jsonl", "parquet"]
    assert Formats().display(frozenset()) == "none"
    with pytest.raises(InvalidValue, match="does not know the output format 'xml'"):
        Formats().from_text("jsonl,xml", ctx)
    for spelling in ("none", "NONE", " None "):
        with pytest.raises(InvalidValue, match="must not contain none"):
            Formats().from_toml([spelling], ctx)
    with pytest.raises(InvalidValue, match="list of output formats"):
        Formats().from_toml("jsonl", ctx)


def test_an_optional_value_is_absent_as_empty_text(tmp_path: Path) -> None:
    kind = OptionalValue(Integer(1))
    assert kind.from_text("", context(tmp_path)) is None
    assert kind.from_text("10", context(tmp_path)) == 10
    assert kind.display(None) == "(not set)"
    with pytest.raises(ValueError, match="no TOML form"):
        kind.to_toml(None)
