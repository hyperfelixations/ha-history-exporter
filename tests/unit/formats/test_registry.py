from __future__ import annotations

from ha_history_exporter.domain.formats import FORMAT_ORDER, Format
from ha_history_exporter.formats import FORMATS, missing_requirements


def test_every_format_has_exactly_one_spec() -> None:
    assert list(FORMATS) == list(FORMAT_ORDER)
    for fmt, spec in FORMATS.items():
        assert spec.format is fmt


def test_only_parquet_needs_an_optional_package() -> None:
    assert {fmt: spec.requirement for fmt, spec in FORMATS.items()} == {
        Format.JSONL: None,
        Format.CSV: None,
        Format.PARQUET: "pyarrow",
    }


def test_missing_requirements_name_the_package_of_a_selected_format(without_pyarrow: None) -> None:
    assert missing_requirements({Format.JSONL, Format.PARQUET}) == ["pyarrow"]
    assert missing_requirements({Format.JSONL, Format.CSV}) == []
    assert missing_requirements(set()) == []
