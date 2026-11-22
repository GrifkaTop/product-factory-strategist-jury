"""Pure executive snapshot of a verified case; no monitoring or side effects."""

from copy import deepcopy
import hashlib
import json

from .errors import ValidationError


_REGISTRIES = ("sources", "claims", "hypotheses", "gaps", "options", "decisions",
               "promises", "experiments")
_BLOCKED = {"blocked", "rejected", "withdrawn"}
_CLOSED_GAPS = {"closed", "resolved", "rejected", "withdrawn"}


def _check_shape(state):
    if not isinstance(state, dict):
        raise ValidationError("Обзор: state должен быть объектом")
    if type(state.get("schema_version")) is not int or state["schema_version"] != 1:
        raise ValidationError("Обзор: требуется schema_version=1")
    if type(state.get("revision")) is not int or state["revision"] < 0:
        raise ValidationError("Обзор: revision должен быть неотрицательным целым")
    for field in ("case_id", "phase", "status"):
        if not isinstance(state.get(field), str) or not state[field].strip():
            raise ValidationError("Обзор: требуется текстовое поле " + field)
    registries = state.get("registries")
    if not isinstance(registries, dict):
        raise ValidationError("Обзор: registries должен быть объектом")
    for name in _REGISTRIES:
        records = registries.get(name)
        if not isinstance(records, list):
            raise ValidationError("Обзор: реестр должен быть списком: " + name)
        seen = set()
        for item in records:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                raise ValidationError("Обзор: запись требует текстовый id: " + name)
            if item["id"] in seen:
                raise ValidationError("Обзор: повтор id в реестре " + name)
            seen.add(item["id"])
            for field in ("status", "selected_option_id", "options_hash", "capability_status"):
                if field in item and not isinstance(item[field], str):
                    raise ValidationError("Обзор: поле должно быть текстом: " + name + "." + field)
            for field in ("owner", "responsible", "metric", "timeframe", "review_condition", "decision_rule"):
                if name == "experiments" and item.get(field) is not None and not isinstance(item[field], str):
                    raise ValidationError("Обзор: поле обязательства должно быть текстом: " + field)
            if name == "experiments":
                value = item.get("guardrails")
                if value is not None and not (isinstance(value, str) or
                        isinstance(value, list) and all(isinstance(v, str) for v in value)):
                    raise ValidationError("Обзор: guardrails должен быть текстом или списком строк")
    try:
        # Also reject non-JSON values and cycles before projecting nested values.
        json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValidationError("Обзор: состояние должно содержать конечные JSON-данные") from exc


def _present(value):
    return isinstance(value, str) and bool(value.strip()) or (
        isinstance(value, list) and bool(value) and all(_present(v) for v in value))


