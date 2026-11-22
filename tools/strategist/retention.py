"""Decompose observed retention change for the same segments in two periods."""
from math import fsum
import re

from .errors import ValidationError
from .table_profile import registered_table


_HEADERS = ("period", "segment", "eligible", "retained")
_COUNT = re.compile(r"[0-9]{1,15}")


def compare_retention(case_dir, state, source_id, before, after):
    if not before or not after or before == after:
        raise ValidationError("Укажите два разных непустых периода")
    source, rows = registered_table(case_dir, state, source_id)
    if not rows:
        raise ValidationError("Таблица пуста")
    heading = [name.strip().casefold() for name in rows[0]["cells"]]
    if len(heading) != len(set(heading)) or not set(_HEADERS).issubset(heading):
        raise ValidationError("Нужны уникальные столбцы period, segment, eligible, retained")
    indices = [heading.index(key) for key in _HEADERS]
    groups = {before: {}, after: {}}
    for row in rows[1:]:
        if not row["cells"]:
            continue
        if len(row["cells"]) != len(heading):
            raise ValidationError(f"Ширина записи не совпадает с заголовком ({row['locator']})")
        period, segment, eligible, retained = (row["cells"][index].strip() for index in indices)
        if period not in groups:
            continue
        if (not segment or len(segment) > 120 or segment in groups[period]
                or not _COUNT.fullmatch(eligible) or not _COUNT.fullmatch(retained)):
            raise ValidationError(f"Неверный или повторный сегмент либо счётчик ({row['locator']})")
        denominator, numerator = int(eligible), int(retained)
        if denominator <= 0 or numerator > denominator:
            raise ValidationError(f"Нужно 0 ≤ retained ≤ eligible и eligible > 0 ({row['locator']})")
        groups[period][segment] = (denominator, numerator, row["locator"])

    first, second = groups[before], groups[after]
    if not first or first.keys() != second.keys():
        raise ValidationError("Оба периода должны содержать одинаковые непустые наборы сегментов")
    n1, n2 = (sum(row[0] for row in group.values()) for group in (first, second))
    y1_total, y2_total = (sum(row[1] for row in group.values()) for group in (first, second))
    pooled_total = n1 + n2
    segments = []
    for key in sorted(first):
        d1, y1, locator1 = first[key]
        d2, y2, locator2 = second[key]
        r1, r2 = y1 / d1, y2 / d2
        w1, w2 = d1 / n1, d2 / n2
        delta = (y2 * d1 - y1 * d2) / (d1 * d2) * 100
        rate_part = (w1 + w2) * delta / 2
        mix_part = (r1 + r2) * (d2 * n1 - d1 * n2) / (n1 * n2) * 50
        pooled_eligible = d1 + d2
        segments.append({"segment": key, "before_locator": locator1, "after_locator": locator2,
                         "before": {"eligible": d1, "retained": y1, "retention_pct": r1 * 100, "share": w1},
                         "after": {"eligible": d2, "retained": y2, "retention_pct": r2 * 100, "share": w2},
                         "delta_pp": delta, "pooled_eligible": pooled_eligible,
                         "pooled_weight": pooled_eligible / pooled_total,
                         "within_segment_pp": rate_part, "mix_pp": mix_part})
    standardized = {
        "reference": "pooled_eligible", "eligible": pooled_total,
        "before_retention_pct": fsum(row["pooled_weight"] * row["before"]["retention_pct"] for row in segments),
        "after_retention_pct": fsum(row["pooled_weight"] * row["after"]["retention_pct"] for row in segments),
        "delta_pp": fsum(row["pooled_weight"] * row["delta_pp"] for row in segments),
    }
    return {"case_id": state["case_id"], "revision": state["revision"], "source_id": source_id,
            "synthetic": source.get("synthetic"), "recovery_warning": state.get("recovery_warning"),
            "before": {"period": before, "eligible": n1, "retained": y1_total, "retention_pct": y1_total / n1 * 100},
            "after": {"period": after, "eligible": n2, "retained": y2_total, "retention_pct": y2_total / n2 * 100},
            "delta_pp": (y2_total * n1 - y1_total * n2) / (n1 * n2) * 100,
            "standardized": standardized,
            "within_segment_pp": fsum(row["within_segment_pp"] for row in segments),
            "mix_pp": fsum(row["mix_pp"] for row in segments),
            "segments": segments, "read_only": True,
            "scope": "Арифметика сегментов. Перед выводом проверьте одинаковые окна, определения "
                     "исхода и отсутствие двойного учёта. Стандартизация использует объединённый состав, "
                     "а разложение — среднее двух распределений. Ни один расчёт не устанавливает причину."}
