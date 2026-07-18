from __future__ import annotations

import zipfile

import pytest

import DICOM_download_script as cli
from dcmget.accession_import import ColumnSelectionError


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _write_xlsx(path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">'
            '<sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{PACKAGE_REL_NS}">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{MAIN_NS}"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>患者</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>检查号</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>张三</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>CT001</t></is></c></row>'
            "</sheetData></worksheet>",
        )


def test_cli_multicolumn_csv_requires_explicit_column(tmp_path):
    source = tmp_path / "检查号.csv"
    source.write_text("患者,检查号\n张三,CT001\n", encoding="utf-8-sig")

    with pytest.raises(ColumnSelectionError, match="--accession-column"):
        cli.load_cli_accessions(source, None)

    assert cli.load_cli_accessions(source, "检查号").values == ("CT001",)
    assert cli.load_cli_accessions(source, "1").values == ("CT001",)


def test_cli_xlsx_supports_explicit_column_name(tmp_path):
    source = tmp_path / "检查号.xlsx"
    _write_xlsx(source)

    args = cli.build_parser().parse_args(
        ["--accessions", str(source), "--accession-column", "检查号"]
    )
    result = cli.load_cli_accessions(args.accessions, args.accession_column)

    assert result.values == ("CT001",)


def test_cli_txt_rejects_accession_column(tmp_path):
    source = tmp_path / "检查号.txt"
    source.write_bytes("CT001\nCT002".encode("gb18030"))

    assert cli.load_cli_accessions(source, None).values == ("CT001", "CT002")
    with pytest.raises(ColumnSelectionError, match="TXT 文件不支持选列"):
        cli.load_cli_accessions(source, "0")
