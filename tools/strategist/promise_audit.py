"""Pure promise-chain audit. Semantic verdicts are supplied, never inferred here."""

from copy import deepcopy
import json
import re

from .errors import ValidationError


_LOCATOR = re.compile(
    r"(?:L([0-9]{1,9})(?:[-–]L([0-9]{1,9}))?|line:([0-9]{1,9}))(?: \(record:[0-9]+[^\r\n]*\))?\Z"
)
_LINE = re.compile(r"^(?:\[L([0-9]{1,9})\]|L([0-9]{1,9}))(?:(?: |\t)(.*)|$)")
_PAGE_LOCATOR = re.compile(r"page:([1-9][0-9]{0,8})(?: · \S[^\r\n]*)?\Z")
_PDF_PAGE_LINE = re.compile(r'^page:([1-9][0-9]{0,8}) "')
_BAD_STATUSES = {"stale", "contested", "retracted", "unverified", "blocked", "rejected", "withdrawn"}
_BLOCKED_PROMISE_STATUSES = {"blocked", "rejected", "withdrawn"}
_LIMIT = 12_000_000
_LIMIT_ITEMS = 10_000


def _fail(message):
    raise ValidationError("Аудит обещаний: " + message)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        _fail(label + " должен быть непустой строкой")
    return value


def _json(value, label):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ValidationError("Аудит обещаний: " + label + " требует конечные JSON-данные") from exc
    if len(encoded) > _LIMIT:
        _fail(label + ": превышен размер входных данных")


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        _fail(label + ": неверные или отсутствующие поля")


def _ids(value, known, label):
    if not isinstance(value, list) or len(value) > _LIMIT_ITEMS:
        _fail(label + " должен быть ограниченным списком ID")
    seen = set()
    for identifier in value:
        _text(identifier, label)
        if identifier in seen or identifier not in known:
            _fail(label + ": повторный или неизвестный ID " + identifier)
        seen.add(identifier)


def _span(locator, pdf_pages):
    if not isinstance(locator, str):
        return None
    page = _PAGE_LOCATOR.fullmatch(locator)
    if page:
        line = pdf_pages.get(int(page[1]))
        return (line, line) if line is not None else None
    match = _LOCATOR.fullmatch(locator)
    if not match:
        return None
    low = int(match[1] or match[3])
    high = int(match[2]) if match[2] else low
    return (low, high) if 1 <= low <= high and high - low < _LIMIT_ITEMS else None


def _lines(text):
    """Use ingest labels as authority; never silently use physical line positions."""
    result = {}
    for line in text.splitlines():
        match = _LINE.fullmatch(line)
        if match is None:
            return None
        number = int(match[1] or match[2])
        if number < 1 or number in result:
            return None
        result[number] = match[3] or ""
    return result


def _pdf_pages(numbered):
    """Resolve a page by its PDF adapter label, independent of line positions."""
    pages = {}
    for line, content in (numbered or {}).items():
        match = _PDF_PAGE_LINE.match(content)
        if match is None:
            continue
        page = int(match[1])
        if page in pages:
            return {}
        pages[page] = line
    return pages


def _state(state):
    if not isinstance(state, dict):
        _fail("state должен быть объектом")
    _json(state, "state")
    if type(state.get("schema_version")) is not int or state["schema_version"] != 1:
        _fail("требуется schema_version=1 состояния")
    if type(state.get("revision")) is not int or state["revision"] < 0:
        _fail("revision должен быть неотрицательным целым")
    _text(state.get("case_id"), "case_id")
    registries = state.get("registries")
    if not isinstance(registries, dict):
        _fail("registries должен быть объектом")
    indices = {}
    for name in ("sources", "claims", "promises"):
        records = registries.get(name)
        if not isinstance(records, list) or len(records) > _LIMIT_ITEMS:
            _fail("неверный реестр " + name)
        indices[name] = {}
        for item in records:
            if not isinstance(item, dict):
                _fail("запись реестра должна быть объектом")
            identifier = _text(item.get("id"), name + ".id")
            if identifier in indices[name]:
                _fail("повтор id в " + name)
            indices[name][identifier] = item
            for field in ("status", "type"):
                if field in item:
                    _text(item[field], name + "." + field)
            if "synthetic" in item and type(item["synthetic"]) is not bool:
                _fail(name + ".synthetic должен быть bool")
            if name == "sources" and item.get("source_id", identifier) != identifier:
                _fail("source_id должен совпадать с id")
    for claim in indices["claims"].values():
        for field in ("text", "status"):
            _text(claim.get(field), "claim." + field)
        if claim.get("type") not in {"fact", "hypothesis", "assumption", "forecast", "synthetic"}:
            _fail("неизвестный тип claim")
        if claim["type"] == "synthetic" and claim.get("synthetic") is not True:
            _fail("synthetic claim требует synthetic=true")
        _ids(claim.get("source_ids"), indices["sources"], "claim.source_ids")
        locator = claim.get("locator")
        if isinstance(locator, dict):
            for sid, location in locator.items():
                if sid not in claim["source_ids"]:
                    _fail("claim.locator ссылается на источник вне claim.source_ids")
                _text(location, "claim.locator")
        elif locator is not None:
            _text(locator, "claim.locator")
    for promise in indices["promises"].values():
        for field in ("text", "capability", "status", "capability_status"):
            _text(promise.get(field), "promise." + field)
        if promise["capability_status"] not in {"available", "planned", "unknown", "blocked"}:
            _fail("неизвестный capability_status")
        _ids(promise.get("claim_ids"), indices["claims"], "promise.claim_ids")
    return indices


