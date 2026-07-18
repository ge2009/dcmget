from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from dcmget.accession_import import (
    AccessionImportError,
    ColumnSelectionError,
    ImportLimitError,
    ImportLimits,
    import_accession_file,
)


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _write_xlsx(
    path: Path,
    worksheet_xml: str,
    *,
    shared_strings_xml: str | None = None,
    styles_xml: str | None = None,
    target: str = "worksheets/sheet1.xml",
    workbook_xml: str | None = None,
    relationships_xml: str | None = None,
    extra_members: dict[str, str] | None = None,
) -> Path:
    workbook = workbook_xml or f"""<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">
  <sheets><sheet name="检查数据" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    relationships = relationships_xml or f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PACKAGE_REL_NS}">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="{target}"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
        if shared_strings_xml is not None:
            archive.writestr("xl/sharedStrings.xml", shared_strings_xml)
        if styles_xml is not None:
            archive.writestr("xl/styles.xml", styles_xml)
        for name, content in (extra_members or {}).items():
            archive.writestr(name, content)
    return path


def _worksheet(rows: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="{MAIN_NS}"><sheetData>{rows}</sheetData></worksheet>"""


def _shared_strings(values: list[str]) -> str:
    items = "".join(f"<si><t>{value}</t></si>" for value in values)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="{MAIN_NS}" count="{len(values)}" uniqueCount="{len(values)}">
{items}</sst>"""


def test_txt_utf8_sig_chinese_path_counts_blanks_duplicates_and_invalid(tmp_path):
    source = tmp_path / "中文目录" / "检查号清单.txt"
    source.parent.mkdir()
    source.write_text(
        " A001\n\nA002\nA001\nA?03\n A004 \n",
        encoding="utf-8-sig",
    )

    result = import_accession_file(source)

    assert result.values == ("A001", "A002", "A004")
    assert result.total_rows == 6
    assert result.blank_count == 1
    assert result.duplicate_count == 1
    assert result.invalid_values == ("A?03",)
    assert result.valid_count == 3
    assert result.encoding == "utf-8-sig"
    assert result.available_columns == ()


def test_txt_gb18030_is_detected(tmp_path):
    source = tmp_path / "访问号.txt"
    source.write_bytes("检查2026甲\n检查2026乙".encode("gb18030"))

    result = import_accession_file(source)

    assert result.values == ("检查2026甲", "检查2026乙")
    assert result.encoding == "gb18030"


def test_csv_recognizes_chinese_column_and_keeps_formula_as_plain_text(tmp_path):
    source = tmp_path / "中文检查号.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write(
            '患者姓名,检查号,备注\r\n'
            '张三,A001,首诊\r\n'
            '李四,"=CONCAT(\"\"CT\"\",2026)",仅作为文本\r\n'
            '王五,A001,重复\r\n'
            '赵六,,空值\r\n'
        )

    result = import_accession_file(source)

    assert result.values == ("A001", '=CONCAT("CT",2026)')
    assert result.total_rows == 4
    assert result.blank_count == 1
    assert result.duplicate_count == 1
    assert result.selected_column is not None
    assert result.selected_column.index == 1
    assert result.selected_column.name == "检查号"
    assert [column.name for column in result.available_columns] == [
        "患者姓名",
        "检查号",
        "备注",
    ]


def test_csv_requires_selection_then_supports_name_and_zero_based_index(tmp_path):
    source = tmp_path / "columns.csv"
    source.write_text("患者,业务编号\n张三,X001\n李四,X002\n", encoding="utf-8")

    with pytest.raises(ColumnSelectionError) as caught:
        import_accession_file(source)
    assert [(column.index, column.name) for column in caught.value.columns] == [
        (0, "患者"),
        (1, "业务编号"),
    ]

    by_name = import_accession_file(source, column=" 业务_编号 ")
    by_index = import_accession_file(source, column=1)

    assert by_name.values == ("X001", "X002")
    assert by_index.values == by_name.values


def test_duplicate_recognized_columns_are_not_selected_implicitly(tmp_path):
    source = tmp_path / "ambiguous.csv"
    source.write_text("检查号,Accession Number\nA001,A002\n", encoding="utf-8")

    with pytest.raises(ColumnSelectionError, match="多个检查号列"):
        import_accession_file(source)
    with pytest.raises(ColumnSelectionError, match="同名列"):
        duplicate = tmp_path / "duplicate.csv"
        duplicate.write_text("检查号,检查号\nA001,A002\n", encoding="utf-8")
        import_accession_file(duplicate, column="检查号")


def test_malformed_csv_is_reported_as_import_error(tmp_path):
    source = tmp_path / "损坏.csv"
    source.write_text('检查号\n"未闭合字段', encoding="utf-8")

    with pytest.raises(AccessionImportError, match="CSV 文件格式损坏"):
        import_accession_file(source)


def test_xlsx_reads_shared_strings_and_sparse_selected_column(tmp_path):
    source = _write_xlsx(
        tmp_path / "共享字符串.xlsx",
        _worksheet(
            """
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>
<row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2" t="s"><v>3</v></c></row>
<row r="3"><c r="A3" t="s"><v>4</v></c></row>
<row r="4"><c r="C4" t="s"><v>3</v></c></row>
"""
        ),
        shared_strings_xml=_shared_strings(
            ["患者姓名", "访问号码", "张三", "CT202601261643", "李四"]
        ),
    )

    result = import_accession_file(source)

    assert result.values == ("CT202601261643",)
    assert result.total_rows == 3
    assert result.blank_count == 1
    assert result.duplicate_count == 1
    assert result.selected_column is not None
    assert result.selected_column.index == 2
    assert [column.name for column in result.available_columns] == [
        "患者姓名",
        "第 2 列",
        "访问号码",
    ]


def test_xlsx_reads_inline_and_rich_text_cells(tmp_path):
    source = _write_xlsx(
        tmp_path / "内联字符串.xlsx",
        _worksheet(
            """
