"""Read-only revision inspection and explicit-link change impact.

Operation prefixes identify eligible history; there are no parent checkpoint
hashes in schema 1. Multiple eligible historical checkpoints are ambiguous and
are rejected, even when their state bytes are identical.
"""
from pathlib import Path

from .errors import IntegrityError, ValidationError
from . import state as storage


def _history(state):
    operations = state["operations"]
    revision = state["revision"]
    if len(operations) != revision or any(type(op.get("revision")) is not int or op["revision"] != i
                                          for i, op in enumerate(operations, 1)):
        raise IntegrityError("Operation history must be contiguous, one operation per revision")
    return operations


def load_revision(project_root: Path, case_id: str, revision: int) -> dict:
    """Load only verified history anchored to a healthy canonical latest.

    A recovery projection is insufficient authority. Future revisions and orphan
    branches are never selected. No checkpoint, pointer or projection is written.
    """
    if type(revision) is not int or revision < 0:
        raise ValidationError("revision must be a nonnegative integer")
    current = storage.load_case(project_root, case_id)
    if "recovery_warning" in current:
        raise IntegrityError("Revision history unavailable until canonical latest is explicitly repaired")
    history = _history(current)
    if revision > current["revision"]:
        raise ValidationError("Requested revision is in the future of canonical state")
    if revision == current["revision"]:
        return current  # The verified pointer unambiguously names this checkpoint.
    case = storage.case_directory(project_root, case_id)
    checkpoints = storage._path(case, "state", "checkpoints")
    candidates = []
    try:
        names = [p.name for p in checkpoints.iterdir()
                 if storage._CHECKPOINT.fullmatch(p.name)
                 and int(storage._CHECKPOINT.fullmatch(p.name)[1]) == revision]
        for name in sorted(names):
            candidate = storage._verified_checkpoint(case, name)
            operations = _history(candidate)
            if (operations == history[:revision]
                    and candidate["created_at"] == current["created_at"]):
                candidates.append(candidate)
    except (OSError, ValidationError) as exc:
        raise IntegrityError(f"Cannot safely inspect revision history: {exc}") from exc
    if len(candidates) != 1:
        raise IntegrityError(f"Revision {revision} has {len(candidates)} eligible checkpoints; missing or ambiguous history")
    return candidates[0]


def _index(state):
    if type(state) is not dict or type(state.get("registries")) is not dict or type(state.get("artifacts")) is not dict:
        raise ValidationError("Comparison requires state objects with registries and artifacts")
    storage._json_bytes(state)
    unknown = set(state["registries"]) - set(storage.REGISTRIES)
    if unknown:
        raise ValidationError("Unknown registry names")
    indices = {}
    for name in storage.REGISTRIES:
        items = state["registries"].get(name, [])
        if type(items) is not list:
            raise ValidationError("Registry must be a list")
        indices[name] = {}
        for item in items:
            if type(item) is not dict:
                raise ValidationError("Registry item must be an object")
            identity = storage._identifier(item.get("id"), "id")
            if identity in indices[name]:
                raise ValidationError("Duplicate registry id")
            indices[name][identity] = item
    for path, content in state["artifacts"].items():
        storage._artifact(path)
        storage._text(content, "artifact")
    return indices


def _diff(before, after):
    return {"added": sorted(after.keys() - before.keys()),
            "removed": sorted(before.keys() - after.keys()),
            "changed": sorted(k for k in before.keys() & after.keys()
                              if storage._json_bytes(before[k]) != storage._json_bytes(after[k]))}


def compare_states(before: dict, after: dict) -> dict:
    """Report structural changes and conservative explicit-link dependents.

    No free text is interpreted, no decision is invalidated, and no business
    truth or semantic completeness is inferred. Missing links remain unknown.
    IDs in affected_ids include changed seeds and transitive linked dependents.
    state_fields reports changed product/workflow fields, excluding bookkeeping.
    """
    left, right = _index(before), _index(after)
    if before.get("case_id") != after.get("case_id"):
        raise ValidationError("Cannot compare different cases")
    changes = {name: _diff(left[name], right[name]) for name in storage.REGISTRIES}
    state_fields = {field: {"before": before.get(field), "after": after.get(field)}
                    for field in ("goal", "phase", "status", "next_action")
                    if before.get(field) != after.get(field)}
    affected = {(name, identity) for name, diff in changes.items()
                for kind in ("added", "removed", "changed") for identity in diff[kind]}
    status_changes = []
    for identity in sorted(left["decisions"].keys() | right["decisions"].keys()):
        old = left["decisions"].get(identity, {}).get("status")
        new = right["decisions"].get(identity, {}).get("status")
        if old != new:
            status_changes.append({"id": identity, "before": old, "after": new})
    links = {"source_ids": ("sources", True), "claim_ids": ("claims", True),
             "option_ids": ("options", True), "option_id": ("options", False),
             "selected_option_id": ("options", False)}
    edges, unresolved, unknown = set(), [], []
    for label, index in (("before", left), ("after", right)):
        for name, items in index.items():
            for identity, item in items.items():
                found = False
                for field, (target, many) in links.items():
                    if field not in item:
                        continue
                    found = True
                    values = item[field] if many else [item[field]]
                    if type(values) is not list or any(not isinstance(v, str) or not v.strip() for v in values):
                        unresolved.append({"state": label, "registry": name, "id": identity, "field": field,
                                           "reason": "invalid link type"})
                        continue
                    for value in values:
                        if value not in index[target]:
                            unresolved.append({"state": label, "registry": name, "id": identity, "field": field,
                                               "target_id": value, "reason": "missing target"})
                        edges.add(((target, value), (name, identity)))
                if not found and name != "sources":
                    unknown.append({"state": label, "registry": name, "id": identity,
                                    "reason": "No supported explicit dependency links; text not interpreted"})
    while True:
        expanded = affected | {dependent for dependency, dependent in edges if dependency in affected}
        if expanded == affected:
            break
        affected = expanded
    return {"state_fields": state_fields,
            "registries": changes, "artifacts": _diff(before["artifacts"], after["artifacts"]),
            "decision_status_changes": status_changes,
            "affected_ids": {name: sorted(identity for registry, identity in affected if registry == name)
                             for name in storage.REGISTRIES},
            "dependencies": {"unresolved": unresolved, "unknown": unknown,
                             "scope": "Explicit source_ids/claim_ids/option links only; not a proven semantic graph"},
            "read_only": True}
