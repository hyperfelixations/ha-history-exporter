from __future__ import annotations

import pytest

from ha_history_exporter.domain.formats import (
    FORMAT_ORDER,
    Format,
    FormatSetError,
    parse_format_set,
    render_format_set,
)


def test_every_format_value_is_its_file_suffix() -> None:
    assert [fmt.value for fmt in FORMAT_ORDER] == ["jsonl", "csv", "parquet"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("jsonl", {Format.JSONL}),
        ("JSONL, Parquet", {Format.JSONL, Format.PARQUET}),
        ("csv,csv", {Format.CSV}),
        (["parquet", " csv "], {Format.CSV, Format.PARQUET}),
        ("none", set()),
        ("", set()),
        ([], set()),
        (["NONE"], set()),
    ],
)
def test_parse_reads_the_set_notation(value: str | list[str], expected: set[Format]) -> None:
    assert parse_format_set(value) == frozenset(expected)


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("arrow", "does not know the output format 'arrow'"),
        ("none,jsonl", "cannot combine 'none'"),
        (["jsonl", 3], "as strings"),
    ],
)
def test_parse_rejects_what_it_cannot_interpret(value: object, problem: str) -> None:
    with pytest.raises(FormatSetError, match=problem) as caught:
        parse_format_set(value)  # type: ignore[arg-type]
    assert caught.value.details


def test_render_uses_canonical_order_and_none_for_the_empty_set() -> None:
    assert render_format_set({Format.PARQUET, Format.JSONL}) == "jsonl,parquet"
    assert render_format_set(frozenset()) == "none"
