"""Check a proposed customer-visible contract against operation states."""
import hashlib
import json
import re

from .errors import ValidationError


_EXPECTED = {"pending": "pending", "confirmed": "success", "failed": "failure", "access_revoked": "unavailable"}
_CONTRACT_FIELDS = ("protected_fields", "success_fields", "states")


def _fields(value, label):
    if (not isinstance(value, list) or any(not isinstance(field, str) or not field.strip() for field in value)
            or len(set(value)) != len(value)):
        raise ValidationError(f"{label} должен быть списком уникальных идентификаторов")
    return set(value)


def _review_wording(review, digest):
    if not isinstance(review, dict) or set(review) != {"contract_sha256", "reviewer", "states"}:
        raise ValidationError("wording_review должен содержать contract_sha256, reviewer и states")
    supplied = review["contract_sha256"]
    reviewer, states = review["reviewer"], review["states"]
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise ValidationError("wording_review.contract_sha256 должен быть SHA-256 в нижнем регистре")
    if reviewer not in ("human", "llm") or not isinstance(states, dict) or set(states) != set(_EXPECTED):
        raise ValidationError("Укажите reviewer human или llm и оценку текста каждого состояния")
    if supplied != digest:
        return "stale", [{"state": "wording_review", "code": "wording_review_stale"}]
    issues = []
    for state, row in states.items():
        if not isinstance(row, dict) or set(row) != {"verdict", "reason"}:
            raise ValidationError(f"wording_review.states.{state} требует verdict и reason")
        verdict, reason = row["verdict"], row["reason"]
        if verdict not in ("matches", "contradicts", "unclear") or not isinstance(reason, str) or not reason.strip():
            raise ValidationError(f"Укажите verdict и непустое обоснование для {state}")
        if verdict != "matches":
            issues.append({"state": state, "code": "wording_" + verdict, "reason": reason})
    return ("rejected" if issues else "reviewed"), issues


def check_feedback(data, *, require_wording_review=False):
    if not isinstance(data, dict) or not isinstance(data.get("states"), dict):
        raise ValidationError("Укажите контракт операции и объект states")
    protected = _fields(data.get("protected_fields"), "protected_fields")
    required = _fields(data.get("success_fields"), "success_fields")
    if not required or any(not isinstance(key, str) or key not in _EXPECTED for key in data["states"]):
        raise ValidationError("Укажите success_fields и известные состояния операции")
    issues = []
    for backend, expected in _EXPECTED.items():
        row = data["states"].get(backend)
        if row is None:
            issues.append({"state": backend, "code": "missing_state"})
            continue
        if not isinstance(row, dict):
            raise ValidationError(f"{backend}: ожидается объект состояния")
        fields = _fields(row.get("visible_fields"), backend + ".visible_fields")
        for key in ("message", "next_action"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                issues.append({"state": backend, "code": "missing_" + key})
        if row.get("display_status") != expected:
            issues.append({"state": backend, "code": "misleading_status"})
        if backend == "confirmed" and not required <= fields:
            issues.append({"state": backend, "code": "unconfirmed_details_missing",
                           "fields": sorted(required - fields)})
        if backend == "access_revoked" and protected & fields:
            issues.append({"state": backend, "code": "protected_details_after_revocation",
                           "fields": sorted(protected & fields)})
    structural_ok = not issues
    try:
        encoded = json.dumps({key: data[key] for key in _CONTRACT_FIELDS}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ValidationError("Матрица должна содержать конечные JSON-данные") from error
    digest = hashlib.sha256(encoded).hexdigest()
    wording_status = "unreviewed"
    if "wording_review" in data:
        wording_status, wording_issues = _review_wording(data["wording_review"], digest)
        issues.extend(wording_issues)
    elif require_wording_review:
        issues.append({"state": "wording_review", "code": "wording_review_required"})
    return {"ok": not issues, "structural_ok": structural_ok, "issues": issues, "read_only": True,
            "wording_status": wording_status, "contract_sha256": digest,
            "scope": "Структуру проверяет скрипт. wording_review хранит вывод проверяющего о смысле текста. "
                     "Реальные права и переходы проверяются в прототипе или пилоте."}
