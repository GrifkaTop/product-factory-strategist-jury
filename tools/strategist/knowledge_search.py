"""Search the project's methodology without loading case data."""
import re

from .errors import ValidationError
from .state import _path


_TERMS = (
    ("retention", "удержание"),
    ("churn", "отток"),
    ("baseline", "база сравнения", "контрольная группа"),
    ("guardrails", "защитные метрики", "нежелательные последствия"),
    ("positioning", "позиционирование"),
    ("unit economics", "юнит-экономика", "экономика"),
    ("source", "источник", "доказательство"),
)


def _normalize(text):
    return " ".join(re.findall(r"[^\W_]+", text.casefold().replace("ё", "е")))


def search_knowledge(root, query):
    if not isinstance(query, str) or len(query) > 160 or not (needle := _normalize(query)):
        raise ValidationError("Введите тему на русском или английском до 160 символов")
    synonyms = {_normalize(term) for group in _TERMS if needle in map(_normalize, group) for term in group}
    related = synonyms - {needle}
    results = []
    for section in ("knowledge", "methods", "instructions"):
        for candidate in sorted(_path(root, section).glob("*.md")):
            path = _path(root, section, candidate.name)
            if not path.is_file() or path.stat().st_size > 200_000:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                text = " " + _normalize(line) + " "
                exact = f" {needle} " in text
                if exact or any(f" {term} " in text for term in related):
                    results.append({"path": f"{section}/{path.name}", "line": number,
                                    "text": line.strip()[:260], "exact_phrase": exact})
    results.sort(key=lambda row: not row["exact_phrase"])
    return {"query": query, "related_terms": sorted(related), "matches": results[:12],
            "total_matches": len(results), "read_only": True,
            "scope": "Словарный поиск по методикам. Результаты помогают выбрать процедуру и источник, "
                     "а факты конкретного кейса берутся из его реестра."}
