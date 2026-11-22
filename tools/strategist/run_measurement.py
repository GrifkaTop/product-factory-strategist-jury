"""Summarize a single observed run from metadata without reading or writing files."""

from datetime import datetime
import math
import re

from .errors import ValidationError
from .token_usage import summarize_token_usage


_OBSERVATIONS = {
    "decision_wait_seconds": False,
    "human_rework_minutes": False,
    "human_interventions": True,
    "measured_tokens": True,
    "direct_cost_rub": False,
}
_REQUIRED = {"schema_version", "mode", "started_at", "result_ready_at", *_OBSERVATIONS}
_IDENTIFIERS = ("case_id", "run_id")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_LIMIT_FIELDS = {"wall_seconds": False, "measured_tokens": True, "direct_cost_rub": False,
                 "human_rework_minutes": False}


def _stamp(value, field):
    if value is None:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is not None:
            return stamp
    except (TypeError, ValueError):
        pass
    raise ValidationError(f"{field} must be an ISO 8601 timestamp with timezone, or null")


def _observation(value, field, *, integer=False):
    if value is None:
        return None
    types = (int,) if integer else (int, float)
    try:
        if type(value) in types and value >= 0 and math.isfinite(value):
            return value
    except OverflowError:
        pass
    kind = "integer" if integer else "number"
    raise ValidationError(f"{field} must be a finite nonnegative {kind}, or null")


def _cli_summary(mode, case_id, start, end, events):
    """Count completed journaled CLI executions inside both inclusive endpoints."""
    metrics = {
        "cli_recorded_seconds_sum": None,
        "cli_event_count": None,
        "cli_timed_event_count": None,
        "cli_failed_event_count": None,
    }
    if mode != "strategist" or case_id is None or start is None or end is None:
        return metrics

    seen = {}
    selected = []
    for event in events:
        if type(event) is not dict:
            raise ValidationError("Expected audited event metadata")
        event_id = event.get("event_id")
        if (event.get("case_id") != case_id or not isinstance(event_id, str)
                or not event_id.startswith("cli-")):
            continue
        event_start = _stamp(event.get("started_at"), "event.started_at")
        event_end = _stamp(event.get("finished_at"), "event.finished_at")
        if event_start is None or event_end is not None and event_end < event_start:
            raise ValidationError("Invalid CLI event interval")
        status = event.get("status")
        if status not in ("started", "succeeded", "failed"):
            raise ValidationError("Invalid CLI event status")
        elapsed = _observation(event.get("elapsed_seconds"), "event.elapsed_seconds")
        metadata = (event_start, event_end, status, elapsed)
        if event_id in seen:
            if seen[event_id] != metadata:
                raise ValidationError("Conflicting timing metadata for the same CLI event_id")
            continue
        seen[event_id] = metadata
        if (status in ("succeeded", "failed") and event_end is not None
                and start <= event_start <= event_end <= end):
            selected.append((status, elapsed))

    measured = [elapsed for _, elapsed in selected if elapsed is not None]
    try:
        metrics["cli_recorded_seconds_sum"] = math.fsum(measured) if measured else None
    except OverflowError as exc:
        raise ValidationError("Sum of recorded CLI durations is out of range") from exc
    metrics["cli_event_count"] = len(selected)
    metrics["cli_timed_event_count"] = len(measured)
    metrics["cli_failed_event_count"] = sum(status == "failed" for status, _ in selected)
    return metrics


def _budget(limits, values):
    if type(limits) is not dict or not limits or set(limits) - set(_LIMIT_FIELDS):
        raise ValidationError("budget_limits должен содержать один или несколько известных показателей")
    checks = {}
    for field, value in limits.items():
        limit = _observation(value, field, integer=_LIMIT_FIELDS[field])
        if limit is None or limit <= 0:
            raise ValidationError(f"Лимит {field} должен быть больше нуля")
        used = values[field]
        if used is None:
            status = "unknown"
        elif used >= limit:
            status = "reached"
        elif used / limit >= 0.8:
            status = "warning"
        else:
            status = "within"
        checks[field] = {"used": used, "limit": limit, "status": status}
    statuses = {item["status"] for item in checks.values()}
    return {"checks": checks,
            "next_action": ("checkpoint_and_confirm" if "reached" in statuses else
                            "check_missing_usage" if "unknown" in statuses else
                            "warn_and_review_remaining_work" if "warning" in statuses else "continue")}


def summarize_measurement(data, events=()):
    """Return JSON-ready measurements for Strategist or a plain-chat comparison.

    Use null for unobserved measurements. Supply events from the local audit
    reader when available. The CLI sum includes every matching measured
    execution even when executions overlap. Counts describe the supplied
    records only. Manual observations are reported independently.
    """
    if type(data) is not dict or set(data) - (_REQUIRED | set(_IDENTIFIERS) | {"budget_limits", "observed_at", "token_usage"}):
        raise ValidationError("Unsupported run measurement fields")
    missing = _REQUIRED - set(data)
    if missing:
        raise ValidationError("Missing run measurement fields: " + ", ".join(sorted(missing)))
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValidationError("Run measurement schema_version must be 1")
    mode = data["mode"]
    if mode not in ("strategist", "plain_chat"):
        raise ValidationError("mode must be strategist or plain_chat")

    identifiers = {}
    for field in _IDENTIFIERS:
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)):
            raise ValidationError(f"{field} must be an ASCII identifier of at most 64 characters, or null")
        identifiers[field] = value

    start = _stamp(data["started_at"], "started_at")
    end = _stamp(data["result_ready_at"], "result_ready_at")
    wall = (end - start).total_seconds() if start is not None and end is not None else None
    if wall is not None and wall < 0:
        raise ValidationError("result_ready_at precedes started_at")
    observations = {
        field: _observation(data[field], field, integer=integer)
        for field, integer in _OBSERVATIONS.items()
    }
    usage = summarize_token_usage(data["token_usage"]) if "token_usage" in data else None
    if usage is not None and usage["basis"] == "api_estimate" and usage["tokens"] is not None:
        if observations["measured_tokens"] not in (None, usage["tokens"]):
            raise ValidationError("measured_tokens расходится с полным набором token_usage")
        observations["measured_tokens"] = usage["tokens"]
    result = {
        "schema_version": 1,
        "mode": mode,
        **identifiers,
        "started_at": start.isoformat() if start is not None else None,
        "result_ready_at": end.isoformat() if end is not None else None,
        "wall_seconds": wall,
        **observations,
        **_cli_summary(mode, identifiers["case_id"], start, end, events),
    }
    if usage is not None:
        result["token_usage"] = usage
    if "observed_at" in data and "budget_limits" not in data:
        raise ValidationError("observed_at используется вместе с budget_limits")
    if "budget_limits" in data:
        observed = _stamp(data.get("observed_at"), "observed_at")
        if observed is not None and (end is not None or start is None or observed < start):
            raise ValidationError("observed_at используется в открытом прогоне и должен быть после начала")
        limit_wall = (observed - start).total_seconds() if observed is not None else wall
        result["budget"] = _budget(data["budget_limits"], {**observations, "wall_seconds": limit_wall})
        result["budget"]["observed_at"] = observed.isoformat() if observed is not None else None
    return result
