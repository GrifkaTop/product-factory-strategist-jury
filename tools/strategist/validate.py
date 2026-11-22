"""Read-only structural checks. Passing them does not establish semantic truth."""

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

from .errors import ValidationError
from .operation_feedback import check_feedback
from .pilot_plan import check_pilot_plan

_REGISTRIES = ("sources", "claims", "hypotheses", "gaps", "options", "decisions", "promises", "experiments")
_PHASES = {"intake", "research_plan", "evidence", "options", "strategies", "validation", "complete"}
_STATUSES = {"active", "paused", "awaiting_human", "needs_review", "complete"}
_CLAIM_TYPES = {"fact", "assumption", "hypothesis", "forecast", "synthetic"}
_CLAIM_STATUSES = {"supported", "contested", "unverified", "stale", "retracted"}
_OPTION_KINDS = {"status_quo", "change", "alternative", "closure"}
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_COMPLETION_OUTPUTS = (
    "brief", "research-plan", "evidence", "options", "decision-memo",
    "product-strategy", "communication-strategy", "validation-plan", "executive-summary",
)
COMPLETION_BLOCKED_STATUSES = ("paused", "awaiting_human", "needs_review")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_relative(value, prefix):
    if not _text(value) or "\\" in value or ":" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute() and not PureWindowsPath(value).is_absolute()
            and len(path.parts) >= 2 and path.parts[0] == prefix
            and all(part not in {"", ".", ".."} and part.rstrip(" .") == part
                    and not any(ord(char) < 32 or char in '<>|?*' for char in part)
                    and part.split(".")[0].upper() not in _WINDOWS_RESERVED
                    for part in value.split("/")))


