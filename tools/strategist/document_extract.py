"""Optional, read-only document adapters. No network, code execution or writes."""

import importlib
from importlib import metadata, util
import io
import json
from pathlib import PurePosixPath
import re
import zipfile
from xml.etree import ElementTree

from .errors import ValidationError


MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_ZIP_PARTS = 2000
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_PAGES = 300
MAX_PDF_STREAM_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_CELLS = 250_000
MAX_COLUMNS = 1024
_ADAPTERS = {"pdf": ("pypdf", "pypdf"), "docx": ("docx", "python-docx"),
             "xlsx": ("openpyxl", "openpyxl")}


def adapter_status() -> dict:
    result = {}
    for kind, (module, distribution) in _ADAPTERS.items():
        try:
            available = util.find_spec(module) is not None
            version = metadata.version(distribution) if available else None
        except (ImportError, ValueError, metadata.PackageNotFoundError):
            available, version = False, None
        result[kind] = {"available": available, "version": version,
                        "dependency": distribution, "install": f'python -m pip install "{distribution}"'}
    return result


def _dependency(kind):
    module, distribution = _ADAPTERS[kind]
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ValidationError(f"Optional adapter {kind} unavailable. Install free dependency: python -m pip install {distribution}") from exc


def _preflight(data, kind):
    """Bound decompression before third-party parsing; never extract paths."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        parts = archive.infolist()
        if len(parts) > MAX_ZIP_PARTS or sum(p.file_size for p in parts) > MAX_UNPACKED_BYTES:
            raise ValidationError("ZIP exceeds part/unpacked size limit")
        names = set()
        total = 0
        for part in parts:
            # ZipInfo normalizes backslashes/truncates NUL on Windows; inspect
            # the original header name before accepting the normalized part.
            name = part.orig_filename
            path = PurePosixPath(name)
            if (not name or any(ord(char) < 32 for char in name)
                    or name.startswith("/") or "\\" in name or ":" in name
                    or any(p in {".", "..", ""} for p in name.rstrip("/").split("/"))
                    or path.is_absolute() or name.casefold() in names):
                raise ValidationError("Unsafe or duplicate ZIP part path")
            names.add(name.casefold())
            if part.flag_bits & 1:
                raise ValidationError("Encrypted ZIP documents are not supported")
            if "vbaproject" in name.casefold() or name.casefold().endswith((".vba", ".xlsm", ".docm")):
                raise ValidationError("Macros are not accepted")
            with archive.open(part) as stream:
                content = stream.read(MAX_UNPACKED_BYTES - total + 1)
            total += len(content)
            if total > MAX_UNPACKED_BYTES:
                raise ValidationError("ZIP exceeds actual unpacked size limit")
            if name.casefold().endswith((".xml", ".rels")):
                probe = content.replace(b"\x00", b"").lower()
                if re.search(br"<!\s*(?:doctype|entity)\b", probe):
                    raise ValidationError("DTD/entity declarations are not accepted")
                if b"macroenabled" in probe or b"vbaproject" in probe:
                    raise ValidationError("Macro-enabled content is not accepted")
                tree = ElementTree.fromstring(content)
                if kind == "xlsx" and name.startswith("xl/worksheets/"):
                    cells = 0
                    for element in tree.iter():
                        tag = element.tag.rsplit("}", 1)[-1]
                        if tag == "row" and int(element.get("r", "1")) > MAX_RECORDS:
                            raise ValidationError("Worksheet exceeds row limit")
                        if tag == "c":
                            cells += 1
                            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", element.get("r", ""))
                            if not match:
                                raise ValidationError("Invalid spreadsheet cell coordinate")
                            column = 0
                            for letter in match[1]:
                                column = column * 26 + ord(letter) - 64
                            if column > MAX_COLUMNS or int(match[2]) > MAX_RECORDS or cells > MAX_CELLS:
                                raise ValidationError("Worksheet exceeds row/column/cell limit")


class _Output:
    def __init__(self):
        self.lines, self.size = [], 0

    def add(self, locator, value):
        line = locator + " " + json.dumps(value, ensure_ascii=False, default=str)
        self.size += len(line.encode("utf-8")) + 1
        if self.size > MAX_OUTPUT_BYTES:
            raise ValidationError("Document extraction exceeds output limit")
        self.lines.append(line)


def _pdf(data, module, output):
    reader = module.PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise ValidationError("Encrypted PDF rejected; provide an authorized unencrypted export (no password/OCR support)")
    if len(reader.pages) > MAX_PAGES:
        raise ValidationError("PDF exceeds page limit")
    warnings = ["PDF text order/layout and tables are not reconstructed reliably; verify against the original. No OCR is performed."]
    for index, page in enumerate(reader.pages, 1):
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > MAX_PDF_STREAM_BYTES:
            raise ValidationError("PDF page content stream exceeds limit")
        text = page.extract_text() or ""
        output.add(f"page:{index}", text)
        if not text.strip():
            warnings.append(f"page:{index}: no readable text; possibly a scan or blank page. OCR is not implemented.")
    return [], warnings


def _docx(data, module, output):
    document = module.Document(io.BytesIO(data))
    if len(document.paragraphs) > MAX_RECORDS:
        raise ValidationError("DOCX exceeds paragraph limit")
    for index, paragraph in enumerate(document.paragraphs, 1):
        output.add(f"paragraph:{index}", paragraph.text)
    tables, count = [], 0
    for index, table in enumerate(document.tables, 1):
        if index > MAX_RECORDS or len(table.rows) > MAX_RECORDS:
            raise ValidationError("DOCX exceeds table/row limit")
        rows = []
        for row_index, row in enumerate(table.rows, 1):
            count += len(row.cells)
            if count > MAX_CELLS:
                raise ValidationError("DOCX exceeds cell limit")
            locator = f"table:{index}/row:{row_index}"
            values = [cell.text for cell in row.cells]
            rows.append({"locator": locator, "cells": values})
            output.add(locator, values)
        tables.append({"locator": f"table:{index}", "rows": rows})
    return tables, ["DOCX extracts main-body paragraphs and top-level tables separately, not visual reading order. Headers, footers, drawings, nested tables, revisions and page layout may be omitted; verify original. External links are not fetched."]


def _xlsx(data, module, output):
    formulas = module.load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    cached = None
    tables, count = [], 0
    try:
        cached = module.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        for sheet in formulas:
            cache_sheet = cached[sheet.title]
            sheet.reset_dimensions()
            cache_sheet.reset_dimensions()
            cache_rows = iter(cache_sheet.iter_rows())
            rows = []
            for row_index, row in enumerate(sheet.iter_rows(), 1):
                if row_index > MAX_RECORDS:
                    raise ValidationError("Workbook exceeds row limit")
                cache_row = next(cache_rows, ())
                values = []
                for offset, cell in enumerate(row):
                    count += 1
                    if count > MAX_CELLS:
                        raise ValidationError("Workbook exceeds visited cell limit")
                    if cell.value is None:
                        continue
                    locator = f"sheet:{sheet.title}/cell:{cell.coordinate}"
                    entry = {"locator": locator, "value": cell.value}
                    if cell.data_type == "f":
                        entry = {"locator": locator, "formula": str(cell.value),
                                 "cached_value": cache_row[offset].value if offset < len(cache_row) else None,
                                 "cache_status": "unverified_not_recalculated"}
                    values.append(entry)
                    output.add(locator, {key: value for key, value in entry.items() if key != "locator"})
                if values:
                    rows.append({"locator": f"sheet:{sheet.title}/row:{row_index}", "cells": values})
            tables.append({"locator": f"sheet:{sheet.title}", "rows": rows})
    finally:
        formulas.close()
        if cached is not None:
            cached.close()
    return tables, ["XLSX is read-only; formulas/macros/external links are not executed. Cached values may be absent or stale and are NOT recalculated. Charts, drawings, merged-cell layout and formatting are not reproduced. Hidden sheets/cells may be included; review confidentiality before import."]


def extract_document(data: bytes, kind: str) -> tuple[str, list, list]:
    """Extract bounded bytes into locator-bearing inert data. No password or OCR."""
    if not isinstance(data, bytes) or not isinstance(kind, str) or kind not in _ADAPTERS:
        raise ValidationError("Expected bytes and kind pdf/docx/xlsx")
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ValidationError("Document is empty or exceeds input size limit")
    try:
        if kind in {"docx", "xlsx"}:
            _preflight(data, kind)
        module = _dependency(kind)
        output = _Output()
        tables, warnings = {"pdf": _pdf, "docx": _docx, "xlsx": _xlsx}[kind](data, module, output)
        return "\n".join(output.lines), tables, warnings
    except ValidationError:
        raise
    except Exception as exc:
        # Third-party parsers have different exception hierarchies; do not leak
        # raw document content or silently accept partial extraction.
        raise ValidationError(f"Cannot extract {kind}: malformed, unsupported or unsafe document ({type(exc).__name__})") from exc
