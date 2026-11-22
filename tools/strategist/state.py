"""Versioned local case state, using only the Python standard library.

Only a verified ``state/latest.json -> checkpoints/CP-*`` is authoritative.
Checkpoints are never overwritten. Their manifest covers state.json and the
UTF-8 bytes of every artifact. No source path supplied in a payload is read.
The manifest detects corruption, not deliberate tampering by a filesystem owner.

Forward phase transitions are adjacent; returning to an earlier phase is allowed.
Pause/resume have dedicated entry points. A recovered read is diagnostic only:
mutations fail until the latest pointer/checkpoint has been explicitly repaired.
Callers must serialize writes to a case, as required by the project contract.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import uuid4

from .errors import CheckpointCommitError, IntegrityError, RevisionConflict, ValidationError
from .filesystem import io_path, resolved_path


SCHEMA_VERSION = 1
PHASES = ("intake", "research_plan", "evidence", "options", "strategies",
          "validation", "complete")
STATUSES = frozenset({"active", "paused", "awaiting_human", "needs_review", "complete"})
REGISTRIES = ("sources", "claims", "hypotheses", "gaps", "options", "decisions",
              "promises", "experiments")
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_CHECKPOINT = re.compile(r"CP-(\d{8,})-([0-9a-f]{32})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                              | {f"{p}{n}" for p in ("COM", "LPT") for n in (*range(10), "¹", "²", "³")})
_PATCH_FIELDS = frozenset({"goal", "next_action", "phase", "status", "registries", "artifacts"})
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: Any) -> bytes:
    try:
        _json_value(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValidationError(f"Value is not finite UTF-8 JSON: {exc}") from exc


def _json_value(value: Any) -> None:
    # json.dumps alone accepts tuples and non-string dict keys, losing meaning.
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _json_value(item)
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError(f"Unsupported JSON type: {type(value).__name__}")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: Any, name: str) -> dict:
    if type(value) is not dict:
        raise ValidationError(f"{name} must be an object")
    return value


def _text(value: Any, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValidationError(f"{name} must be {'nonempty ' if nonempty else ''}text")
    return value


def _integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValidationError(f"{name} must be a nonnegative integer")
    return value


def _version(value: Any) -> None:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValidationError("Unsupported schema_version; expected 1")


def _identifier(value: Any, name: str) -> str:
    _text(value, name, nonempty=True)
    if len(value) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationError(f"Unsafe {name}")
    return value


def _relative(value: Any) -> str:
    """Require a canonical relative portable path, including on Windows."""
    _text(value, "artifact/path", nonempty=True)
    if "\\" in value or len(value) > 240:
        raise ValidationError("Paths must use forward slashes and be at most 240 characters")
    for part in value.split("/"):
        stem = part.split(".", 1)[0].upper()
        if (not part or part in (".", "..") or part.endswith((" ", "."))
                or stem in _WINDOWS_RESERVED or any(c in '<>:"|?*' for c in part)
                or any(ord(c) < 32 or ord(c) == 127 for c in part)):
            raise ValidationError(f"Unsafe relative path: {value!r}")
    return value


def _artifact(value: Any) -> str:
    relative = _relative(value)
    if not relative.startswith("outputs/"):
        raise ValidationError("Artifacts must be files under outputs/")
    return relative


def _is_link(path: Path) -> bool:
    try:
        info = io_path(path).lstat()
    except FileNotFoundError:
        return False
    # Covers Windows junctions/reparse points as well as POSIX symlinks.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _bounded(anchor: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(anchor)
        resolved_path(path).relative_to(resolved_path(anchor))
    except (ValueError, OSError, RuntimeError) as exc:
        raise ValidationError(f"Path escapes its managed directory: {path}") from exc
    current = anchor
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        if _is_link(current):
            raise ValidationError(f"Symlinks and reparse points are not allowed here: {current}")
    return path


def case_directory(project_root: Path, case_id: str) -> Path:
    """Validate the case ID and return its isolated directory without creating it."""
    if (not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id)
            or case_id.upper() in _WINDOWS_RESERVED):
        raise ValidationError("case_id must be 1–64 ASCII letters/digits/_/-, starting with a letter/digit")
    root = resolved_path(Path(project_root))
    return _bounded(root, root / "projects" / case_id)


def _path(case: Path, *parts: str) -> Path:
    return _bounded(case, case.joinpath(*parts))


def _options_hash(state: dict) -> str:
    return _digest(_json_bytes(state["registries"]["options"]))


def _approved(state: dict) -> bool:
    options = state["registries"]["options"]
    option_ids = {o["id"] for o in options}
    current_hash = _options_hash(state)
    return any(d.get("status") == "approved"
               and d.get("options_hash") == current_hash
               and d.get("selected_option_id") in option_ids
               for d in state["registries"]["decisions"])


def _source_locations(state: dict) -> dict[str, str | None]:
    """Only the two whitelisted inert source locations can be read."""
    locations = {}
    for source in state["registries"]["sources"]:
        for path_key, hash_key, directory in (
            ("snapshot_path", "sha256", "inputs/"),
            ("extracted_path", "extracted_sha256", "extracted/"),
        ):
            relative = _relative(source.get(path_key))
            if not relative.startswith(directory):
                raise ValidationError(f"{path_key} must be under {directory}")
            expected = source.get(hash_key)
            if expected is None and hash_key == "extracted_sha256":
                # The checkpoint manifest will pin extraction bytes even for
                # older callers that only provide the contracted snapshot hash.
                pass
            elif not isinstance(expected, str) or not _SHA256.fullmatch(expected):
                raise ValidationError(f"{hash_key} must be a SHA256 hex digest")
            if relative in locations and locations[relative] != expected:
                raise ValidationError("Conflicting source hashes for the same path")
            locations[relative] = expected
    return locations


def _validate_state(state: dict, case_id: str) -> None:
    _object(state, "state")
    _version(state.get("schema_version"))
    if state.get("case_id") != case_id:
        raise ValidationError("Checkpoint belongs to another case")
    _integer(state.get("revision"), "revision")
    if (state.get("phase") not in PHASES or not isinstance(state.get("status"), str)
            or state["status"] not in STATUSES):
        raise ValidationError("Invalid phase or status")
    for field in ("goal", "next_action"):
        _text(state.get(field), field)
    for field in ("created_at", "updated_at"):
        try:
            stamp = datetime.fromisoformat(state[field])
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("timezone is required")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"{field} must be an ISO8601 timestamp with timezone") from exc
    registries = _object(state.get("registries"), "registries")
    if set(registries) != set(REGISTRIES):
        raise ValidationError("registries must contain exactly the contracted registry names")
    for registry in REGISTRIES:
        items = registries[registry]
        if type(items) is not list:
            raise ValidationError(f"{registry} must be a list")
        ids = set()
        for item in items:
            _object(item, registry + " item")
            item_id = _identifier(item.get("id"), registry + ".id")
            if item_id in ids:
                raise ValidationError(f"Duplicate {registry} id: {item_id}")
            ids.add(item_id)
            if registry == "sources":
                if item.get("source_id") != item_id:
                    raise ValidationError("source_id must equal id")
                for key in ("snapshot_path", "extracted_path"):
                    if key in item:
                        _relative(item[key])
            if registry == "decisions" and item.get("status") == "approved":
                _text(item.get("user_response"), "user_response", nonempty=True)
                _integer(item.get("revision"), "decision.revision")
                if item["revision"] > state["revision"]:
                    raise ValidationError("Decision revision is in the future")
                if not isinstance(item.get("options_hash"), str) or not _SHA256.fullmatch(item["options_hash"]):
                    raise ValidationError("Approved decision needs an options_hash")
                _identifier(item.get("selected_option_id"), "selected_option_id")
    artifacts = _object(state.get("artifacts"), "artifacts")
    for name, content in artifacts.items():
        _artifact(name)
        _text(content, f"artifacts[{name}]")
    # File/prefix and case-insensitive aliases cannot coexist on Windows.
    names = [name.casefold() for name in artifacts]
    if len(set(names)) != len(names):
        raise ValidationError("Artifact paths must also be unique ignoring case")
    for name in names:
        if any(parent in names for parent in _parents(name)):
            raise ValidationError("Artifact file conflicts with an artifact directory")
    _source_locations(state)
    operations = state.get("operations")
    if type(operations) is not list:
        raise ValidationError("operations must be a list")
    op_ids = set()
    for operation in operations:
        _object(operation, "operation")
        operation_id = _identifier(operation.get("operation_id"), "operation_id")
        if operation_id in op_ids:
            raise ValidationError("Duplicate operation_id in checkpoint")
        op_ids.add(operation_id)
        if not isinstance(operation.get("sha256"), str) or not _SHA256.fullmatch(operation["sha256"]):
            raise ValidationError("Invalid operation hash")
        if _integer(operation.get("revision"), "operation.revision") > state["revision"]:
            raise ValidationError("Operation revision is in the future")
    if state["status"] == "paused":
        if (not isinstance(state.get("resume_status"), str)
                or state["resume_status"] not in STATUSES - {"paused"}):
            raise ValidationError("Paused state needs its previous resume_status")
    elif "resume_status" in state:
        raise ValidationError("resume_status is only valid while paused")
    _json_bytes(state)


def _parents(name: str):
    parts = name.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def _read_json(path: Path) -> tuple[dict, bytes]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        raw = io_path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        _object(value, path.name)
        _json_bytes(value)
        return value, raw
    except (OSError, ValueError, UnicodeError, ValidationError, RecursionError) as exc:
        raise IntegrityError(f"Cannot read verified JSON {path.name}: {exc}") from exc


def _verified_checkpoint(case: Path, name: str, pointer: dict | None = None) -> dict:
    if not isinstance(name, str) or not _CHECKPOINT.fullmatch(name):
        raise IntegrityError("Unsafe checkpoint name")
    try:
        checkpoint = _path(case, "state", "checkpoints", name)
        manifest, manifest_bytes = _read_json(_bounded(checkpoint, checkpoint / "manifest.json"))
        if pointer is not None and _digest(manifest_bytes) != pointer.get("manifest_sha256"):
            raise IntegrityError("Manifest hash does not match latest pointer")
        _version(manifest.get("schema_version"))
        if manifest.get("case_id") != case.name:
            raise IntegrityError("Manifest belongs to another case")
        files = _object(manifest.get("files"), "manifest.files")
        if "state.json" not in files:
            raise IntegrityError("Manifest has no state.json")
        for relative, expected_hash in files.items():
            _relative(relative)
            if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
                raise IntegrityError("Invalid file hash in manifest")
            stored = _bounded(checkpoint, checkpoint / relative)
            if _digest(io_path(stored).read_bytes()) != expected_hash:
                raise IntegrityError(f"Checkpoint hash mismatch: {relative}")
        state, _ = _read_json(_bounded(checkpoint, checkpoint / "state.json"))
        _validate_state(state, case.name)
        if state["revision"] != manifest.get("revision") or int(_CHECKPOINT.fullmatch(name)[1]) != state["revision"]:
            raise IntegrityError("Checkpoint revision mismatch")
        if pointer is not None and state["revision"] != pointer.get("revision"):
            raise IntegrityError("Latest pointer revision mismatch")
        source_locations = _source_locations(state)
        expected_files = ({"state.json"} | {"artifacts/" + key for key in state["artifacts"]}
                          | {"sources/" + key for key in source_locations})
        if set(files) != expected_files:
            raise IntegrityError("Manifest and state artifact package disagree")
        for relative, content in state["artifacts"].items():
            _path(case, relative)  # Reject aliased output locations before projection/export.
            artifact = _bounded(checkpoint, checkpoint / "artifacts" / relative)
            if io_path(artifact).read_bytes() != content.encode("utf-8"):
                raise IntegrityError(f"Artifact bytes disagree with state: {relative}")
        for relative, expected in source_locations.items():
            packaged_hash = files["sources/" + relative]
            if expected is not None and expected != packaged_hash:
                raise IntegrityError(f"Registered source hash mismatch: {relative}")
            if _digest(io_path(_path(case, relative)).read_bytes()) != packaged_hash:
                raise IntegrityError(f"Source snapshot/extraction has changed: {relative}")
        return state
    except (OSError, ValidationError, UnicodeError) as exc:
        raise IntegrityError(f"Unsafe or invalid checkpoint {name}: {exc}") from exc


def load_case(project_root: Path, case_id: str) -> dict:
    """Read verified state; fallback reads always carry recovery_warning.

    Neither damaged files nor the latest pointer are repaired by this function.
    Fully written checkpoints are candidates; .pending-* directories are ignored.
    """
    case = case_directory(project_root, case_id)
    if not io_path(case).is_dir():
        raise ValidationError(f"Case does not exist: {case_id}")
    rejected_name = None
    revision_limit = None
    try:
        pointer, _ = _read_json(_path(case, "state", "latest.json"))
        _version(pointer.get("schema_version"))
        if pointer.get("case_id") != case_id:
            raise IntegrityError("Latest pointer belongs to another case")
        _integer(pointer.get("revision"), "latest.revision")
        rejected_name = pointer.get("checkpoint")
        revision_limit = pointer["revision"]
        return _verified_checkpoint(case, pointer.get("checkpoint"), pointer)
    except (IntegrityError, ValidationError) as exc:
        problem = str(exc)
    try:
        checkpoints = _path(case, "state", "checkpoints")
        names = [p.name for p in io_path(checkpoints).iterdir() if _CHECKPOINT.fullmatch(p.name)
                 and p.name != rejected_name
                 and (revision_limit is None or int(_CHECKPOINT.fullmatch(p.name)[1]) <= revision_limit)]
    except (OSError, ValidationError) as exc:
        raise IntegrityError(f"No verified checkpoint available; latest problem: {problem}; {exc}") from exc
    diagnostics = []
    for name in sorted(names, key=lambda n: (int(_CHECKPOINT.fullmatch(n)[1]), n), reverse=True):
        try:
            state = _verified_checkpoint(case, name)
        except IntegrityError as exc:
            diagnostics.append(f"{name}: {exc}")
            continue
        state["recovery_warning"] = (
            f"Latest could not be trusted: {problem}. Recovered verified checkpoint {name} "
            f"(revision {state['revision']}); no files were repaired. "
            + ("Rejected checkpoints: " + "; ".join(diagnostics) if diagnostics else "")
        ).strip()
        # A rollback cannot silently bring an older approval back into force.
        state["status"] = "needs_review"
        state.pop("resume_status", None)
        for decision in state["registries"]["decisions"]:
            if decision.get("status") == "approved":
                decision["status"] = "needs_review"
                decision["invalidation_reason"] = "Recovered projection requires human review"
        return state
    raise IntegrityError(f"No verified checkpoint available; latest problem: {problem}; " + "; ".join(diagnostics))


def _write_file(path: Path, content: bytes) -> None:
    with io_path(path).open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    # Directory fsync is supported on POSIX, but not by normal Windows handles.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _completion_ready(case: Path, state: dict) -> None:
    if state["status"] != "complete":
        return
    if state["phase"] != "complete" or not _approved(state):
        raise ValidationError("Complete requires phase=complete and a current approved human decision")
    # Imported only at the final readiness gate; intake and early work remain
    # possible while the independent validation module is being integrated.
    from .validate import validate_state
    report = validate_state(state, case, completion=True)
    missing = [item["path"] for item in report["errors"] if item["code"] == "completion.output_missing"]
    if missing:
        raise ValidationError("Complete requires nonempty artifacts: " + ", ".join(missing))
    if not report["ok"]:
        details = "; ".join(f"{item['code']} at {item['path']}" for item in report["errors"])
        raise ValidationError("Case is not ready for complete: " + details)


def _commit(case: Path, state: dict) -> dict:
    _validate_state(state, case.name)
    _completion_ready(case, state)
    checkpoint_id = f"CP-{state['revision']:08d}-{uuid4().hex}"
    stage = "prepare_checkpoint"
    try:
        checkpoints = _path(case, "state", "checkpoints")
        io_path(checkpoints).mkdir(parents=True, exist_ok=True)
        staging = _path(case, "state", "checkpoints", ".pending-" + uuid4().hex)
        io_path(staging).mkdir()
        files = {"state.json": _json_bytes(state)}
        for relative in state["artifacts"]:
            _path(case, relative)
        files.update({"artifacts/" + name: content.encode("utf-8")
                      for name, content in state["artifacts"].items()})
        for relative, expected in _source_locations(state).items():
            content = io_path(_path(case, relative)).read_bytes()
            if expected is not None and _digest(content) != expected:
                raise IntegrityError(f"Source hash does not match registered content: {relative}")
            files["sources/" + relative] = content
        for relative, content in files.items():
            target = _bounded(staging, staging / relative)
            io_path(target.parent).mkdir(parents=True, exist_ok=True)
            _write_file(target, content)
        manifest = {"schema_version": SCHEMA_VERSION, "case_id": state["case_id"],
                    "revision": state["revision"], "created_at": state["updated_at"],
                    "files": {name: _digest(content) for name, content in files.items()}}
        manifest_bytes = _json_bytes(manifest)
        _write_file(staging / "manifest.json", manifest_bytes)
        # All files are durable before the complete directory is published.
        for directory, _, _ in os.walk(io_path(staging, recursive=True), topdown=False):
            _sync_directory(Path(directory))
        final = _path(case, "state", "checkpoints", checkpoint_id)
        stage = "publish_checkpoint"
        os.rename(io_path(staging), io_path(final))
        _sync_directory(checkpoints)
        stage = "publish_latest"
        pointer = {"schema_version": SCHEMA_VERSION, "case_id": state["case_id"],
                   "revision": state["revision"], "checkpoint": checkpoint_id,
                   "manifest_sha256": _digest(manifest_bytes)}
        temporary = _path(case, "state", ".latest-" + uuid4().hex + ".tmp")
        _write_file(temporary, _json_bytes(pointer))
        latest = _path(case, "state", "latest.json")
        os.replace(io_path(temporary), io_path(latest))
        stage = "sync_latest"
        _sync_directory(latest.parent)
    except (OSError, ValidationError) as exc:
        raise CheckpointCommitError(case.name, state["revision"], stage, exc) from exc
    return deepcopy(state)


def init_case(project_root: Path, case_id: str, goal: str = "") -> dict:
    """Create revision zero without inheriting anything from other cases."""
    case = case_directory(project_root, case_id)
    _text(goal, "goal")
    if _path(case, "state").exists():
        raise ValidationError(f"Case state already exists: {case_id}")
    timestamp = _now()
    state = {"schema_version": SCHEMA_VERSION, "case_id": case_id, "revision": 0,
             "phase": "intake", "status": "active", "goal": goal, "next_action": "",
             "registries": {name: [] for name in REGISTRIES}, "artifacts": {},
             "operations": [], "created_at": timestamp, "updated_at": timestamp}
    _validate_state(state, case_id)
    return _commit(case, state)


def _writable_state(project_root: Path, case_id: str) -> tuple[Path, dict]:
    state = load_case(project_root, case_id)
    if "recovery_warning" in state:
        raise IntegrityError("Refusing to mutate a recovered projection. Inspect and explicitly repair "
                             "the latest pointer/checkpoint first. " + state["recovery_warning"])
    return case_directory(project_root, case_id), state


def _upsert(state: dict, registries: dict) -> set[str]:
    _object(registries, "payload.registries")
    unknown = set(registries) - set(REGISTRIES)
    if unknown:
        raise ValidationError(f"Unknown registries: {sorted(unknown)}")
    if "decisions" in registries:
        raise ValidationError("patch cannot change decisions; use human_decision")
    changed = set()
    for name, items in registries.items():
        if type(items) is not list:
            raise ValidationError(f"{name} upsert must be a list")
        existing = state["registries"][name]
        positions = {item["id"]: i for i, item in enumerate(existing)}
        seen = set()
        for item in items:
            _object(item, name + " item")
            item_id = _identifier(item.get("id"), name + ".id")
            if item_id in seen:
                raise ValidationError(f"Repeated {name} id in one patch: {item_id}")
            seen.add(item_id)
            item = deepcopy(item)
            if name == "sources":
                item.setdefault("source_id", item_id)
            previous = existing[positions[item_id]] if item_id in positions else None
            if previous is None or _json_bytes(previous) != _json_bytes(item):
                changed.add(name)
                if previous is None:
                    positions[item_id] = len(existing)
                    existing.append(item)
                else:
                    existing[positions[item_id]] = item
    return changed


def _patch(state: dict, payload: dict) -> None:
    unknown = set(payload) - _PATCH_FIELDS
    if unknown:
        raise ValidationError(f"Unknown patch fields: {sorted(unknown)}")
    old_phase = state["phase"]
    new_phase = payload.get("phase", old_phase)
    if new_phase not in PHASES:
        raise ValidationError("Invalid phase")
    if PHASES.index(new_phase) > PHASES.index(old_phase) + 1:
        raise ValidationError("Forward phase transitions must be adjacent")
    if "status" in payload:
        if not isinstance(payload["status"], str) or payload["status"] not in STATUSES:
            raise ValidationError("Invalid status")
        if payload["status"] == "paused":
            raise ValidationError("Use pause_case to pause")
    if payload.get("phase") == "complete" or payload.get("status") == "complete":
        from .validate import COMPLETION_BLOCKED_STATUSES
        if (state["status"] in COMPLETION_BLOCKED_STATUSES
                or payload.get("status") in COMPLETION_BLOCKED_STATUSES):
            raise ValidationError("Resolve the pause, pending decision or recovery before completing the case")
    for key in ("goal", "next_action"):
        if key in payload:
            _text(payload[key], key)
    goal_changed = "goal" in payload and payload["goal"] != state["goal"]
    changed = _upsert(state, payload.get("registries", {}))
    artifacts = _object(payload.get("artifacts", {}), "payload.artifacts")
    for name, content in artifacts.items():
        _artifact(name)
        _text(content, f"artifacts[{name}]")
    state["artifacts"].update(artifacts)
    for key in ("goal", "next_action", "phase", "status"):
        if key in payload:
            state[key] = payload[key]
    invalidated = False
    if goal_changed or changed & {"sources", "claims", "options"}:
        for decision in state["registries"]["decisions"]:
            if decision.get("status") == "approved":
                decision["status"] = "needs_review"
                decision["invalidated_at_revision"] = state["revision"] + 1
                decision["invalidation_reason"] = "goal changed" if goal_changed else ", ".join(sorted(changed & {"sources", "claims", "options"})) + " changed"
                invalidated = True
        if invalidated:
            state["status"] = "needs_review"
    # Editing evidence in an advanced phase is allowed, but re-entering or
    # advancing beyond the choice gate needs a fresh approval.
    if ("phase" in payload and PHASES.index(new_phase) >= PHASES.index("strategies")
            and not _approved(state)):
        raise ValidationError("An up-to-date approved human decision is required for this phase")
    if payload.get("status") == "complete" and (new_phase != "complete" or not _approved(state)):
        raise ValidationError("Complete requires phase=complete and a current approved decision")
    if "phase" in payload and new_phase == "complete" and not invalidated:
        state["status"] = "complete"
    elif old_phase == "complete" and new_phase != "complete" and "status" not in payload and not invalidated:
        state["status"] = "active"
    if (not _approved(state)
            and any(d.get("status") == "needs_review" for d in state["registries"]["decisions"])):
        explicit_rework = ("phase" in payload
                           and PHASES.index(new_phase) < PHASES.index(old_phase)
                           and PHASES.index(new_phase) < PHASES.index("strategies"))
        if not explicit_rework:
            state["status"] = "needs_review"


def _human_decision(state: dict, payload: dict) -> None:
    required = {"id", "selected_option_id", "user_response", "options_hash", "rationale"}
    if set(payload) != required:
        raise ValidationError("human_decision requires exactly id, selected_option_id, user_response, options_hash, rationale")
    decision_id = _identifier(payload["id"], "decision.id")
    selected = _identifier(payload["selected_option_id"], "selected_option_id")
    _text(payload["user_response"], "user_response", nonempty=True)
    _text(payload["rationale"], "rationale")
    if selected not in {item["id"] for item in state["registries"]["options"]}:
        raise ValidationError("Selected option does not exist")
    if payload["options_hash"] != _options_hash(state):
        raise ValidationError("options_hash does not match the current option package")
    decisions = state["registries"]["decisions"]
    if any(item["id"] == decision_id for item in decisions):
        raise ValidationError("Decision id already exists; use a new id to preserve history")
    for item in decisions:
        if item.get("status") == "approved":
            item["status"] = "superseded"
    decision = deepcopy(payload)
    decision.update({"status": "approved", "revision": state["revision"] + 1,
                     "decided_at": _now()})
    decisions.append(decision)
    state["status"] = "active" if state["phase"] != "complete" else "needs_review"


def apply_change(project_root: Path, case_id: str, change: dict) -> dict:
    """Validate and commit a patch or an observed human response exactly once.

    An identical retry returns the current state without a new checkpoint, even
    if expected_revision is now old. Reusing an operation_id for other content
    is an error. A paused case rejects even an otherwise idempotent apply.
    Registry upserts replace the entire object with that ID; omitted IDs remain.
    """
    _object(change, "change")
    if set(change) != {"schema_version", "operation_id", "expected_revision", "kind", "payload"}:
        raise ValidationError("change requires exactly schema_version, operation_id, expected_revision, kind, payload")
    _version(change["schema_version"])
    operation_id = _identifier(change["operation_id"], "operation_id")
    _integer(change["expected_revision"], "expected_revision")
    _object(change["payload"], "payload")
    if change["kind"] not in ("patch", "human_decision"):
        raise ValidationError("kind must be patch or human_decision")
    content_hash = _digest(_json_bytes(change))
    change = deepcopy(change)
    case, state = _writable_state(project_root, case_id)
    if state["status"] == "paused":
        raise ValidationError("Case is paused; use resume_case before modifying it")
    for operation in state["operations"]:
        if operation["operation_id"] == operation_id:
            if operation["sha256"] != content_hash:
                raise ValidationError("operation_id was already used with different content")
            return state
    if state["revision"] != change["expected_revision"]:
        raise RevisionConflict(f"Expected revision {change['expected_revision']}; current revision is {state['revision']}")
    if change["kind"] == "patch":
        _patch(state, change["payload"])
    else:
        _human_decision(state, change["payload"])
    state["revision"] += 1
    state["updated_at"] = _now()
    state["operations"].append({"operation_id": operation_id, "sha256": content_hash,
                                "kind": change["kind"], "revision": state["revision"],
                                "created_at": state["updated_at"]})
    return _commit(case, state)


def _lifecycle(project_root: Path, case_id: str, *, pause: bool) -> dict:
    case, state = _writable_state(project_root, case_id)
    if (state["status"] == "paused") == pause:
        return state
    if pause:
        state["resume_status"] = state["status"]
        state["status"] = "paused"
    else:
        state["status"] = state.pop("resume_status")
    state["revision"] += 1
    state["updated_at"] = _now()
    kind = "pause" if pause else "resume"
    operation = {"operation_id": f"{kind}-{uuid4().hex}", "kind": kind,
                 "revision": state["revision"], "created_at": state["updated_at"]}
    operation["sha256"] = _digest(_json_bytes(operation))
    state["operations"].append(operation)
    return _commit(case, state)


def pause_case(project_root: Path, case_id: str) -> dict:
    """Save a pause; an already paused case is returned without a revision."""
    return _lifecycle(project_root, case_id, pause=True)


def resume_case(project_root: Path, case_id: str) -> dict:
    """Restore the pre-pause status; an unpaused case is left unchanged."""
    return _lifecycle(project_root, case_id, pause=False)
