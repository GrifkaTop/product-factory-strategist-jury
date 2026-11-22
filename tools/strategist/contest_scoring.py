"""Calculate a documented assessment without inferring untested capabilities."""
from .errors import ValidationError
from .token_usage import compare_token_usage, summarize_token_usage


_EVIDENCE = ("artifact", "run", "measurement")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Заполните {label}")
    return value.strip()


def _rating(indicator, assessment):
    identity = indicator["id"]
    if not isinstance(assessment, dict):
        raise ValidationError(f"{identity}: оценка должна быть объектом")
    level = assessment.get("level")
    why = _text(assessment.get("why"), f"{identity}.why")
    evidence = assessment.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValidationError(f"{identity}.evidence должен быть списком")
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") not in _EVIDENCE:
            raise ValidationError(f"{identity}: kind должен быть artifact, run или measurement")
        _text(item.get("locator"), f"{identity}.evidence.locator")
    if level is not None:
        if type(level) is not int or not 0 <= level <= 4:
            raise ValidationError(f"{identity}: укажите уровень 0–4 или null")
        if not any(item["kind"] in indicator["evidence"] for item in evidence):
            raise ValidationError(f"{identity}: требуется свидетельство типа " + " / ".join(indicator["evidence"]))
    return {"id": identity, "level": level, "why": why, "evidence": evidence,
            "status": "assessed" if level is not None else "not_tested",
            "points": indicator["points"] * level / 4 if level is not None else None,
            "max_points": indicator["points"]}


def score_contest(rubric, card):
    """A missing observation does not become a zero or increase other weights."""
    if card.get("rubric_id") != rubric["id"]:
        raise ValidationError("rubric_id не соответствует используемой шкале")
    profile = _text(card.get("profile"), "profile")
    profiles = rubric["profiles"]
    if profile != "full" and profile not in profiles:
        raise ValidationError("Выберите full, brief или workflow")
    subject = _text(card.get("subject"), "subject")
    assessor = _text(card.get("assessor"), "assessor")
    assessments = card.get("assessments")
    if not isinstance(assessments, dict):
        raise ValidationError("assessments должен быть объектом")
    dimensions = rubric["dimensions"]
    selected = {item["id"] for dimension in dimensions for item in dimension["indicators"]} if profile == "full" else set(profiles[profile])
    if set(assessments) - selected:
        raise ValidationError("Показатель отсутствует в выбранном профиле: "
                              + ", ".join(sorted(set(assessments) - selected)))
    bands, gaps = [], []
    for dimension in dimensions:
        rows = [_rating(item, assessments.get(item["id"], {"why": "Нет наблюдения в карточке"}))
                for item in dimension["indicators"] if item["id"] in selected]
        if not rows:
            continue
        assessed = [row for row in rows if row["points"] is not None]
        assessed_max = sum(row["max_points"] for row in assessed)
        scope_max = sum(row["max_points"] for row in rows)
        points = sum(row["points"] for row in assessed)
        gaps.extend(row["id"] for row in rows if row["points"] is None)
        bands.append({"id": dimension["id"], "label": dimension["label"],
                      "points_awarded": points if assessed else None, "assessed_max": assessed_max,
                      "scope_max": scope_max, "criterion_max": 10, "indicators": rows})
    known_max = sum(band["assessed_max"] for band in bands)
    scope_max = sum(band["scope_max"] for band in bands)
    points = sum(band["points_awarded"] or 0 for band in bands)
    result = {"rubric_id": rubric["id"], "profile": profile, "subject": subject, "assessor": assessor,
            "dimensions": bands, "untested": gaps,
            "points_awarded": points if known_max else None, "assessed_max": known_max, "scope_max": scope_max,
            "profile_score": points if not gaps else None,
            "contest_score_estimate": points if profile == "full" and not gaps else None,
            "scope": "Оценка по внутренней детализации четырёх критериев. Программа проверяет форму "
                     "и тип источника, смысл оценки и подлинность сведений проверяет оценщик."}
    if "token_usage" in card:
        result["token_usage"] = summarize_token_usage(card["token_usage"])
    if "token_accounting" in selected:
        token_level = next(row["level"] for band in bands for row in band["indicators"]
                           if row["id"] == "token_accounting")
        usage = result.get("token_usage")
        if token_level and (usage is None or usage["basis"] != "api_estimate" or usage["known_tokens_subtotal"] is None):
            raise ValidationError("Начисление баллов за token_accounting требует наблюдаемого token_usage")
        if token_level == 4 and (usage["tokens"] is None or usage["estimated_cost"] is None):
            raise ValidationError("Уровень 4 за token_accounting требует полного покрытия токенов и тарифов")
    return result


def compare_scorecards(current, baseline):
    fields = ("rubric_id", "profile", "untested")
    if any(current[field] != baseline[field] for field in fields):
        raise ValidationError("Сравнение требует одинакового профиля, версии и набора оценённых показателей")
    if current["assessed_max"] == 0:
        raise ValidationError("Для сравнения нужен хотя бы один оценённый показатель")
    result = {"baseline": baseline["subject"], "assessed_max": current["assessed_max"],
            "difference_points": current["points_awarded"] - baseline["points_awarded"],
            "scope": "Сравнение по общим оценённым показателям; эквивалентность задания и условий проверяет оценщик."}
    if "token_usage" in current or "token_usage" in baseline:
        result["token_usage"] = compare_token_usage(current.get("token_usage"), baseline.get("token_usage"))
    return result
