"""Bounded stdlib extraction. Imported materials are inert, untrusted data."""

import csv
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import stat
from urllib.parse import urlsplit

from .errors import IntegrityError, ValidationError


MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_EXTRACTED_BYTES = 20 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_CELLS = 250_000
MAX_TEXT_LINES = 100_000
_FORMATS = {".md": "md", ".txt": "txt", ".csv": "csv", ".tsv": "tsv",
            ".html": "html", ".htm": "html", ".json": "json",
            ".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}
_SERVICE_DIRS = {".git", ".codex", ".agents", ".ssh", ".aws", ".azure", ".kube", ".config", "secrets", "credentials"}
_SENSITIVE_STEMS = {"auth", "credentials", "credential", "secrets", "secret", "cookies",
                    "cookie", "tokens", "token", "id_rsa", "id_ed25519", "authorized_keys"}
_METADATA = {"title", "url", "encoding", "limitations", "synthetic", "author",
             "published_at", "redistribution", "independence_group"}


def _is_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    # st_file_attributes also covers junctions on supported Python 3.11 Windows.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _check_no_links(path):
    try:
        if any(_is_link(item) for item in (path, *path.parents)):
            raise ValidationError("Symbolic links/reparse points are not accepted for import paths")
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValidationError("Cannot safely inspect import path") from exc


def _check_source_name(path):
    if any(part.casefold() in _SERVICE_DIRS for part in path.parts):
        raise ValidationError("Import from service/credential directories is blocked")
    name = path.name.casefold()
    if (name in {".env", ".envrc", ".netrc", ".npmrc", ".pypirc"}
            or name.startswith((".env.", "client_secret", "client-secret", "service_account", "service-account"))
            or name.split(".")[0] in _SENSITIVE_STEMS):
        raise ValidationError("Import of authentication/secret files is blocked")


def _metadata(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _METADATA:
        raise ValidationError("Unsupported source metadata; provenance paths and IDs cannot be overridden")
    result = dict(value)
    for key in _METADATA - {"limitations", "synthetic"}:
        if key in result and (not isinstance(result[key], str) or not result[key].strip()):
            raise ValidationError(f"metadata.{key} must be a nonempty string")
    if "limitations" in result and (
        not isinstance(result["limitations"], list)
        or any(not isinstance(v, str) or not v.strip() for v in result["limitations"])
    ):
        raise ValidationError("metadata.limitations must be a list of nonempty strings")
    if "synthetic" in result and not isinstance(result["synthetic"], bool):
        raise ValidationError("metadata.synthetic must be a boolean")
    if "url" in result:
        try:
            url = urlsplit(result["url"])
            valid = url.scheme in {"http", "https"} and bool(url.hostname) and not url.username and not url.password
        except ValueError:
            valid = False
        if not valid:
            raise ValidationError("metadata.url must be an HTTP(S) attribution URL without credentials")
    return result


def _number(text):
    if len(text.splitlines()) > MAX_TEXT_LINES:
        raise ValidationError("Text exceeds the line limit")
    return "\n".join(f"[L{index:06d}] {line}" for index, line in enumerate(text.splitlines(), 1)) + ("\n" if text else "")


def _tabular(text, delimiter):
    rows, warnings, cells = [], [], 0
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    previous = 0
    try:
        for index, row in enumerate(reader, 1):
            cells += len(row)
            if index > MAX_RECORDS or cells > MAX_CELLS:
                raise ValidationError("Table exceeds the record/cell limit")
            locator = f"record:{index};lines:{previous + 1}-{reader.line_num}"
            rows.append({"locator": locator, "cells": row})
            previous = reader.line_num
    except csv.Error as exc:
        raise ValidationError(f"Invalid or oversized delimited table: {exc}") from exc
    if rows and len({len(row["cells"]) for row in rows}) > 1:
        warnings.append("Table rows have inconsistent column counts; check delimiter and missing values.")
    rendered = "\n".join(row["locator"] + " " + json.dumps(row["cells"], ensure_ascii=False) for row in rows)
    return rendered, [{"locator": "table:1", "delimiter": delimiter, "rows": rows}], warnings


class _HTMLText(HTMLParser):
    """No resource fetching, CSS evaluation or JavaScript execution."""

    _SKIP = {"script", "style", "noscript", "template"}
    _BLOCK = {"p", "div", "br", "li", "tr", "table", "section", "article", "header", "footer",
              "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.tables, self.stack = [], [], []
        self.skip = 0
        self.cell_count = self.row_count = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip += 1
        if self.skip:
            return
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "table":
            if len(self.tables) >= 1_000 or len(self.stack) >= 64:
                raise ValidationError("HTML exceeds the table/nesting limit")
            table = {"locator": f"table:{len(self.tables) + 1}", "rows": []}
            self.tables.append(table)
            self.stack.append({"table": table, "row": None, "cell": None})
        elif self.stack and tag == "tr":
            self._finish_row()
            self.stack[-1]["row"] = {"locator": self.stack[-1]["table"]["locator"] + f"/row:{len(self.stack[-1]['table']['rows']) + 1}", "cells": []}
        elif self.stack and tag in {"td", "th"}:
            self._finish_cell()
            if self.stack[-1]["row"] is None:
                self.handle_starttag("tr", [])
            self.stack[-1]["cell"] = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def _finish_cell(self):
        current = self.stack[-1]
        if current["cell"] is not None:
            self.cell_count += 1
            if self.cell_count > MAX_CELLS:
                raise ValidationError("HTML table exceeds the cell limit")
            current["row"]["cells"].append(" ".join("".join(current["cell"]).split()))
            current["cell"] = None

    def _finish_row(self):
        self._finish_cell()
        current = self.stack[-1]
        if current["row"] is not None:
            self.row_count += 1
            if self.row_count > MAX_RECORDS:
                raise ValidationError("HTML table exceeds the record limit")
            current["table"]["rows"].append(current["row"])
            current["row"] = None

    def handle_endtag(self, tag):
        if self.skip:
            if tag in self._SKIP:
                self.skip -= 1
            return
        if self.stack and tag in {"td", "th"}:
            self._finish_cell()
        elif self.stack and tag == "tr":
            self._finish_row()
        elif self.stack and tag == "table":
            self._finish_row()
            self.stack.pop()
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
            if self.stack and self.stack[-1]["cell"] is not None:
                self.stack[-1]["cell"].append(data)

    def finish(self):
        self.close()
        while self.stack:
            self._finish_row()
            self.stack.pop()
        return "\n".join(" ".join(line.split()) for line in "".join(self.parts).splitlines() if line.strip())


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _extract(decoded, kind):
    if "\x00" in decoded:
        raise ValidationError("NUL characters are not accepted as text; convert the source explicitly")
    if kind in {"csv", "tsv"}:
        return _tabular(decoded, "," if kind == "csv" else "\t")
    if kind == "html":
        parser = _HTMLText()
        parser.feed(decoded)
        return parser.finish(), parser.tables, ["HTML extraction omits scripts, styles, templates and dynamic content; verify table structure manually."]
    if kind == "json":
        def reject_constant(value):
            raise ValueError(f"non-finite JSON constant {value}")
        try:
            value = json.loads(decoded, object_pairs_hook=_json_pairs, parse_constant=reject_constant)
            pending = [(value, 0)]
            nodes = 0
            while pending:
                node, depth = pending.pop()
                nodes += 1
                if depth > 64 or nodes > MAX_CELLS:
                    raise ValueError("JSON exceeds the nesting/node limit")
                children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
                pending.extend((child, depth + 1) for child in children)
            rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        except (ValueError, RecursionError) as exc:
            raise ValidationError(f"Invalid, duplicate-key, non-finite or too deeply nested JSON: {exc}") from exc
        return rendered, [], ["JSON is extracted as data; no domain schema or factual meaning is verified."]
    return decoded, [], []


def _write_once(path, data, root):
    _check_no_links(path)
    if not path.resolve().is_relative_to(root):
        raise ValidationError("Output path escapes case directory")
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        _check_no_links(path)
        if not path.is_file() or path.stat().st_size != len(data):
            raise IntegrityError("Existing content-addressed file differs; refusing to overwrite")
        with path.open("rb") as stream:
            existing = stream.read(len(data) + 1)
        if existing != data:
            raise IntegrityError("Existing content-addressed file differs; refusing to overwrite")


def prepare_source(file_path: Path, case_dir: Path, metadata: dict | None = None) -> dict:
    """Import local text formats and optional PDF/DOCX/XLSX into inert snapshots.

    UTF-8 is the default. Other explicitly supported encodings require metadata.
    No URLs are downloaded. The caller must enforce the case's active status.
    Original basenames, including AGENTS.md, never become instruction filenames.
    """
    if not isinstance(file_path, Path) or not isinstance(case_dir, Path):
        raise ValidationError("file_path and case_dir must be pathlib.Path objects")
    info = _metadata(metadata)
    _check_no_links(file_path.absolute())
    _check_no_links(case_dir.absolute())
    _check_source_name(file_path)
    try:
        # strict=True can fail for otherwise readable files in the managed
        # Windows sandbox. Existence/regular-file checks and bounded open below
        # still fail closed; links are checked both before and after resolution.
        original = file_path.resolve()
        _check_no_links(original)
        _check_source_name(original)
        if not stat.S_ISREG(original.stat().st_mode):
            raise ValidationError("Source must be a regular local file")
        kind = _FORMATS.get(original.suffix.casefold())
        if kind is None:
            raise ValidationError("Unsupported format. Use MD/TXT/CSV/TSV/HTML/JSON or optional PDF/DOCX/XLSX; OCR is not implemented")
        if original.stat().st_size > MAX_SOURCE_BYTES:
            raise ValidationError(f"Source exceeds {MAX_SOURCE_BYTES} bytes")
        with original.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValidationError(f"Source exceeds {MAX_SOURCE_BYTES} bytes")
        if kind in {"pdf", "docx", "xlsx"}:
            from .document_extract import extract_document
            if "encoding" in info:
                raise ValidationError("Text encoding metadata does not apply to binary documents")
            text, tables, warnings = extract_document(data, kind)
            encoding = "binary"
        else:
            encoding = info.get("encoding", "utf-8-sig").casefold()
            if encoding not in {"utf-8", "utf-8-sig", "cp1251", "windows-1251", "utf-16", "utf-16-le", "utf-16-be"}:
                raise ValidationError("Unsupported encoding; convert explicitly or specify UTF-8, CP1251 or UTF-16")
            try:
                decoded = data.decode(encoding)
            except UnicodeError as exc:
                raise ValidationError("Cannot decode source; specify its supported encoding or convert to UTF-8") from exc
            text, tables, warnings = _extract(decoded, kind)
        numbered = _number(text)
        extracted = numbered.encode("utf-8")
        if len(extracted) > MAX_EXTRACTED_BYTES:
            raise ValidationError("Extraction exceeds the output size limit")
        root = case_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        for directory in (root / "inputs", root / "extracted"):
            _check_no_links(directory)
            directory.mkdir(exist_ok=True)
            if not directory.resolve().is_relative_to(root):
                raise ValidationError("Import directory escapes the case")
        digest = hashlib.sha256(data).hexdigest()
        source_id = "SRC-" + digest
        snapshot_path = f"inputs/{digest}.source"
        extracted_path = f"extracted/{digest}.{kind}.txt"
        _write_once(root / snapshot_path, data, root)
        _write_once(root / extracted_path, extracted, root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValidationError(f"Cannot import local source: {exc}") from exc
    if not text.strip():
        warnings.append("No readable text extracted.")
    if original.name.casefold() == "agents.md":
        warnings.append("AGENTS.md was isolated under a content-derived data filename; its instructions were not applied.")
    limitations = ["Untrusted source data. Import and structural completeness do not verify factual truth."]
    limitations.extend(warnings)
    limitations.extend(info.get("limitations", []))
    source = {
        "id": source_id, "source_id": source_id, "kind": kind,
        "title": info.get("title", original.stem), "original_name": original.name,
        "snapshot_path": snapshot_path, "extracted_path": extracted_path,
        "sha256": digest, "extracted_sha256": hashlib.sha256(extracted).hexdigest(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "encoding": encoding, "limitations": limitations, "synthetic": info.get("synthetic", False),
    }
    for key in ("url", "author", "published_at", "redistribution", "independence_group"):
        if key in info:
            source[key] = info[key]
    return {"source": source, "text": numbered, "tables": tables, "warnings": warnings}