<row r="1"><c r="A1" t="inlineStr"><is><r><t>Accession</t></r><r><t> Number</t></r></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><r><t>CT</t></r><r><t>001</t></r></is></c></row>
<row r="3"><c r="A3"><v>202601261643</v></c></row>
"""
        ),
    )

    result = import_accession_file(source)

    assert result.values == ("CT001", "202601261643")
    assert result.selected_column is not None
    assert result.selected_column.name == "Accession Number"


def test_xlsx_preserves_leading_zeroes_from_all_zero_number_format(tmp_path):
    styles = f"""<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="{MAIN_NS}">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="000000"/></numFmts>
  <cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs>
</styleSheet>"""
    source = _write_xlsx(
        tmp_path / "前导零.xlsx",
        _worksheet(
            """
<row r="1"><c r="A1" t="inlineStr"><is><t>检查号</t></is></c></row>
<row r="2"><c r="A2" s="1"><v>1234</v></c></row>
"""
        ),
        styles_xml=styles,
    )

    result = import_accession_file(source)

    assert result.values == ("001234",)


def test_xlsx_rejects_numeric_accession_format_that_cannot_be_preserved(tmp_path):
    styles = f"""<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="{MAIN_NS}">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="000-000"/></numFmts>
  <cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs>
</styleSheet>"""
    source = _write_xlsx(
        tmp_path / "不安全数字样式.xlsx",
        _worksheet(
            """
<row r="1"><c r="A1" t="inlineStr"><is><t>检查号</t></is></c></row>
<row r="2"><c r="A2" s="1"><v>123456</v></c></row>
"""
        ),
        styles_xml=styles,
    )

    with pytest.raises(AccessionImportError, match="设置为文本"):
        import_accession_file(source)


def test_xlsx_rejects_formula_in_selected_column_even_with_cached_value(tmp_path):
    source = _write_xlsx(
        tmp_path / "公式.xlsx",
        _worksheet(
            """