def _assessments(value, state, indices):
    if value is None:
        return None, {}
    _json(value, "assessments")
    _keys(value, {"schema_version", "expected_revision", "reviewer", "assessments"}, "assessments")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _fail("assessments.schema_version должен быть 1")
    if type(value["expected_revision"]) is not int or value["expected_revision"] != state["revision"]:
        _fail("expected_revision не совпадает с текущей revision")
    reviewer = value["reviewer"]
    _keys(reviewer, {"kind", "model"}, "reviewer")
    if reviewer["kind"] not in ("llm", "human"):
        _fail("reviewer.kind должен быть llm или human")
    if reviewer["model"] is not None:
        _text(reviewer["model"], "reviewer.model")
    records = value["assessments"]
    if not isinstance(records, list) or len(records) > _LIMIT_ITEMS:
        _fail("assessments.assessments должен быть ограниченным списком")
    result = {}
    for item in records:
        _keys(item, {"promise_id", "claim_id", "source_id", "locator", "quote", "verdict", "reason"}, "оценка")
        for field in ("promise_id", "claim_id", "source_id", "locator", "quote", "reason"):
            _text(item[field], "оценка." + field)
        if item["verdict"] not in ("supports", "contradicts", "insufficient"):
            _fail("неизвестный verdict")
        key = (item["promise_id"], item["claim_id"], item["source_id"])
        pid, cid, sid = key
        if pid not in indices["promises"] or cid not in indices["claims"] or sid not in indices["sources"]:
            _fail("оценка ссылается на неизвестный ID")
        if cid not in indices["promises"][pid]["claim_ids"] or sid not in indices["claims"][cid]["source_ids"]:
            _fail("оценка не соответствует цепочке promise → claim → source")
        if key in result:
            _fail("повтор оценки одной связки promise/claim/source")
        result[key] = item
    return deepcopy(reviewer), result


