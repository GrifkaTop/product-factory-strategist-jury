"""Project the decision record and carry-forward obligations across revisions."""
from .errors import ValidationError
from .pilot_plan import check_pilot_plan
from .review import build_review
from .state import _json_bytes


_TRACKED = {
    "claims": ("text", "type", "status", "source_ids", "locator"),
    "options": ("title", "mechanism", "objection", "selection_condition", "claim_ids"),
    "promises": ("text", "status", "capability", "capability_status", "channel", "operation_feedback", "claim_ids"),
    "experiments": ("status", "metric", "numerator", "denominator", "baseline", "baseline_plan", "timeframe",
                    "guardrails", "decision_rule", "review_condition", "owner", "responsible", "decision_owner",
                    "pilot_plan", "test_protocol", "claim_ids"),
}
_OUTPUTS = ("outputs/decision-memo.md", "outputs/product-strategy.md",
            "outputs/communication-strategy.md", "outputs/validation-plan.md",
            "outputs/executive-summary.md")


def _field_changes(previous, current, fields):
    for key in fields:
        if key not in previous and key not in current:
            continue
        status = ("added" if key not in previous else "removed" if key not in current else
                  "retained" if _json_bytes(previous[key]) == _json_bytes(current[key]) else "changed")
        yield key, {"status": status, "before": previous.get(key), "after": current.get(key)}


def _empty(value):
    return value in (None, [], {}) or isinstance(value, str) and not value.strip()


def _cleared_paths(previous, current, path):
    if not _empty(previous) and _empty(current):
        yield path
    elif isinstance(previous, dict) and isinstance(current, dict):
        for key, value in previous.items():
            nested = path + "." + key
            if key not in current:
                yield nested
            else:
                yield from _cleared_paths(value, current[key], nested)


def _pilot_summary(record):
    if "pilot_plan" not in record:
        return None
    try:
        plan = check_pilot_plan(record["pilot_plan"])
    except ValidationError as error:
        return {"error": str(error)}
    target = record["pilot_plan"].get("target_total")
    return {"required": target, "capacity": plan["projected_eligible"],
            "headroom": plan["sample_headroom"],
            "calendar_fits": plan["calendar_fits"], "decision_slack_days": plan["decision_slack_days"]}


def build_decision_handoff(before, after, difference):
    """Retain the decision-relevant parts even when the text of the report changes."""
    reviews = [build_review(checkpoint) for checkpoint in (before, after)]
    affected = difference["affected_ids"]
    records, missing = [], []
    for registry, tracked in _TRACKED.items():
        previous, current = [{row["id"]: row for row in checkpoint["registries"][registry]}
                             for checkpoint in (before, after)]
        impacted = set(affected[registry])
        identities = previous.keys() | current.keys() if registry in {"promises", "experiments"} else impacted
        for identity in sorted(identities):
            left, right = previous.get(identity, {}), current.get(identity, {})
            fields = dict(_field_changes(left, right, tracked))
            for field, data in fields.items():
                if data["status"] == "removed":
                    lost = [field]
                elif data["status"] == "changed":
                    lost = _cleared_paths(data["before"], data["after"], field)
                else:
                    continue
                missing.extend({"registry": registry, "id": identity, "field": path} for path in lost)
            row = {"registry": registry, "id": identity, "affected_by_saved_links": identity in impacted,
                   "fields": fields}
            if registry == "experiments" and ("pilot_plan" in left or "pilot_plan" in right):
                row["pilot_capacity"] = {"before": _pilot_summary(left), "after": _pilot_summary(right)}
            records.append(row)
    revised_outputs = set(difference["artifacts"]["changed"]) | set(difference["artifacts"]["added"])
    decision_changed = ("goal" in difference["state_fields"] or any(
        affected[name] for name in ("sources", "claims", "options", "decisions", "promises", "experiments")))
    untouched_outputs = [path for path in _OUTPUTS if decision_changed and
                         path in after["artifacts"] and path not in revised_outputs]
    sources_added = set(difference["registries"]["sources"]["added"])
    return {"case_id": after["case_id"], "from_revision": before["revision"], "to_revision": after["revision"],
            "approval_before": reviews[0]["approval"], "approval_after": reviews[1]["approval"],
            "to_status": after["status"], "next_action": after.get("next_action"),
            "unlinked_new_sources": sorted(sources_added & set(reviews[1]["unlinked_source_ids"])),
            "records": records, "removed_or_cleared_fields": missing, "unchanged_outputs_to_review": untouched_outputs,
            "read_only": True,
            "scope": "Состояние и поля взяты из проверенных версий. Связи явные. "
                     "Открытые источники, пропавшие поля и прежние документы требуют смысловой сверки. "
                     "Пакет не утверждает пересмотр и не разрешает действие от лица руководителя."}