<row r="1"><c r="A1" t="inlineStr"><is><t>检查号</t></is></c></row>
<row r="2"><c r="A2"><f>WEBSERVICE(&quot;https://invalid.example&quot;)</f><v>SAFE-CACHED</v></c></row>
"""
        ),
    )

    with pytest.raises(AccessionImportError, match="含公式"):
        import_accession_file(source)


@pytest.mark.parametrize(
    "filename,content",
    [
        ("不是工作簿.xlsx", b"not-a-zip"),
        ("截断工作簿.xlsx", b"PK\x03\x04broken"),
    ],
)
def test_damaged_xlsx_is_reported_as_import_error(tmp_path, filename, content):
    source = tmp_path / filename
    source.write_bytes(content)

    with pytest.raises(AccessionImportError, match="损坏|有效"):
        import_accession_file(source)


def test_xlsx_rejects_shared_string_out_of_range_and_xml_entities(tmp_path):
    invalid_reference = _write_xlsx(
        tmp_path / "引用损坏.xlsx",
        _worksheet(
            '<row r="1"><c r="A1" t="s"><v>99</v></c></row>'
        ),
        shared_strings_xml=_shared_strings(["检查号"]),
    )
    with pytest.raises(AccessionImportError, match="共享字符串引用"):
        import_accession_file(invalid_reference)

    entity = _write_xlsx(
        tmp_path / "实体.xlsx",
        '<!DOCTYPE worksheet [<!ENTITY x "检查号">]>'
        f'<worksheet xmlns="{MAIN_NS}"><sheetData/></worksheet>',
    )
    with pytest.raises(AccessionImportError, match="实体声明"):
        import_accession_file(entity)


def test_import_limits_cover_file_rows_cells_and_zip_compression(tmp_path):
    oversized = tmp_path / "oversized.txt"
    oversized.write_text("A001\nA002", encoding="utf-8")
    with pytest.raises(ImportLimitError, match="大小"):
        import_accession_file(oversized, limits=ImportLimits(max_input_bytes=4))

    rows = tmp_path / "rows.txt"
    rows.write_text("A001\nA002", encoding="utf-8")
    with pytest.raises(ImportLimitError, match="行数"):
        import_accession_file(rows, limits=ImportLimits(max_rows=1))

    long_cell = tmp_path / "long.csv"
    long_cell.write_text("检查号\nTOO-LONG", encoding="utf-8")
    with pytest.raises(ImportLimitError, match="单元格"):
        import_accession_file(long_cell, limits=ImportLimits(max_cell_characters=4))

    compressed = _write_xlsx(
        tmp_path / "高压缩比.xlsx",
        _worksheet(
            '<row r="1"><c r="A1" t="inlineStr"><is><t>检查号</t></is></c></row>'
        ),
        extra_members={"xl/padding.xml": "0" * 10_000},
    )
    with pytest.raises(ImportLimitError, match="压缩比"):
        import_accession_file(
            compressed,
            limits=ImportLimits(max_zip_compression_ratio=2),
        )


def test_xlsx_rejects_relationship_path_escape(tmp_path):
    source = _write_xlsx(
        tmp_path / "路径逃逸.xlsx",
        _worksheet(""),
        target="../../outside.xml",
    )

    with pytest.raises(AccessionImportError, match="路径不安全"):
        import_accession_file(source)


def test_unsupported_extension_and_txt_column_are_rejected(tmp_path):
    unsupported = tmp_path / "检查号.xls"
    unsupported.write_bytes(b"legacy")
    with pytest.raises(AccessionImportError, match="仅支持"):
        import_accession_file(unsupported)

    text = tmp_path / "检查号.txt"
    text.write_text("A001", encoding="utf-8")
    with pytest.raises(ColumnSelectionError, match="不支持选列"):
        import_accession_file(text, column=0)
