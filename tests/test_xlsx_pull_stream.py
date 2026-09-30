"""The direct pull-parser loop retains partial rows and validates EOF."""

import io
import zipfile
from xml.etree.ElementTree import ParseError

import pytest

from diceo.sheets import SheetDiagnostics, _iter_part_rows


def test_pull_stream_crosses_feeds_and_checks_eof():
    value = "é" * 20_000
    row = f'<row r="1"><c r="A1" t="inlineStr"><is><t>{value}</t></is></c></row>'
    body = f'<worksheet><sheetData>{row}<row r="2"><c><v>7</v></c></row></sheetData>'
    for closing in ("</worksheet>", ""):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("sheet.xml", body + closing)
        with zipfile.ZipFile(buffer) as archive:
            report = SheetDiagnostics()
            rows = _iter_part_rows(archive, "sheet.xml", "S", [], set(), False, report, None)
            assert next(rows).cells == [value]
            assert next(rows).cells == ["7"]
            if closing:
                assert list(rows) == []
            else:
                with pytest.raises(ParseError):
                    next(rows)
            assert (report.rows, report.cells) == (2, 2)
            limited = SheetDiagnostics()
            assert (
                len(
                    list(
                        _iter_part_rows(archive, "sheet.xml", "S", [], set(), False, limited, 1)
                    )
                )
                == 1
            )
            assert limited.truncated == [("max_rows", 1, -1)]
