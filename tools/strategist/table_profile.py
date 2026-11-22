"""Compact diagnostics for a registered delimited table."""
from collections import Counter

from .errors import ValidationError
from .feature_io import _source_data
from .ingest import _tabular


def registered_table(case_dir, state, source_id):
    source = next((row for row in state["registries"]["sources"] if row["id"] == source_id), None)
    if not source or source.get("kind") not in {"csv", "tsv"}:
        raise ValidationError("Укажите ID импортированного источника CSV или TSV")
    data = _source_data(case_dir, source).decode(source["encoding"])
    _, tables, _ = _tabular(data, "," if source["kind"] == "csv" else "\t")
    return source, tables[0]["rows"]


def profile_table(case_dir, state, source_id, header=False):
    source, registered = registered_table(case_dir, state, source_id)
    records = [row["cells"] for row in registered]
    names = records[0] if header and records else []
    rows = records[1:] if header else records
    widths = Counter(map(len, rows))
    width = max(max(widths, default=0), len(names))
    counts = Counter(name.strip().casefold() for name in names)
    columns = []
    for index in range(min(width, 50)):
        name = names[index] if index < len(names) else None
        columns.append({"number": index + 1, "header": name[:160] if name is not None else None,
                        "empty_values": sum(index < len(row) and not row[index].strip() for row in rows),
                        "absent_cells": sum(count for row_width, count in widths.items() if row_width <= index),
                        "duplicate_header": name is not None and bool(name.strip())
                                            and counts[name.strip().casefold()] > 1})
    return {"case_id": state["case_id"], "revision": state["revision"], "source_id": source_id,
            "recovery_warning": state.get("recovery_warning"), "synthetic": source.get("synthetic"),
            "data_rows": len(rows), "header_used": bool(header and records),
            "row_widths": dict(widths), "columns": columns, "omitted_columns": max(0, width - 50),
            "read_only": True, "scope": "Проверка структуры сохранённого CSV/TSV. "
                                      "Пустые поля, отсутствующие ячейки и качество данных оцениваются отдельно."}
