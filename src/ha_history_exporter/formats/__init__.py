"""The format register: one :class:`FormatSpec` per output format."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..domain.formats import FORMAT_ORDER, Format
from .base import FormatSpec, RecordWriter
from .csv import CsvWriter, read_csv
from .jsonl import JsonlWriter, read_jsonl
from .parquet import ParquetWriter, read_parquet

FORMATS: Mapping[Format, FormatSpec] = {
    Format.JSONL: FormatSpec(Format.JSONL, JsonlWriter, read_jsonl, sequential=True),
    Format.CSV: FormatSpec(Format.CSV, CsvWriter, read_csv, sequential=True),
    Format.PARQUET: FormatSpec(
        Format.PARQUET, ParquetWriter, read_parquet, sequential=False, requirement="pyarrow"
    ),
}


def missing_requirements(formats: Iterable[Format]) -> list[str]:
    """Import names of optional packages that the selected *formats* lack."""
    chosen = set(formats)
    return [
        spec.requirement
        for fmt in FORMAT_ORDER
        if fmt in chosen
        and (spec := FORMATS[fmt]).requirement is not None
        and not spec.requirement_met()
    ]


__all__ = ["FORMATS", "FormatSpec", "RecordWriter", "missing_requirements"]
