from __future__ import annotations

import csv
import io
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOCUMENT_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REFERENCE_RE = re.compile(r"^([A-Za-z]+)[1-9][0-9]*$")
_INVALID_ACCESSION_RE = re.compile(r"[\\*?\x00-\x1f\x7f]")
_ZERO_NUMBER_FORMAT_RE = re.compile(r"0+")
_BUILTIN_NUMBER_FORMATS = {
    0: "General",
    1: "0",
    49: "@",
}
_ACCESSION_HEADERS = {
    "accession",
    "accessionno",
    "accessionnumber",
    "accessno",
    "accno",
    "检查号",
    "检查号码",
    "检查编号",
    "访问号",
    "访问号码",
}


class AccessionImportError(ValueError):
    """Base class for safe accession-file import failures."""


class ImportLimitError(AccessionImportError):
    pass


class ColumnSelectionError(AccessionImportError):
    def __init__(self, message: str, columns: tuple["ImportColumn", ...] = ()):
        super().__init__(message)
        self.columns = columns


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_input_bytes: int = 32 * 1024 * 1024
    max_rows: int = 100_000
    max_columns: int = 256
    max_cell_characters: int = 4_096
    max_zip_entries: int = 512
    max_zip_member_bytes: int = 64 * 1024 * 1024
    max_zip_uncompressed_bytes: int = 128 * 1024 * 1024
    max_zip_compression_ratio: float = 250.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_input_bytes,
            self.max_rows,
            self.max_columns,
            self.max_cell_characters,
            self.max_zip_entries,
            self.max_zip_member_bytes,
            self.max_zip_uncompressed_bytes,
        )
        if any(value <= 0 for value in integer_limits):
            raise ValueError("导入安全限制必须大于 0")
        if self.max_zip_compression_ratio <= 0:
            raise ValueError("ZIP 压缩比限制必须大于 0")


@dataclass(frozen=True, slots=True)
class ImportColumn:
    """A selectable zero-based source column."""

    index: int
    name: str


@dataclass(frozen=True, slots=True)
class AccessionImportResult:
    values: tuple[str, ...]
    total_rows: int
    blank_count: int
    duplicate_count: int
    invalid_values: tuple[str, ...]
    available_columns: tuple[ImportColumn, ...] = ()
    selected_column: ImportColumn | None = None
    encoding: str = ""

    @property
    def valid_count(self) -> int:
        return len(self.values)

    @property
    def invalid_count(self) -> int:
        return len(self.invalid_values)


@dataclass(frozen=True, slots=True)
class _Cell:
    value: str
    formula: bool = False
    numeric: bool = False
    style_index: int | None = None


def import_accession_file(
    path: str | Path,
    *,
    column: str | int | None = None,
    limits: ImportLimits | None = None,
) -> AccessionImportResult:
    """Import accessions from TXT, CSV, or XLSX without evaluating formulas.

    CSV and XLSX inputs use the first non-empty row as their header. ``column``
    may be an exact/normalized header name or a zero-based column index. When
    omitted, a common Chinese or English accession header is selected.
    """

    active_limits = limits or ImportLimits()
    source = _validated_source(path, active_limits)
    suffix = source.suffix.casefold()
    if suffix == ".txt":
        if column is not None:
            raise ColumnSelectionError("TXT 文件不支持选列")
        data = _read_bounded(source, active_limits.max_input_bytes)
        text, encoding = _decode_text(data)
        rows = text.splitlines()
        _check_row_count(len(rows), active_limits)
        return _build_result(rows, total_rows=len(rows), encoding=encoding)
    if suffix == ".csv":
        return _import_csv(source, column, active_limits)
    if suffix == ".xlsx":
        return _import_xlsx(source, column, active_limits)
    raise AccessionImportError("仅支持 TXT、CSV 和 XLSX 文件")