def validate_state(state: dict, case_dir: Path | None = None, *, completion: bool = False) -> dict:
    """Return diagnostics without changing state or reading paths outside a case.

    Missing readiness fields are warnings until validation/complete. Invalid
    types, dangling IDs, forged/stale approvals and unsafe paths are always
    errors. A source/claim link only establishes structural traceability.
    Completion preflight additionally enforces the final deliverable list.
    """
    errors, warnings = [], []
    full = isinstance(state, dict) and (completion or state.get("phase") in ("validation", "complete") or state.get("status") == "complete")

    def issue(code, path, message, *, readiness=False, warning=False):
        target = warnings if warning or (readiness and not full) else errors
        target.append({"code": code, "path": path, "message": message})

    issue("scope.semantic_truth", "$", "Structural completeness does not prove factual truth, sufficiency of evidence, substantive distinctness of options, or the identity of a human respondent.", warning=True)
    if not isinstance(state, dict):
        issue("state.type", "$", "State must be an object")
        return {"ok": False, "errors": errors, "warnings": warnings, "checks": [{"code": "state", "ok": False}]}
    if type(state.get("schema_version")) is not int or state.get("schema_version") != 1:
        issue("state.schema_version", "schema_version", "schema_version must be integer 1")
    if not _text(state.get("case_id")):
        issue("state.case_id", "case_id", "case_id must be a nonempty string")
    if not _integer(state.get("revision")):
        issue("state.revision", "revision", "revision must be a nonnegative integer")
    phase = state.get("phase")
    if not isinstance(phase, str) or phase not in _PHASES:
        issue("state.phase", "phase", "Unknown phase")
    status = state.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        issue("state.status", "status", "Unknown status")
    if completion:
        if phase not in ("validation", "complete"):
            issue("completion.phase", "phase", "Reach the validation phase before completing the case")
        if status in COMPLETION_BLOCKED_STATUSES or "recovery_warning" in state:
            issue("completion.status", "status", "Resolve the pause, pending decision or recovery before completing the case")
    for field in ("goal", "next_action"):
        if field in state and not isinstance(state[field], str):
            issue("state.field_type", field, f"{field} must be a string")
        elif not _text(state.get(field)):
            issue("state.readiness", field, f"{field} is not filled in", readiness=True)
    for field, expected in (("artifacts", dict), ("operations", list)):
        if field not in state:
            issue("state.readiness", field, f"{field} is missing", readiness=True)
        elif not isinstance(state[field], expected):
            issue("state." + field, field, f"{field} has an invalid type")
    artifacts = state.get("artifacts")
    if isinstance(artifacts, dict):
        for name, content in artifacts.items():
            if not _safe_relative(name, "outputs") or not isinstance(content, str):
                issue("state.artifact", "artifacts", "Artifacts must map safe outputs/ paths to UTF-8 text")
        if completion:
            for name in _COMPLETION_OUTPUTS:
                path = f"outputs/{name}.md"
                if not _text(artifacts.get(path)):
                    issue("completion.output_missing", path, "Required output is missing or empty")

    root = None
    if case_dir is not None:
        if not isinstance(case_dir, Path):
            issue("source.case_dir", "$", "case_dir must be a pathlib.Path")
        else:
            try:
                if any(_link(item) for item in (case_dir.absolute(), *case_dir.absolute().parents)):
                    issue("source.case_dir", "$", "case_dir must not traverse a symlink/junction")
                elif not case_dir.is_dir():
                    issue("source.case_dir", "$", "case_dir is not an existing directory")
                else:
                    root = case_dir.resolve()
            except (OSError, ValueError):
                issue("source.case_dir", "$", "Cannot safely resolve case_dir")

    registries = state.get("registries")
    if not isinstance(registries, dict):
        issue("registry.type", "registries", "registries must be an object")
        registries = {}
    records, indices = {}, {}
    for registry in _REGISTRIES:
        items = registries.get(registry, [])
        if registry not in registries:
            issue("registry.missing", f"registries.{registry}", "Registry is missing", readiness=True)
        if not isinstance(items, list):
            issue("registry.type", f"registries.{registry}", "Registry must be a list")
            items = []
        records[registry], indices[registry] = [], {}
        for index, item in enumerate(items):
            path = f"registries.{registry}[{index}]"
            if not isinstance(item, dict):
                issue("registry.item", path, "Registry item must be an object")
                continue
            records[registry].append((item, path))
            identity = item.get("id")
            if not _text(identity):
                issue("registry.id", path + ".id", "Registry ID must be a nonempty string")
            elif identity in indices[registry]:
                issue("registry.duplicate_id", path + ".id", "Duplicate registry ID")
            else:
                indices[registry][identity] = item

    def required(item, field, path):
        if not _text(item.get(field)):
            if field in item and not isinstance(item[field], str):
                issue("field.type", path + "." + field, f"{field} must be a string")
            else:
                issue("readiness.field", path + "." + field, f"{field} is required for readiness", readiness=True)

    def links(item, field, registry, path, required_links=False):
        value = item.get(field, [])
        if not isinstance(value, list) or any(not _text(v) for v in value):
            issue("link.type", path + "." + field, f"{field} must be a list of nonempty IDs")
            return []
        if len(set(value)) != len(value):
            issue("link.duplicate", path + "." + field, "Duplicate references", warning=True)
        for identity in value:
            if identity not in indices[registry]:
                issue("link.missing", path + "." + field, f"Unknown {registry} ID: {identity}")
        if required_links and not value:
            issue("readiness.evidence", path + "." + field, "Evidence links are required", readiness=True)
        return value

    for registry in _REGISTRIES:
        for item, path in records[registry]:
            if registry != "claims" and "source_ids" in item:
                links(item, "source_ids", "sources", path)
            if registry != "promises" and "claim_ids" in item:
                links(item, "claim_ids", "claims", path)

    for item, path in records["sources"]:
        if item.get("source_id") != item.get("id") or not _text(item.get("source_id")):
            issue("source.id", path + ".source_id", "source_id must equal id")
        for field in ("kind", "title", "original_name"):
            required(item, field, path)
        if "synthetic" in item and not isinstance(item["synthetic"], bool):
            issue("source.synthetic", path + ".synthetic", "synthetic must be a boolean")
        for field in ("sha256", "extracted_sha256"):
            value = item.get(field)
            if value is None:
                issue("source.hash_missing", path + "." + field, "Saved content hash is required", readiness=True)
            elif not isinstance(value, str) or not _HASH.fullmatch(value):
                issue("source.hash", path + "." + field, "Expected lowercase SHA256 hex digest")
        retrieved = item.get("retrieved_at")
        if retrieved is None:
            issue("source.date_missing", path + ".retrieved_at", "Retrieval time is required", readiness=True)
        else:
            try:
                parsed = datetime.fromisoformat(retrieved) if isinstance(retrieved, str) else None
                if parsed is None or parsed.utcoffset() is None:
                    raise ValueError("Missing timezone")
            except (ValueError, OverflowError):
                issue("source.date", path + ".retrieved_at", "Expected ISO8601 retrieval time with timezone")
        limitations = item.get("limitations")
        if limitations is None:
            issue("source.limitations", path + ".limitations", "Document source limitations", readiness=True)
        elif not isinstance(limitations, list) or any(not _text(v) for v in limitations):
            issue("source.limitations", path + ".limitations", "limitations must be a list of nonempty strings")
        for field, prefix, hash_field, size_limit in (
            ("snapshot_path", "inputs", "sha256", 5 * 1024 * 1024),
            ("extracted_path", "extracted", "extracted_sha256", 20 * 1024 * 1024),
        ):
            value = item.get(field)
            if value is None:
                issue("source.path_missing", path + "." + field, "Saved source path is required", readiness=True)
                continue
            if not _safe_relative(value, prefix):
                issue("source.path", path + "." + field, "Unsafe path; expected a safe relative path in " + prefix)
                continue
            if root is None:
                continue
            try:
                target = root / value
                if any(_link(part) for part in (target, *target.parents)) or not target.resolve().is_relative_to(root):
                    issue("source.path", path + "." + field, "Source path traverses a link or escapes the case")
                elif not target.is_file():
                    issue("source.file_missing", path + "." + field, "Saved source file is missing")
                elif target.stat().st_size > size_limit:
                    issue("source.file_size", path + "." + field, "Saved source file exceeds the size limit")
                elif _HASH.fullmatch(str(item.get(hash_field, ""))):
                    with target.open("rb") as stream:
                        data = stream.read(size_limit + 1)
                    if len(data) > size_limit or hashlib.sha256(data).hexdigest() != item[hash_field]:
                        issue("source.integrity", path + "." + field, "Saved file does not match its recorded hash")
            except (OSError, ValueError, RuntimeError):
                issue("source.file_access", path + "." + field, "Cannot safely inspect saved source file")
    if records["sources"] and root is None:
        issue("source.files_unchecked", "registries.sources", "File existence and hashes were not checked without a usable case_dir", warning=True)

    for item, path in records["claims"]:
        for field in ("text", "status"):
            required(item, field, path)
        kind = item.get("type")
        if not isinstance(kind, str) or kind not in _CLAIM_TYPES:
            issue("claim.type", path + ".type", "Unknown claim type")
        if "status" in item and (not isinstance(item["status"], str) or item["status"] not in _CLAIM_STATUSES):
            issue("claim.status", path + ".status", "Unknown claim status")
        if "synthetic" in item and not isinstance(item["synthetic"], bool):
            issue("claim.synthetic", path + ".synthetic", "synthetic must be a boolean")
        if kind == "synthetic" and item.get("synthetic") is not True:
            issue("claim.synthetic", path + ".synthetic", "Synthetic claims require synthetic=true")
        if item.get("synthetic") is True:
            issue("claim.synthetic_evidence", path, "Synthetic material does not establish real demand, business facts, or a human response", warning=True)
        source_ids = links(item, "source_ids", "sources", path, required_links=kind == "fact")
        if any(indices["sources"].get(sid, {}).get("synthetic") is True for sid in source_ids) and item.get("synthetic") is not True:
            issue("claim.synthetic_origin", path, "Claims derived from synthetic sources must retain synthetic=true")
        locator = item.get("locator")
        if kind == "fact" and not (_text(locator) or isinstance(locator, dict) and bool(locator)):
            issue("claim.locator_missing", path + ".locator", "Fact requires a source locator", readiness=True)
        elif locator is not None and not isinstance(locator, (str, dict)):
            issue("claim.locator_type", path + ".locator", "locator must be text or a source-ID to locator mapping")
        elif isinstance(locator, dict):
            if any(not _text(key) or key not in source_ids or not _text(value) for key, value in locator.items()):
                issue("claim.locator", path + ".locator", "Locator map must reference claim sources with nonempty locators")
            if kind == "fact" and any(sid not in locator for sid in source_ids):
                issue("claim.locator_missing", path + ".locator", "Each fact source needs a locator", readiness=True)
        if item.get("status") in ("contested", "unverified", "stale", "retracted"):
            issue("claim.unsettled", path + ".status", "Claim is not current supported evidence", warning=True)

    kinds = set()
    for item, path in records["options"]:
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in _OPTION_KINDS:
            issue("option.kind", path + ".kind", "Unknown option kind")
        else:
            kinds.add(kind)
        for field in ("title", "mechanism", "objection"):
            required(item, field, path)
    if (len(records["options"]) < 3 or not {"status_quo", "closure"}.issubset(kinds)
            or not kinds.intersection({"change", "alternative"})):
        issue("option.coverage", "registries.options", "Need at least three options including status_quo, closure and a change or alternative, with mechanisms and objections; substantive distinctness needs human review", readiness=True)

    try:
        options_hash = hashlib.sha256(json.dumps(registries.get("options", []), ensure_ascii=False,
                                                sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    except (TypeError, ValueError, RecursionError):
        options_hash = None
        issue("decision.options_serialization", "registries.options", "Options must be finite JSON data")
    current_approved = False
    for item, path in records["decisions"]:
        selected = item.get("selected_option_id")
        if selected is not None and (not _text(selected) or selected not in indices["options"]):
            issue("decision.option", path + ".selected_option_id", "Decision references an unknown option")
        if item.get("status") == "approved":
            valid = True
            if not _text(item.get("user_response")):
                issue("decision.human_response", path + ".user_response", "Approved decision requires exact nonempty user response")
                valid = False
            required(item, "rationale", path)
            if not _text(selected) or selected not in indices["options"]:
                issue("decision.option", path + ".selected_option_id", "Approved decision needs a current option")
                valid = False
            if options_hash is None or item.get("options_hash") != options_hash:
                issue("decision.stale", path + ".options_hash", "Approved decision does not match the current options hash")
                valid = False
            revision = item.get("revision")
            if not _integer(revision) or not _integer(state.get("revision")) or revision > state["revision"]:
                issue("decision.revision", path + ".revision", "Approved decision needs a nonnegative revision no later than state")
                valid = False
            current_approved = current_approved or valid
        elif item.get("status") == "needs_review":
            issue("decision.needs_review", path, "Decision requires a new human review", warning=True)
    if not current_approved:
        issue("decision.required", "registries.decisions", "A current approved human decision is required before strategies or completion",
              readiness=phase not in ("strategies", "validation", "complete"))

    for item, path in records["promises"]:
        for field in ("text", "capability", "status"):
            required(item, field, path)
        capability_status = item.get("capability_status")
        if not isinstance(capability_status, str) or capability_status not in {"available", "planned", "unknown"}:
            issue("promise.capability_status", path + ".capability_status", "Expected available, planned or unknown capability status")
        blocked = item.get("status") in ("blocked", "rejected", "withdrawn")
        claim_ids = links(item, "claim_ids", "claims", path, required_links=not blocked)
        if blocked:
            issue("promise.blocked", path, "Promise is blocked and must not be communicated as available", warning=True)
            continue
        if capability_status == "unknown":
            issue("promise.unknown_capability", path, "Unknown capability must be blocked until checked", readiness=True)
        elif capability_status == "planned":
            issue("promise.planned", path, "Planned capability may only be communicated honestly as a plan/pilot; wording requires human review", warning=True)
        supported = any(
            claim.get("type") == "fact" and claim.get("status") == "supported"
            and claim.get("synthetic") is not True and bool(claim.get("source_ids"))
            and bool(claim.get("locator"))
            and all(sid in indices["sources"] and indices["sources"][sid].get("synthetic") is not True
                    for sid in claim.get("source_ids", []) if isinstance(sid, str))
            for claim in (indices["claims"].get(cid, {}) for cid in claim_ids)
            if isinstance(claim.get("source_ids", []), list)
        )
        if capability_status == "available" and not supported:
            issue("promise.evidence", path + ".claim_ids", "Available capability requires current supported, sourced, non-synthetic fact evidence", readiness=True)
        if capability_status == "planned" and item.get("status") == "approved":
            issue("promise.planned_review", path, "Approved planned promise requires semantic review; validation cannot establish whether wording is honest", warning=True)
        if "operation_feedback" in item:
            feedback_path = path + ".operation_feedback"
            try:
                feedback = check_feedback(item["operation_feedback"])
            except ValidationError as error:
                issue("promise.operation_feedback", feedback_path, str(error))
            else:
                if feedback["wording_status"] == "unreviewed" and feedback["structural_ok"]:
                    issue("promise.operation_wording_review", feedback_path,
                          "Customer wording needs review before use", warning=True)
                for finding in feedback["issues"]:
                    issue("promise.operation_feedback", feedback_path + "." + finding["state"],
                          finding["code"], readiness=True)

    for item, path in records["experiments"]:
        for field in ("hypothesis", "expected_change", "metric", "timeframe"):
            required(item, field, path)
        decision_owner = item.get("decision_owner")
        if decision_owner is not None and (not _text(decision_owner) or len(decision_owner) > 160
                                           or any(ord(char) < 32 or ord(char) == 127 for char in decision_owner)):
            issue("experiment.decision_owner", path + ".decision_owner", "decision_owner must be a role of up to 160 characters or null")
        baseline = item.get("baseline")
        if baseline is None:
            if "baseline" not in item:
                issue("experiment.baseline_missing", path + ".baseline", "Set baseline explicitly, using null when unknown", readiness=True)
            required(item, "baseline_plan", path)
        elif isinstance(baseline, bool) or not isinstance(baseline, (str, int, float)):
            issue("experiment.baseline", path + ".baseline", "baseline must be text, a finite number or null")
        elif isinstance(baseline, str) and not baseline.strip():
            issue("experiment.baseline", path + ".baseline", "Empty baseline is unknown; use null and baseline_plan", readiness=True)
        elif isinstance(baseline, (int, float)):
            try:
                if not math.isfinite(baseline):
                    raise ValueError("Non-finite baseline")
            except (ValueError, OverflowError):
                issue("experiment.baseline", path + ".baseline", "Numeric baseline must be finite")
        guardrails = item.get("guardrails")
        if guardrails is not None and (not isinstance(guardrails, (str, list))
                                      or isinstance(guardrails, list) and any(not isinstance(v, str) for v in guardrails)):
            issue("experiment.guardrails", path + ".guardrails", "guardrails must be text or a list of text")
        elif not (_text(guardrails) or isinstance(guardrails, list) and bool(guardrails) and all(_text(v) for v in guardrails)):
            issue("experiment.guardrails", path + ".guardrails", "Define nonempty guardrails as text or a list of text", readiness=True)
        if "pilot_plan" in item:
            try:
                plan = check_pilot_plan(item["pilot_plan"])
            except ValidationError as error:
                issue("experiment.pilot_plan", path + ".pilot_plan", str(error))
            else:
                if not plan["calendar_fits"]:
                    issue("experiment.pilot_calendar", path + ".pilot_plan",
                          f"Recruitment, follow-up and analysis miss the decision by {-plan['decision_slack_days']} days",
                          readiness=True)
                if plan["sample_shortfall"]:
                    issue("experiment.pilot_sample", path + ".pilot_plan",
                          f"Projected recruitment is short by {plan['sample_shortfall']} eligible units",
                          warning=True)
                if "budget" in plan:
                    budget = plan["budget"]
                    if budget["fits"] is False:
                        issue("experiment.pilot_budget", path + ".pilot_plan.budget",
                              f"Known costs exceed the limit by {-budget['headroom_after_known_minor']} minor units",
                              readiness=True)
                    if budget["unknown_items"]:
                        issue("experiment.pilot_budget_unknown", path + ".pilot_plan.budget",
                              "Some pilot costs are unknown", warning=True)

    for registry in ("sources", "claims", "promises", "experiments"):
        if not records[registry]:
            issue("readiness.registry", "registries." + registry, f"No {registry} recorded", readiness=True)
    checks = []
    groups = ("state", "registry", "source", "claim", "link", "option", "decision", "promise", "experiment", "readiness", "field")
    if completion:
        groups += ("completion",)
    for group in groups:
        error_count = sum(item["code"].split(".")[0] == group for item in errors)
        warning_count = sum(item["code"].split(".")[0] == group for item in warnings)
        checks.append({"code": group, "ok": error_count == 0, "errors": error_count, "warnings": warning_count})
    checks.append({"code": "scope", "ok": True, "message": "Read-only structural checks; semantic truth is not established", "full_readiness": full})
    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}
