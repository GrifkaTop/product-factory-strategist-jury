"""Read-only audit of decision-critical claims against saved extracted text."""

from collections import Counter

from .errors import ValidationError
from .promise_audit import _BAD_STATUSES, _lines, _pdf_pages, _span


def _fail(message):
    raise ValidationError("Аудит выводов: " + message)


def _require_fields(value, fields):
    if not isinstance(value, dict) or set(value) != fields:
        _fail("неверные или отсутствующие поля. Ожидаются " + ", ".join(sorted(fields)))


def _require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        _fail(name + " должен быть непустой строкой")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail(name + " должен содержать корректный UTF-8")


def _request_items(request, revision, claims):
    _require_fields(request, {"schema_version", "expected_revision", "items"})
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        _fail("schema_version должен быть 1")
    if type(request["expected_revision"]) is not int or request["expected_revision"] != revision:
        _fail(f"expected_revision не совпадает с текущей revision {revision}")
    items = request["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 5:
        _fail("items должен содержать от одного до пяти выводов")
    seen_claims = set()
    for item in items:
        _require_fields(item, {"claim_id", "impact", "checks"})
        claim_id = item["claim_id"]
        _require_text(claim_id, "claim_id")
        _require_text(item["impact"], "impact")
        if claim_id not in claims or claim_id in seen_claims:
            _fail("неизвестный или повторный claim_id " + claim_id)
        seen_claims.add(claim_id)
        if not isinstance(item["checks"], list):
            _fail("checks должен быть списком")
        seen_checks = set()
        for check in item["checks"]:
            _require_fields(check, {"source_id", "locator", "quote", "verdict", "reason"})
            for field, value in check.items():
                _require_text(value, field)
            if check["verdict"] not in ("supports", "insufficient", "contradicts"):
                _fail("неизвестный verdict")
            reference = (check["source_id"], check["locator"], check["quote"])
            if reference in seen_checks:
                _fail("повтор проверки одной цитаты " + claim_id)
            seen_checks.add(reference)
    return items


def _audit_check(check, source, saved_locators, extracts, synthetic, claim_eligible):
    source_id = check["source_id"]
    saved_locator = saved_locators.get(source_id)
    quote_match = None
    issues = []
    source_synthetic = bool(source and source.get("synthetic") is True)
    if source is None:
        issues.append("unknown_source")
    if source_id not in saved_locators:
        issues.append("unlinked_source")
    elif source is not None:
        numbered, pages = extracts.get(source_id, (None, {}))
        span = _span(check["locator"], pages)
        saved_span = _span(saved_locator, pages)
        if source_id not in extracts:
            issues.append("extract_missing")
        elif numbered is None:
            issues.append("extract_unnumbered")
        else:
            if not saved_locator:
                issues.append("saved_locator_missing")
            elif saved_span is None:
                issues.append("saved_locator_unresolved")
            elif not all(number in numbered for number in range(saved_span[0], saved_span[1] + 1)):
                issues.append("saved_locator_lines_missing")
            elif span and not saved_span[0] <= span[0] <= span[1] <= saved_span[1]:
                issues.append("outside_saved_locator")

            if span is None:
                issues.append("locator_unresolved")
            else:
                line_numbers = range(span[0], span[1] + 1)
                if all(number in numbered for number in line_numbers):
                    fragment = "\n".join(numbered[number] for number in line_numbers)
                    quote_match = check["quote"] in fragment
                    if not quote_match:
                        issues.append("quote_not_found")
                else:
                    issues.append("locator_lines_missing")

    provenance_verified = quote_match is True and not issues
    source_status = source.get("status") if source else None
    source_blocked = source_status is not None and (
        not isinstance(source_status, str)
        or source_status in _BAD_STATUSES
        or source_status.startswith("blocked_")
    )
    if source_blocked:
        issues.append("source_status_unsettled")
    if check["verdict"] != "supports":
        issues.append("semantic_" + check["verdict"])
    return {
        "source_id": source_id,
        "source_status": source_status,
        "saved_locator": saved_locator,
        "locator": check["locator"],
        "quote": check["quote"],
        "quote_match": quote_match,
        "provenance_status": "quote_verified" if provenance_verified else "unchecked",
        "supplied_semantic_verdict": check["verdict"],
        "supplied_reason": check["reason"],
        "synthetic": synthetic or source_synthetic,
        "eligible_real_evidence": provenance_verified and claim_eligible and not source_blocked,
        "issues": issues,
    }


def _audit_item(item, claim, sources, extracts):
    source_ids = claim.get("source_ids", [])
    if not isinstance(source_ids, list):
        _fail("claim.source_ids должен быть списком")
    for source_id in source_ids:
        _require_text(source_id, "claim.source_ids")
    locator = claim.get("locator")
    saved = {sid: locator.get(sid) if isinstance(locator, dict) else locator for sid in source_ids}
    synthetic = (
        claim.get("type") == "synthetic" or claim.get("synthetic") is True
        or any(sources.get(sid, {}).get("synthetic") is True for sid in saved)
    )
    claim_eligible = claim.get("type") == "fact" and claim.get("status") == "supported" and not synthetic
    checks = [
        _audit_check(check, sources.get(check["source_id"]), saved, extracts, synthetic, claim_eligible)
        for check in item["checks"]
    ]
    reviewed = {check["source_id"] for check in checks}
    missing = [
        {"source_id": sid, "locator": saved_locator}
        for sid, saved_locator in saved.items() if sid not in reviewed
    ]
    issues = []
    if not saved:
        issues.append("no_saved_sources")
    if any(sid not in sources for sid in saved):
        issues.append("unknown_saved_source")
    if synthetic:
        issues.append("synthetic_material")
    if claim.get("type") != "fact":
        issues.append("claim_not_fact")
    if claim.get("status") != "supported":
        issues.append("claim_status_unsettled")
    if any(
        check["provenance_status"] == "quote_verified" and check["supplied_semantic_verdict"] == "contradicts"
        for check in checks
    ):
        status = "contradicted"
    elif checks and not missing and all(check["eligible_real_evidence"] and not check["issues"] for check in checks):
        status = "human_review"
    else:
        status = "needs_evidence"
    return {
        "claim_id": item["claim_id"],
        "claim_text": claim.get("text"),
        "claim_type": claim.get("type"),
        "claim_status": claim.get("status"),
        "impact": item["impact"],
        "status": status,
        "synthetic": synthetic,
        "assessment_remaining": status != "human_review",
        "checks": checks,
        "missing_assessments": missing,
        "issues": issues,
    }


def audit_claims(state: dict, evidence_texts: dict[str, str], request: dict) -> dict:
    """Check citations at one verified revision and show supplied semantic judgments."""
    registries = state["registries"]
    claims = {claim["id"]: claim for claim in registries["claims"]}
    sources = {source["id"]: source for source in registries["sources"]}
    items = _request_items(request, state["revision"], claims)
    if not isinstance(evidence_texts, dict) or any(
        source_id not in sources or not isinstance(text, str) for source_id, text in evidence_texts.items()
    ):
        _fail("evidence_texts требует известные source IDs и текстовые значения")
    requested_sources = {check["source_id"] for item in items for check in item["checks"]}
    extracts = {}
    for source_id in requested_sources & evidence_texts.keys():
        numbered = _lines(evidence_texts[source_id])
        pages = _pdf_pages(numbered) if sources[source_id].get("kind") == "pdf" else {}
        extracts[source_id] = numbered, pages
    results = [_audit_item(item, claims[item["claim_id"]], sources, extracts) for item in items]
    counts = Counter(item["status"] for item in results)
    status = next(status for status in ("contradicted", "needs_evidence", "human_review") if counts[status])
    return {
        "schema_version": 1,
        "mode": "claims_audit",
        "case_id": state["case_id"],
        "revision": state["revision"],
        "read_only": True,
        "ok": status == "human_review",
        "status": status,
        "status_counts": dict(counts),
        "items": results,
        "human_review_required": True,
        "limitations": [
            "quote_match подтверждает точное вхождение цитаты в указанном диапазоне.",
            "supplied_semantic_verdict передаёт предоставленную оценку. Её смысл и полноту проверяет человек.",
            "Synthetic пригоден для учебного разбора. Для оценки реального эффекта нужны реальные данные.",
            "Целостность checkpoint и извлечений проверяет вызывающий код.",
        ],
    }