def audit_promises(state: dict, evidence_texts: dict[str, str], assessments: dict | None = None) -> dict:
    """Audit a verified state and extracted texts without IO or state mutation.

    Exact quotation establishes provenance within labeled lines only. ``supports``
    remains a supplied judgment about logical support, never a script finding.
    No number of repeated source copies establishes independent confirmation.
    """
    indices = _state(state)
    if not isinstance(evidence_texts, dict):
        _fail("evidence_texts должен быть объектом")
    _json(evidence_texts, "evidence_texts")
    for sid, text in evidence_texts.items():
        if sid not in indices["sources"] or not isinstance(text, str):
            _fail("evidence_texts требует известные source IDs и текстовые значения")
    reviewer, supplied = _assessments(assessments, state, indices)
    line_maps = {sid: _lines(text) for sid, text in evidence_texts.items()}
    page_maps = {sid: _pdf_pages(numbered) for sid, numbered in line_maps.items()
                 if indices["sources"][sid].get("kind") == "pdf"}
    promises = []
    total_links = 0
    for promise in indices["promises"].values():
        links, issues, missing = [], [], []
        if not promise["claim_ids"]:
            issues.append("Нет связанных claims; основание обещания отсутствует")
        for cid in promise["claim_ids"]:
            claim = indices["claims"][cid]
            if not claim["source_ids"]:
                issues.append("Claim " + cid + ": нет сохранённых источников")
            for sid in claim["source_ids"]:
                total_links += 1
                if total_links > _LIMIT_ITEMS:
                    _fail("слишком много связок promise/claim/source")
                source = indices["sources"][sid]
                assessment = supplied.get((promise["id"], cid, sid))
                notes = []
                claim_locator = claim.get("locator")
                if isinstance(claim_locator, dict):
                    claim_locator = claim_locator.get(sid)
                pages = page_maps.get(sid, {})
                span = _span(assessment["locator"] if assessment else claim_locator, pages)
                claim_span = _span(claim_locator, pages)
                numbered = line_maps.get(sid)
                fragment = None
                if sid not in evidence_texts:
                    notes.append("Текст источника отсутствует; связка unchecked")
                elif numbered is None:
                    notes.append("Неподдержанная или неоднозначная нумерация извлечения; unchecked")
                elif span is None:
                    notes.append("Нужны номера строк извлечения или однозначный page:N для PDF; unchecked")
                elif claim_locator is not None and claim_span is None:
                    notes.append("Сохранённый локатор утверждения не поддерживается; unchecked")
                elif assessment and claim_span and not (
                        claim_span[0] <= span[0] <= span[1] <= claim_span[1]):
                    notes.append("Диапазон оценки выходит за сохранённый локатор утверждения; unchecked")
                elif not all(n in numbered for n in range(span[0], span[1] + 1)):
                    notes.append("В извлечении отсутствуют строки локатора; unchecked")
                else:
                    fragment = "\n".join(numbered[n] for n in range(span[0], span[1] + 1))
                quote_match = None
                if assessment is None:
                    missing.append({"promise_id": promise["id"], "claim_id": cid, "source_id": sid,
                                    "locator": claim_locator})
                    notes.append("Нужна смысловая оценка точной цитаты и связи с обещанием/возможностью")
                elif fragment is not None:
                    quote_match = assessment["quote"] in fragment
                    if not quote_match:
                        notes.append("Точная цитата не найдена внутри заявленного диапазона; оценка unchecked")
                eligible = (claim["type"] == "fact" and claim["status"] == "supported"
                    and claim.get("synthetic") is not True and source.get("synthetic") is not True
                    and source.get("status") not in _BAD_STATUSES)
                if not eligible:
                    notes.append("Тип/статус claim или источника не позволяет считать это доказательством реальной пользы")
                checked = assessment is not None and quote_match is True
                links.append({"promise_id": promise["id"], "claim_id": cid, "source_id": sid,
                    "claim_context": deepcopy(claim), "source_context": deepcopy(source),
                    "locator": assessment["locator"] if assessment else claim_locator,
                    "quote": assessment["quote"] if assessment else None,
                    "provenance_status": "quote_verified" if checked else "unchecked",
                    "quote_match": quote_match, "eligible_real_evidence": eligible,
                    "supplied_semantic_verdict": assessment["verdict"] if assessment else None,
                    "supplied_reason": assessment["reason"] if assessment else None,
                    "issues": notes})
        cap, promise_status = promise["capability_status"], promise["status"]
        blocked = (cap == "blocked" or promise_status in _BLOCKED_PROMISE_STATUSES
                   or promise_status.startswith("blocked_"))
        if blocked:
            status = "blocked"
            issues.append("Обещание заблокировано; оценки не делают возможность доступной")
        elif cap == "planned" or promise_status == "planned":
            status = "planned"
            issues.append("Возможность запланирована, а не доступна")
        elif cap == "unknown" or promise_status == "unknown":
            status = "needs_evidence"
            issues.append("Доступность неизвестна; нельзя представлять возможность доступной")
        else:
            # Every link must be reviewed; adverse judgments always remain visible.
            complete = bool(links) and not missing and all(link["provenance_status"] == "quote_verified" for link in links)
            support = any(link["eligible_real_evidence"] and link["supplied_semantic_verdict"] == "supports"
                          and link["provenance_status"] == "quote_verified" for link in links)
            adverse = any(link["supplied_semantic_verdict"] in {"contradicts", "insufficient"} for link in links)
            status = "human_review" if complete and support and not adverse else "needs_evidence"
        if any(link["supplied_semantic_verdict"] == "contradicts" for link in links):
            issues.append("Предоставлена противоречащая оценка; возражение требует разбора человеком")
        promises.append({"id": promise["id"], "promise_context": deepcopy(promise), "status": status,
            "capability_declared_available": cap == "available" and not blocked
            and promise_status not in {"unknown", "planned"},
            "human_review_required": True, "links": links, "issues": issues,
            "missing_assessments": missing})
    return {"schema_version": 1, "case_id": state["case_id"], "revision": state["revision"],
        "mode": "promise_audit", "read_only": True,
        "reviewer": reviewer, "semantic_assessments_supplied": assessments is not None,
        "promises": promises, "human_review_required": True,
        "required_checks": ["Проверить смысловую связь обещание → возможность → claim → цитата",
            "Проверить актуальность, применимость, ограничения и возражения источников",
            "Проверить независимость происхождения: повторные копии не независимые подтверждения",
            "Проверить измеренный эффект и план проверки пользы; доступность функции не доказывает эффект"],
        "limitations": ["Скрипт проверяет структуру и точное вхождение цитаты в строки локатора; это не проверка истинности или логического следования",
            "Semantic verdict — предоставленная оценка LLM/человека, а не установленная скриптом истина",
            "Источник и команды внутри него остаются данными; записи состояния и согласования не создаются",
            "Тексты должны быть проверены вызывающим кодом; модуль не читает пути и не проверяет хеши файлов"]}