def _validated_source(path: str | Path, limits: ImportLimits) -> Path:
    source = Path(path).expanduser()
    try:
        if source.is_symlink():
            raise AccessionImportError("导入文件不能是符号链接")
        metadata = source.stat()
    except FileNotFoundError as exc:
        raise AccessionImportError(f"导入文件不存在：{source}") from exc
    except OSError as exc:
        raise AccessionImportError(f"无法读取导入文件：{exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AccessionImportError("导入路径必须是普通文件")
    if metadata.st_size > limits.max_input_bytes:
        raise ImportLimitError("导入文件超过大小限制")
    return source


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
    except OSError as exc:
        raise AccessionImportError(f"无法读取导入文件：{exc}") from exc
    if len(data) > maximum:
        raise ImportLimitError("导入文件超过大小限制")
    return data


def _decode_text(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        try:
            return data.decode("gb18030"), "gb18030"
        except UnicodeDecodeError as exc:
            raise AccessionImportError("文本编码无法识别，请使用 UTF-8 或 GB18030") from exc


def _import_csv(
    path: Path,
    requested_column: str | int | None,
    limits: ImportLimits,
) -> AccessionImportResult:
    text, encoding = _decode_text(_read_bounded(path, limits.max_input_bytes))
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        rows: list[list[str]] = []
        for row in reader:
            if len(rows) > limits.max_rows:
                raise ImportLimitError("数据行数超过限制")
            _check_width_and_cells(row, limits)
            rows.append(row)
    except csv.Error as exc:
        raise AccessionImportError(f"CSV 文件格式损坏：{exc}") from exc

    header_index = _first_nonempty_row(rows)
    if header_index is None:
        return _build_result((), total_rows=0, encoding=encoding)
    header = rows[header_index]
    columns = _columns_from_header(header)
    selected = _select_column(columns, requested_column)
    data_rows = rows[header_index + 1 :]
    values = [row[selected.index] if selected.index < len(row) else "" for row in data_rows]
    return _build_result(
        values,
        total_rows=len(data_rows),
        columns=columns,
        selected=selected,
        encoding=encoding,
    )


def _import_xlsx(
    path: Path,
    requested_column: str | int | None,
    limits: ImportLimits,
) -> AccessionImportResult:
    try:
        with zipfile.ZipFile(path) as archive:
            _validate_archive(archive, limits)
            shared_strings = _read_shared_strings(archive, limits)
            style_formats = _read_style_formats(archive, limits)
            worksheet_path = _first_worksheet_path(archive, limits)
            worksheet = _read_xml_member(archive, worksheet_path, limits)
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise AccessionImportError("XLSX 文件损坏或不是有效的工作簿") from exc
    except KeyError as exc:
        raise AccessionImportError("XLSX 文件缺少必要的工作簿内容") from exc
    except OSError as exc:
        raise AccessionImportError(f"无法读取 XLSX 文件：{exc}") from exc

    rows = _worksheet_rows(worksheet, shared_strings, limits)
    header_index = _first_nonempty_cell_row(rows)
    if header_index is None:
        return _build_result((), total_rows=0)
    header_cells = rows[header_index]
    if any(cell.formula for cell in header_cells.values()):
        raise AccessionImportError("XLSX 表头不能使用公式")
    max_header_column = max(header_cells, default=-1)
    header = [header_cells.get(index, _Cell("")).value for index in range(max_header_column + 1)]
    columns = _columns_from_header(header)
    selected = _select_column(columns, requested_column)
    data_rows = rows[header_index + 1 :]
    values: list[str] = []
    for relative_index, row in enumerate(data_rows, start=1):
        cell = row.get(selected.index, _Cell(""))
        if cell.formula:
            raise AccessionImportError(
                f"XLSX 第 {header_index + relative_index + 1} 行检查号单元格含公式，已拒绝导入"
            )
        values.append(
            _accession_cell_value(
                cell,
                style_formats,
                row_number=header_index + relative_index + 1,
            )
        )
    return _build_result(
        values,
        total_rows=len(data_rows),
        columns=columns,
        selected=selected,
    )


def _validate_archive(archive: zipfile.ZipFile, limits: ImportLimits) -> None:
    entries = archive.infolist()
    if len(entries) > limits.max_zip_entries:
        raise ImportLimitError("XLSX 压缩包文件数量超过限制")
    total_size = 0
    seen_names: set[str] = set()
    for info in entries:
        normalized = PurePosixPath(info.filename.replace("\\", "/"))
        if (
            info.filename in seen_names
            or normalized.is_absolute()
            or ".." in normalized.parts
            or not normalized.parts
        ):
            raise AccessionImportError("XLSX 压缩包包含不安全路径或重复文件")
        seen_names.add(info.filename)
        if info.flag_bits & 0x1:
            raise AccessionImportError("不支持加密的 XLSX 文件")
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            raise AccessionImportError("XLSX 压缩包不能包含符号链接")
        if info.is_dir():
            continue
        if info.file_size > limits.max_zip_member_bytes:
            raise ImportLimitError("XLSX 内部文件超过大小限制")
        total_size += info.file_size
        if total_size > limits.max_zip_uncompressed_bytes:
            raise ImportLimitError("XLSX 解压后总大小超过限制")
        if info.file_size:
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > limits.max_zip_compression_ratio:
                raise ImportLimitError("XLSX 压缩比异常，可能是压缩炸弹")


def _read_zip_member(
    archive: zipfile.ZipFile,
    name: str,
    limits: ImportLimits,
) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limits.max_zip_member_bytes:
        raise ImportLimitError("XLSX 内部文件超过大小限制")
    try:
        with archive.open(info) as handle:
            data = handle.read(limits.max_zip_member_bytes + 1)
    except (RuntimeError, zipfile.BadZipFile) as exc:
        raise AccessionImportError("XLSX 内部文件读取失败") from exc
    if len(data) > limits.max_zip_member_bytes:
        raise ImportLimitError("XLSX 内部文件超过大小限制")
    return data


def _read_xml_member(
    archive: zipfile.ZipFile,
    name: str,
    limits: ImportLimits,
) -> ElementTree.Element:
    data = _read_zip_member(archive, name, limits)
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise AccessionImportError("XLSX XML 包含不允许的实体声明")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise AccessionImportError("XLSX XML 内容损坏") from exc


def _read_shared_strings(
    archive: zipfile.ZipFile,
    limits: ImportLimits,
) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return ()
    root = _read_xml_member(archive, "xl/sharedStrings.xml", limits)
    strings: list[str] = []
    for item in root.findall(f"{{{_SPREADSHEET_NS}}}si"):
        value = "".join(
            node.text or "" for node in item.iter(f"{{{_SPREADSHEET_NS}}}t")
        )
        _check_cell(value, limits)
        strings.append(value)
        if len(strings) > limits.max_rows * limits.max_columns:
            raise ImportLimitError("XLSX 共享字符串数量超过限制")
    return tuple(strings)


def _read_style_formats(
    archive: zipfile.ZipFile,
    limits: ImportLimits,
) -> tuple[str | None, ...]:
    """Return the effective number format for each XLSX cell style.

    Only formats needed to preserve accession text are interpreted later.  An
    unknown format is retained as ``None`` so a numeric accession cell can be
    rejected instead of silently importing a value different from Excel's
    displayed value.
    """

    if "xl/styles.xml" not in archive.namelist():
        return ()
    root = _read_xml_member(archive, "xl/styles.xml", limits)
    custom_formats: dict[int, str] = {}
    number_formats = root.find(f"{{{_SPREADSHEET_NS}}}numFmts")
    if number_formats is not None:
        for item in number_formats.findall(f"{{{_SPREADSHEET_NS}}}numFmt"):
            try:
                format_id = int(item.attrib.get("numFmtId", ""))
            except ValueError as exc:
                raise AccessionImportError("XLSX 数字格式编号无效") from exc
            format_code = item.attrib.get("formatCode", "")
            _check_cell(format_code, limits)
            custom_formats[format_id] = format_code

    cell_formats = root.find(f"{{{_SPREADSHEET_NS}}}cellXfs")
    if cell_formats is None:
        return ()
    formats: list[str | None] = []
    for item in cell_formats.findall(f"{{{_SPREADSHEET_NS}}}xf"):
        try:
            format_id = int(item.attrib.get("numFmtId", "0"))
        except ValueError as exc:
            raise AccessionImportError("XLSX 单元格数字格式编号无效") from exc
        formats.append(
            custom_formats.get(format_id, _BUILTIN_NUMBER_FORMATS.get(format_id))
        )
        if len(formats) > limits.max_columns * 16:
            raise ImportLimitError("XLSX 单元格样式数量超过限制")
    return tuple(formats)


def _first_worksheet_path(
    archive: zipfile.ZipFile,
    limits: ImportLimits,
) -> str:
    workbook = _read_xml_member(archive, "xl/workbook.xml", limits)
    relationships = _read_xml_member(
        archive, "xl/_rels/workbook.xml.rels", limits
    )
    targets: dict[str, str] = {}
    for relationship in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        if relationship.attrib.get("TargetMode", "").casefold() == "external":
            continue
        relation_id = relationship.attrib.get("Id", "")
        target = relationship.attrib.get("Target", "")
        if relation_id and target:
            targets[relation_id] = target

    sheets = workbook.find(f"{{{_SPREADSHEET_NS}}}sheets")
    if sheets is None:
        raise AccessionImportError("XLSX 工作簿不包含工作表")
    candidates = [
        sheet
        for sheet in sheets.findall(f"{{{_SPREADSHEET_NS}}}sheet")
        if sheet.attrib.get("state", "visible") == "visible"
    ]
    if not candidates:
        raise AccessionImportError("XLSX 工作簿没有可见工作表")
    relation_id = candidates[0].attrib.get(f"{{{_DOCUMENT_REL_NS}}}id", "")
    target = targets.get(relation_id, "")
    path = _safe_xlsx_target(target)
    if not path or path not in archive.namelist():
        raise AccessionImportError("XLSX 工作表引用无效")
    return path


def _safe_xlsx_target(target: str) -> str:
    if not target:
        return ""
    normalized = PurePosixPath(target.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise AccessionImportError("XLSX 工作表路径不安全")
    if normalized.parts and normalized.parts[0] == "xl":
        return normalized.as_posix()
    return (PurePosixPath("xl") / normalized).as_posix()


def _worksheet_rows(
    root: ElementTree.Element,
    shared_strings: tuple[str, ...],
    limits: ImportLimits,
) -> list[dict[int, _Cell]]:
    rows: list[dict[int, _Cell]] = []
    sheet_data = root.find(f"{{{_SPREADSHEET_NS}}}sheetData")
    if sheet_data is None:
        return rows
    for row_element in sheet_data.findall(f"{{{_SPREADSHEET_NS}}}row"):
        if len(rows) >= limits.max_rows + 1:
            raise ImportLimitError("数据行数超过限制")
        row: dict[int, _Cell] = {}
        inferred_column = 0
        for cell_element in row_element.findall(f"{{{_SPREADSHEET_NS}}}c"):
            reference = cell_element.attrib.get("r", "")
            column_index = _column_index(reference) if reference else inferred_column
            inferred_column = column_index + 1
            if column_index >= limits.max_columns:
                raise ImportLimitError("数据列数超过限制")
            row[column_index] = _xlsx_cell(cell_element, shared_strings, limits)
        rows.append(row)
    return rows


def _xlsx_cell(
    element: ElementTree.Element,
    shared_strings: tuple[str, ...],
    limits: ImportLimits,
) -> _Cell:
    formula = element.find(f"{{{_SPREADSHEET_NS}}}f") is not None
    cell_type = element.attrib.get("t", "")
    if cell_type == "inlineStr":
        value = "".join(
            node.text or "" for node in element.iter(f"{{{_SPREADSHEET_NS}}}t")
        )
    else:
        value_element = element.find(f"{{{_SPREADSHEET_NS}}}v")
        value = value_element.text or "" if value_element is not None else ""
        if cell_type == "s" and value:
            try:
                shared_index = int(value)
                if shared_index < 0:
                    raise IndexError
                value = shared_strings[shared_index]
            except (ValueError, IndexError) as exc:
                raise AccessionImportError("XLSX 共享字符串引用无效") from exc
    _check_cell(value, limits)
    style_value = element.attrib.get("s")
    style_index: int | None = None
    if style_value is not None:
        try:
            style_index = int(style_value)
        except ValueError as exc:
            raise AccessionImportError("XLSX 单元格样式引用无效") from exc
        if style_index < 0:
            raise AccessionImportError("XLSX 单元格样式引用无效")
    return _Cell(
        value=value,
        formula=formula,
        numeric=cell_type in {"", "n"} and bool(value),
        style_index=style_index,
    )


def _accession_cell_value(
    cell: _Cell,
    style_formats: tuple[str | None, ...],
    *,
    row_number: int,
) -> str:
    if not cell.numeric:
        return cell.value
    if cell.style_index is None:
        format_code = "General"
    elif not style_formats and cell.style_index == 0:
        format_code = "General"
    elif cell.style_index >= len(style_formats):
        raise AccessionImportError(
            f"XLSX 第 {row_number} 行检查号单元格样式引用无效"
        )
    else:
        format_code = style_formats[cell.style_index]

    if format_code in {"", "General", "@"}:
        return cell.value
    if format_code is not None and _ZERO_NUMBER_FORMAT_RE.fullmatch(format_code):
        if not re.fullmatch(r"[0-9]+", cell.value):
            raise AccessionImportError(
                f"XLSX 第 {row_number} 行检查号无法安全应用数字格式 {format_code!r}，"
                "请将检查号列设置为文本"
            )
        return cell.value.zfill(len(format_code))
    raise AccessionImportError(
        f"XLSX 第 {row_number} 行检查号使用了不支持的数字格式，"
        "请将检查号列设置为文本后重新导入"
    )


def _column_index(reference: str) -> int:
    match = _CELL_REFERENCE_RE.fullmatch(reference)
    if not match:
        raise AccessionImportError("XLSX 单元格引用无效")
    result = 0
    for character in match.group(1).upper():
        result = result * 26 + ord(character) - ord("A") + 1
    return result - 1


def _columns_from_header(header: list[str]) -> tuple[ImportColumn, ...]:
    columns = tuple(
        ImportColumn(index, value.strip() or f"第 {index + 1} 列")
        for index, value in enumerate(header)
    )
    if not columns:
        raise ColumnSelectionError("文件表头为空")
    return columns


def _select_column(
    columns: tuple[ImportColumn, ...],
    requested: str | int | None,
) -> ImportColumn:
    if isinstance(requested, bool):
        raise ColumnSelectionError("列序号必须是从 0 开始的整数", columns)
    if isinstance(requested, int):
        if 0 <= requested < len(columns):
            return columns[requested]
        raise ColumnSelectionError("所选列序号超出范围", columns)
    if isinstance(requested, str):
        normalized = _normalize_header(requested)
        matches = [column for column in columns if _normalize_header(column.name) == normalized]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ColumnSelectionError("存在同名列，请使用列序号选择", columns)
        raise ColumnSelectionError(f"找不到列：{requested}", columns)

    matches = [
        column
        for column in columns
        if _normalize_header(column.name) in _ACCESSION_HEADERS
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ColumnSelectionError("检测到多个检查号列，请明确选择", columns)
    if len(columns) == 1:
        return columns[0]
    raise ColumnSelectionError("未识别检查号列，请明确选择", columns)


def _normalize_header(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"[\s_\-（）()]+", "", normalized)


def _first_nonempty_row(rows: list[list[str]]) -> int | None:
    return next((index for index, row in enumerate(rows) if any(cell.strip() for cell in row)), None)


def _first_nonempty_cell_row(rows: list[dict[int, _Cell]]) -> int | None:
    return next(
        (
            index
            for index, row in enumerate(rows)
            if any(cell.value.strip() or cell.formula for cell in row.values())
        ),
        None,
    )


def _check_row_count(count: int, limits: ImportLimits) -> None:
    if count > limits.max_rows:
        raise ImportLimitError("数据行数超过限制")


def _check_width_and_cells(row: list[str], limits: ImportLimits) -> None:
    if len(row) > limits.max_columns:
        raise ImportLimitError("数据列数超过限制")
    for value in row:
        _check_cell(value, limits)


def _check_cell(value: str, limits: ImportLimits) -> None:
    if len(value) > limits.max_cell_characters:
        raise ImportLimitError("单元格内容超过长度限制")


def _build_result(
    raw_values: list[str] | tuple[str, ...],
    *,
    total_rows: int,
    columns: tuple[ImportColumn, ...] = (),
    selected: ImportColumn | None = None,
    encoding: str = "",
) -> AccessionImportResult:
    values: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []
    blank_count = 0
    duplicate_count = 0
    for raw_value in raw_values:
        value = str(raw_value).strip()
        if not value:
            blank_count += 1
            continue
        if _INVALID_ACCESSION_RE.search(value):
            invalid.append(value)
            continue
        if value in seen:
            duplicate_count += 1
            continue
        seen.add(value)
        values.append(value)
    return AccessionImportResult(
        values=tuple(values),
        total_rows=total_rows,
        blank_count=blank_count,
        duplicate_count=duplicate_count,
        invalid_values=tuple(invalid),
        available_columns=columns,
        selected_column=selected,
        encoding=encoding,
    )
