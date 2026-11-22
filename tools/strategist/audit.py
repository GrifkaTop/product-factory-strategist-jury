"""Local metadata-only audit log, separate from canonical case state.

Callers serialize writers. Never supply prompts, source text, user responses or
credentials in free-text metadata. Unknown usage is null, never estimated.
Corrupt or incomplete JSONL is reported, never silently truncated or repaired.
"""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from uuid import uuid4

from .errors import IntegrityError, ValidationError
from .state import case_directory


_FIELDS = set("event_id case_id action status started_at finished_at elapsed_seconds model reasoning measured_tokens weekly_used_percent input_paths output_paths error human_minutes notes".split())
_UNKNOWN = ("finished_at", "elapsed_seconds", "model", "reasoning", "measured_tokens",
            "weekly_used_percent", "error", "human_minutes")
_BLOCKED = {".git", ".codex", ".agents", ".aws", ".ssh", ".env", "credentials", "auth.json"}
_DEVICES = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in (*range(10), "¹", "²", "³")}


def _no_links(root, path):
    try:
        relative = path.relative_to(root)
        path.resolve().relative_to(root.resolve())
        for item in (root, *(root.joinpath(*relative.parts[:n]) for n in range(1, len(relative.parts) + 1))):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValidationError("Audit paths must not traverse links/reparse points")
    except (ValueError, OSError, RuntimeError) as exc:
        raise ValidationError("Audit path escapes its project root") from exc
    return path


def _location(project_root):
    root = Path(project_root).absolute()
    _no_links(root, root)
    return root, _no_links(root, root / "runs" / "events.jsonl")


def _text(value, field, maximum=2000):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValidationError(f"{field} must be short text (at most {maximum} characters)")
    # Obvious credential blobs are refused; this is not a general secret scanner.
    if re.search(r"(?i)(bearer\s+\S+|sk-[a-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY-----)", value):
        raise ValidationError("Credentials must not be stored in audit metadata")
    return value


def _stamp(value, field):
    _text(value, field, 80)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("timezone missing")
        return parsed
    except ValueError as exc:
        raise ValidationError(f"{field} needs ISO8601 with timezone") from exc


def _relative(root, value):
    _text(value, "path", 240)
    if not value or "\\" in value:
        raise ValidationError("Paths must be portable relative paths")
    for part in value.split("/"):
        if (not part or part in (".", "..") or part.endswith((" ", "."))
                or part.split(".")[0].upper() in _DEVICES
                or part.casefold() in _BLOCKED or part.casefold().startswith(".env.")
                or any(c in '<>:"|?*' or ord(c) < 32 for c in part)):
            raise ValidationError("Unsafe audit metadata path")
    _no_links(root, root / value)
    return value


def _normalize(root, event, *, previous=None, stored=False):
    if type(event) is not dict or set(event) - _FIELDS:
        raise ValidationError("Unsupported audit fields; log metadata only")
    if stored and set(event) != _FIELDS:
        raise ValidationError("Stored audit record is incomplete")
    result = {field: None for field in _UNKNOWN}
    result.update({"case_id": None, "input_paths": [], "output_paths": [], "notes": ""})
    result.update(event)
    for field in ("event_id", "action"):
        _text(result.get(field), field, 160)
        if not result[field].strip() or any(ord(c) < 32 for c in result[field]):
            raise ValidationError(f"{field} must be a nonempty single-line label")
    if result.get("status") not in ("started", "succeeded", "failed"):
        raise ValidationError("Invalid audit status")
    if result["case_id"] is not None:
        case_directory(root, result["case_id"])  # Validation only; no case is created.
    result.setdefault("started_at", previous["started_at"] if previous else datetime.now(timezone.utc).isoformat())
    start = _stamp(result["started_at"], "started_at")
    if result["finished_at"] is not None and _stamp(result["finished_at"], "finished_at") < start:
        raise ValidationError("finished_at precedes started_at")
    for field in ("elapsed_seconds", "measured_tokens", "weekly_used_percent", "human_minutes"):
        number = result[field]
        if number is None:
            continue
        try:
            valid = type(number) in (int, float) and math.isfinite(number) and number >= 0
        except OverflowError:
            valid = False
        if not valid or (field == "measured_tokens" and type(number) is not int):
            raise ValidationError(f"{field} must be finite and nonnegative, or null")
        if field == "weekly_used_percent" and number > 100:
            raise ValidationError("weekly_used_percent must be at most 100")
    for field in ("model", "reasoning", "error", "notes"):
        if result[field] is not None:
            _text(result[field], field)
    for field in ("input_paths", "output_paths"):
        if type(result[field]) is not list or len(result[field]) > 200:
            raise ValidationError(f"{field} must be a list of at most 200 paths")
        result[field] = [_relative(root, p) for p in result[field]]
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValidationError("Audit metadata must be UTF-8 JSON") from exc
    return result


def _read(root, path):
    if not path.exists():
        return []
    try:
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise IntegrityError("Audit log has an incomplete tail; no automatic repair")
        records, seen = [], set()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result
        for index, line in enumerate(data.splitlines(), 1):
            try:
                record = _normalize(root, json.loads(line.decode("utf-8"), object_pairs_hook=unique), stored=True)
                if record["event_id"] in seen:
                    raise ValueError("Duplicate event_id")
            except (ValueError, ValidationError, UnicodeError) as exc:
                raise IntegrityError(f"Invalid audit record at line {index}: {exc}") from exc
            seen.add(record["event_id"])
            records.append(record)
        return records
    except OSError as exc:
        raise IntegrityError(f"Cannot read audit log: {exc}") from exc


def read_events(project_root: Path, case_id: str | None = None) -> list[dict]:
    """Read without writing; validate the entire journal before filtering."""
    root, path = _location(project_root)
    if case_id is not None:
        case_directory(root, case_id)
    records = _read(root, path)
    return records if case_id is None else [r for r in records if r["case_id"] == case_id]


def record_event(project_root: Path, event: dict) -> dict:
    """Append one immutable metadata event; identical event_id retries are no-ops.

    started/succeeded/failed are separate events and need distinct event IDs.
    A same-directory atomic replacement preserves the old log on write failure.
    """
    root, path = _location(project_root)
    if not root.is_dir():
        raise ValidationError("Project root must already exist")
    records = _read(root, path)
    previous = next((r for r in records if type(event) is dict and r["event_id"] == event.get("event_id")), None)
    normalized = _normalize(root, event, previous=previous)
    if previous is not None:
        if previous != normalized:
            raise ValidationError("event_id already used with different content")
        return previous
    records.append(normalized)
    temporary = path.parent / (".events-" + uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(exist_ok=True)
        _no_links(root, temporary)
        with temporary.open("xb") as stream:
            for record in records:
                stream.write((json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        _no_links(root, path)
        os.replace(temporary, path)
    except OSError as exc:
        raise IntegrityError(f"Audit write failed; previous log preserved: {exc}") from exc
    finally:
        if temporary.exists():
            _no_links(root, temporary)
            temporary.unlink()
    return normalized