def build_review(state: dict) -> dict:
    """Project a verified ``load_case`` result, without reading or changing it.

    Approval means a current recorded choice, not identity verification. Promise
    availability reflects registry fields, not independent evidence verification.
    Free text is displayed as data, never interpreted as a dependency graph.
    """
    _check_shape(state)
    registries = state["registries"]
    recovery = "recovery_warning" in state
    digest = hashlib.sha256(json.dumps(registries["options"], ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    option_ids = {item["id"] for item in registries["options"]}
    current = next((item for item in reversed(registries["decisions"])
        if not recovery and item.get("status") == "approved"
        and item.get("options_hash") == digest
        and item.get("selected_option_id") in option_ids), None)
    approval = {"current": current is not None,
        "decision_id": current["id"] if current else None,
        "selected_option_id": current["selected_option_id"] if current else None,
        "reason": "Актуальный выбор записан" if current else (
            "Восстановленный checkpoint: согласование не считается актуальным" if recovery
            else "Нет актуального согласования текущих вариантов")}
    decisions_now = []

    def action(identifier, kind, message, priority="high", related_id=None):
        item = {"id": identifier, "priority": priority, "kind": kind, "message": message}
        if related_id is not None:
            item["related_id"] = related_id
        decisions_now.append(item)

    blocked_reason = (("Восстановленный checkpoint доступен только для чтения; запись заблокирована "
                       "до явного принятия восстановления") if recovery else
                      "Кейс на паузе; изменения заблокированы до явного возобновления"
                      if state["status"] == "paused" else None)
    if recovery:
        action("review:recovery", "recovery", blocked_reason)
    if state["status"] == "needs_review":
        action("review:state", "needs_review", "Текущая версия требует пересмотра человеком")
    stale = next((item for item in reversed(registries["decisions"])
                  if item.get("status") == "needs_review"), None) if current is None else None
    reconsideration = None
    if stale is not None:
        message = "Записанное решение требует пересмотра"
        invalidated = stale.get("invalidated_at_revision")
        # Comparison is unavailable on recovered history or legacy decisions
        # without a recorded revision of invalidation.
        if not recovery and type(invalidated) is int and 1 <= invalidated <= state["revision"]:
            reconsideration = {"decision_id": stale["id"], "invalidated_at_revision": invalidated,
                               "from_revision": invalidated - 1, "to_revision": state["revision"]}
            message += f". Сравнить основания через diff: r{invalidated - 1} → r{state['revision']}"
        action("review:decision:" + stale["id"], "needs_review", message, related_id=stale["id"])
    choice_stage = state["phase"] in {"options", "strategies", "validation", "complete"}
    if current is None and (choice_stage or stale is not None):
        action("review:approval", "approval", approval["reason"])

    evidence_attention = []
    linked_sources = set()
    for item in registries["claims"]:
        source_ids = item.get("source_ids")
        source_ids = [sid for sid in source_ids if isinstance(sid, str) and sid.strip()] \
            if isinstance(source_ids, list) else []
        linked_sources.update(source_ids)
        status = item.get("status")
        claim_type = item.get("type")
        if status not in {"stale", "retracted", "contested"} and not (
                claim_type == "fact" and status == "unverified"):
            continue
        evidence_attention.append({"id": item["id"],
            "type": claim_type if isinstance(claim_type, str) else None,
            "status": status,
            "source_ids": source_ids})
        action("review:evidence:" + item["id"], "evidence_review",
            "Проверить основание: " + status,
            priority="normal" if status == "unverified" else "high", related_id=item["id"])

    unlinked_source_ids = [
        source["id"] for source in registries["sources"]
        if source["id"] not in linked_sources and source.get("status") not in _BLOCKED | {"retracted"}
    ]
    for source_id in unlinked_source_ids:
        action("review:source:" + source_id, "unlinked_source",
               "Источник сохранён без связи с утверждениями. Проверьте, какие выводы он может затронуть",
               priority="normal", related_id=source_id)

    promises = []
    for item in registries["promises"]:
        status = item.get("status")
        capability_status = item.get("capability_status", "unknown")
        blocked = status in _BLOCKED
        promises.append({"id": item["id"], "status": status,
            "capability_status": capability_status,
            "capability_declared_available": capability_status == "available" and not blocked})
        if blocked or capability_status not in {"available", "planned"}:
            action("review:promise:" + item["id"], "blocked_promise",
                "Обещание заблокировано или возможность не подтверждена; нельзя представлять доступным",
                related_id=item["id"])
        elif capability_status == "planned":
            action("review:promise:" + item["id"], "planned_promise",
                "Возможность запланирована; формулировка обещания требует проверки человеком",
                priority="normal", related_id=item["id"])

    gaps = []
    for item in registries["gaps"]:
        if item.get("status") in _CLOSED_GAPS:
            continue
        question = next((item[field] for field in ("question", "text", "description")
                         if isinstance(item.get(field), str) and item[field].strip()), None)
        gaps.append({"id": item["id"], "priority": "high" if item.get("priority") == "high" else "normal",
                     "status": item.get("status", "unknown"), "question": question})

    commitments = []
    for item in registries["experiments"]:
        commitment = {"id": item["id"]}
        for field in ("owner", "responsible", "metric", "timeframe", "review_condition", "decision_rule", "guardrails"):
            commitment[field] = deepcopy(item.get(field)) if _present(item.get(field)) else None
        missing = []
        if not (commitment["owner"] or commitment["responsible"]):
            missing.append("owner/responsible")
        if not (commitment["review_condition"] or commitment["decision_rule"]):
            missing.append("review_condition/decision_rule")
        missing.extend(field for field in ("metric", "timeframe", "guardrails") if commitment[field] is None)
        commitment["missing_fields"] = missing
        commitments.append(commitment)
        if missing:
            action("review:experiment:" + item["id"], "incomplete_commitment",
                   "Дополнить план проверки: " + ", ".join(missing), priority="normal", related_id=item["id"])

    return {"schema_version": 1, "case_id": state["case_id"], "revision": state["revision"],
        "phase": state["phase"], "status": state["status"], "mode": "snapshot", "read_only": True,
        "limitation": "Снимок состояния и плана; не мониторинг выполнения и не уведомления. "
            "Доступность взята из реестра и не доказывает истинность обещания. "
            "Свободный текст не интерпретируется как граф зависимостей.",
        "approval": approval, "reconsideration": reconsideration, "decisions_now": decisions_now, "open_gaps": gaps,
        "commitments": commitments, "promises": promises, "evidence_attention": evidence_attention,
        "unlinked_source_ids": unlinked_source_ids,
        "mutations_blocked": recovery or state["status"] == "paused", "blocked_reason": blocked_reason}
