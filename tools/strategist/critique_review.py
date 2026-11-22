"""Compare a critic's response with objections saved in the same revision."""
from collections import Counter

from .errors import ValidationError
from .state import _options_hash


_MODES = {"main_context", "separate_context", "different_family"}
_ASSESSMENTS = {"new", "repeat", "unclear"}
_ACTIONS = {"reconsider", "verify", "none"}


def _text(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 1500:
        raise ValidationError(f"Укажите непустое поле {key} до 1500 символов")
    return value.strip()


def _normalized(text):
    return " ".join(text.casefold().split())


def compare_critique(state, result):
    if (state.get("recovery_warning") or result.get("case_id") != state["case_id"]
            or type(result.get("revision")) is not int or result["revision"] != state["revision"]
            or result.get("options_hash") != _options_hash(state)):
        raise ValidationError("Отзыв должен соответствовать кейсу, проверенной ревизии и хешу вариантов")
    mode = _text(result, "mode")
    if mode not in _MODES:
        raise ValidationError("Неизвестный режим критики")
    reviewer = _text(result, "reviewer")
    options = {option["id"]: option for option in state["registries"]["options"]}
    items = result.get("items")
    if len(options) < 3 or not isinstance(items, list) or len(items) != len(options):
        raise ValidationError("Нужно предоставить одну запись критика для каждого из минимум трёх вариантов")
    compared = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("Запись критика должна быть объектом")
        identity = _text(item, "option_id")
        if identity not in options or identity in compared:
            raise ValidationError("В отзыве неизвестный или повторный ID варианта")
        original = _text(options[identity], "objection")
        objection = _text(item, "objection")
        assessment = _text(item, "comparison")
        action = _text(item, "action")
        if assessment not in _ASSESSMENTS or action not in _ACTIONS:
            raise ValidationError("Некорректная ручная оценка новизны или действия")
        exact_repeat = _normalized(original) == _normalized(objection)
        compared[identity] = {"option_id": identity, "original_objection": original,
                              "critic_objection": objection,
                              "comparison": "repeat" if exact_repeat else assessment,
                              "basis": "exact_text" if exact_repeat else "assessor",
                              "action": action, "reason": _text(item, "reason")}
    counts = Counter(row["comparison"] for row in compared.values())
    return {"case_id": state["case_id"], "revision": state["revision"],
            "options_hash": result["options_hash"], "mode_reported": mode, "reviewer_reported": reviewer,
            "counts": {key: counts[key] for key in ("new", "repeat", "unclear")},
            "review": [compared[key] for key in sorted(compared)], "read_only": True,
            "scope": "Дословные повторы сверяет программа. Перефразы, новизну, отдельный контекст "
                     "и влияние на выбор фиксирует проверяющий. Решение остаётся за человеком."}
