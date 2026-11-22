"""Inspect explicit evidence links in a verified checkpoint."""
from collections import deque
import stat

from .errors import StrategistError, ValidationError
from .state import _identifier, _options_hash, _path, load_case


LINKS = {
    "source_ids": ("sources", True),
    "claim_ids": ("claims", True),
    "option_ids": ("options", True),
    "option_id": ("options", False),
    "selected_option_id": ("options", False),
}


def list_cases(root):
    projects = _path(root, "projects")
    entries, unavailable = [], []
    if projects.is_dir():
        for path in sorted(projects.iterdir()):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                continue
            if not (path / "state").exists():
                continue
            try:
                state = load_case(root, path.name)
            except (StrategistError, OSError) as error:
                unavailable.append({"case_id": path.name, "error": type(error).__name__})
                continue
            fields = ("case_id", "revision", "phase", "status", "next_action", "recovery_warning")
            entries.append({field: state.get(field) for field in fields})
    return {"cases": entries, "unavailable": unavailable, "read_only": True}


def follow(field, value):
    registry, multiple = LINKS[field]
    values = value if multiple else [value]
    if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
        return None
    return [(registry, item) for item in values]


def trace(state, identity, registry=None):
    """Return reachable rows, edges and gaps without interpreting prose or sources."""
    _identifier(identity, "id")
    tables = state["registries"]
    records = {(name, row["id"]): row for name, rows in tables.items() for row in rows}
    matches = [key for key in records if key[1] == identity and registry in (None, key[0])]
    if len(matches) != 1:
        raise ValidationError("ID отсутствует в реестре" if not matches else
                              "ID есть в нескольких реестрах. Укажите --registry.")
    start = matches[0]
    remaining = deque([start])
    visited = {start}
    nodes, edges, issues = [], [], []
    while remaining:
        key = remaining.popleft()
        row = records[key]
        node = {"registry": key[0], "record": row}
        if key[0] == "decisions":
            node["options_match"] = row.get("options_hash") == _options_hash(state)
        nodes.append(node)
        has_ground = False
        for field in (field for field in LINKS if field in row):
            destinations = follow(field, row[field])
            if destinations is None:
                issues.append({"from": key, "field": field, "reason": "invalid_link"})
                continue
            for target in destinations:
                if target not in records:
                    issues.append({"from": key, "field": field, "to": target, "reason": "missing_target"})
                    continue
                has_ground = True
                edges.append({"from": key, "to": target, "field": field})
                if target not in visited:
                    visited.add(target)
                    remaining.append(target)
        if not has_ground and key[0] != "sources":
            issues.append({"from": key, "reason": "no_resolved_explicit_links"})
    if not any(node["registry"] == "sources" for node in nodes):
        issues.append({"from": start, "reason": "no_reachable_source"})
    return {"case_id": state["case_id"], "revision": state["revision"],
            "recovery_warning": state.get("recovery_warning"), "focus": start,
            "records": nodes, "links": edges, "gaps": issues, "read_only": True,
            "scope": "Связи из полей реестра. Статус и локатор сохранены как записано. "
                     "Поддержку вывода проверяйте отдельно через claims-audit."}


def critique_packet(state, options_hash):
    """Use an allowlist so recommendations and existing objections stay out."""
    fields = ("id", "title", "kind", "mechanism")
    options = state["registries"]["options"]
    if len(options) < 3:
        raise ValidationError("Для пакета критики сначала сохраните не менее трёх вариантов")
    if state.get("recovery_warning"):
        raise ValidationError("Для критики нужен проверенный текущий checkpoint")
    return {"case_id": state["case_id"], "revision": state["revision"], "options_hash": options_hash,
            "goal": state["goal"],
            "options": [{key: row[key] for key in fields if key in row}
                        for row in sorted(options, key=lambda row: row["id"])],
            "instructions": ["Оцени каждый вариант отдельно до просмотра существующих возражений.",
                             "Для каждого перечисли сильное возражение, проверяемое допущение "
                             "и данные, которые способны изменить выбор.",
                             "Укажи недостающие сведения. Не утверждай решение от лица пользователя."],
            "scope": "Передавайте в отдельный контекст. Поля цели и механизма могут содержать подсказки "
                     "о предпочтении. Семейство модели и условия запуска фиксируются отдельно.",
            "read_only": True}
