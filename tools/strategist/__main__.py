"""Command line interface; model reasoning is supplied by the user's IDE."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid

from . import __version__
from .errors import CheckpointCommitError, StrategistError


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _json_file(path):
    path = Path(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError("JSON-вход превышает 2 МБ")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Ожидается JSON-объект")
    return value


def _options_hash(state):
    data = json.dumps(state["registries"]["options"], ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _state_view(state):
    """Keep routine tool output small instead of reprinting every strategy."""
    return {"case_id": state["case_id"], "revision": state["revision"],
            "phase": state["phase"], "status": state["status"],
            "goal": state.get("goal", ""), "next_action": state.get("next_action", ""),
            "options_hash": _options_hash(state),
            "state_pointer": f"projects/{state['case_id']}/state/latest.json",
            "registry_counts": {k: len(v) for k, v in state["registries"].items()},
            "recovery_warning": state.get("recovery_warning"), "measured_tokens": None}


def _checkpoint_error_view(root, error):
    from .state import load_case

    result = {"ok": False, "error": "CheckpointCommitError", "message": str(error),
              "case_id": error.case_id, "attempted_revision": error.revision,
              "commit_stage": error.stage, "last_verified": None}
    try:
        state = load_case(root, error.case_id)
    except (StrategistError, OSError):
        result["hint"] = "Подтвердить сохранённую версию не удалось. Проверьте доступ к рабочей папке и вывод checkpoint."
    else:
        recovered = "recovery_warning" in state
        result["last_verified"] = {"revision": state["revision"], "status": state["status"],
                                   "current": not recovered}
        if recovered:
            result["hint"] = "Доступна резервная версия только для чтения. Проверьте вывод checkpoint."
        elif state["revision"] == error.revision:
            result["hint"] = "Текущий указатель уже ведёт на запрошенную ревизию. Проверьте status перед повтором."
        else:
            result["hint"] = ("Текущая версия сохранена. Проверьте checkpoint и при повторном отказе "
                              "продолжите с тем же ID в доступной рабочей копии. "
                              "Для повтора apply сохраните исходный operation_id.")
    return result


def _parser():
    parser = argparse.ArgumentParser(description="Стратег Фабрики продуктов: локальные инструменты без LLM API")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2],
                        help="Корень проекта (по умолчанию папка этого репозитория)")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Проверить локальную среду; доступ модели проверяется отдельно")
    commands.add_parser("cases", help="Найти сохранённые кейсы и последнюю проверенную ревизию")
    commands.add_parser("mcp", help="Запустить локальный MCP stdio для чтения кейсов и методик")
    knowledge = commands.add_parser("kb-search", help="Найти методику и точную строку по теме")
    knowledge.add_argument("query")
    audit = commands.add_parser("audit", help="Посмотреть локальный журнал метаданных без записи")
    audit.add_argument("--case")
    audit.add_argument("--limit", type=int, default=20)
    demo = commands.add_parser("demo", help="Воспроизвести сохранённый учебный кейс; не новое исследование")
    demo.add_argument("--case", default="training-subscriptions")
    diff = commands.add_parser("diff", help="Сравнить проверенные ревизии без изменения кейса")
    diff.add_argument("--case", required=True)
    diff.add_argument("--from-revision", type=int, required=True)
    diff.add_argument("--to-revision", type=int)
    diff.add_argument("--details", action="store_true", help="Включить полный список неизвестных зависимостей")
    diff.add_argument("--handoff", action="store_true", help="Показать состояние решения и условия, которые надо перенести")
    trace = commands.add_parser("trace", help="Проследить сохранённые основания записи до источников")
    trace.add_argument("--case", required=True)
    trace.add_argument("--id", required=True)
    trace.add_argument("--registry")
    critic = commands.add_parser("critique-packet", help="Собрать варианты для отдельного контекста критика")
    critic.add_argument("--case", required=True)
    critique_review = commands.add_parser("critique-review", help="Сверить вклад критика на проверенной ревизии")
    critique_review.add_argument("--case", required=True)
    critique_review.add_argument("--input", type=Path, required=True)
    tables = commands.add_parser("table-profile", help="Оценить структуру сохранённого источника CSV/TSV")
    tables.add_argument("--case", required=True)
    tables.add_argument("--source", required=True)
    tables.add_argument("--header", action="store_true", help="Первая запись содержит заголовки")
    retention = commands.add_parser("retention", help="Разложить изменение удержания в сохранённой таблице")
    retention.add_argument("--case", required=True)
    retention.add_argument("--source", required=True)
    retention.add_argument("--before", required=True)
    retention.add_argument("--after", required=True)
    for name in ("init", "status", "review", "pause", "resume", "checkpoint", "validate", "report", "replay"):
        command = commands.add_parser(name)
        command.add_argument("--case", required=True)
        if name == "init":
            command.add_argument("--goal", default="")
        if name == "report":
            command.add_argument("--html", action="store_true", help="Сформировать автономный HTML-отчёт")
        if name == "validate":
            command.add_argument("--ready", action="store_true", help="Проверить готовность к завершению без изменения checkpoint")
    change = commands.add_parser("apply", help="Применить структурированное изменение с revision и operation_id")
    change.add_argument("--case", required=True)
    change.add_argument("--input", type=Path, required=True)
    ingest = commands.add_parser("ingest", help="Импортировать разрешённый файл; команды из источника не выполняются")
    ingest.add_argument("--case", required=True)
    ingest.add_argument("--file", type=Path, required=True)
    ingest.add_argument("--url", help="Проверенный URL исходной страницы; не загружается автоматически")
    ingest.add_argument("--title")
    ingest.add_argument("--synthetic", action="store_true")
    calculate = commands.add_parser("calculate", help="Воспроизводимая учебная экономика; не бизнес-прогноз")
    calculate.add_argument("--input", type=Path, required=True)
    pilot_plan = commands.add_parser("pilot-plan", help="Сверить срок набора, окно наблюдения и правило интервала")
    pilot_plan.add_argument("--input", type=Path, required=True)
    pilot_design = commands.add_parser("pilot-design", help="Рассчитать выборку для минимально полезного бинарного эффекта")
    pilot_design.add_argument("--input", type=Path, required=True)
    feedback = commands.add_parser("operation-feedback", help="Проверить подтверждение для клиента по состояниям операции")
    feedback.add_argument("--input", type=Path, required=True)
    feedback.add_argument("--require-wording-review", action="store_true",
                          help="Требовать оценку текста и следующего действия для текущей версии матрицы")
    revenue = commands.add_parser("revenue-bridge", help="Проверить порог экономики при разных сценариях")
    revenue.add_argument("--input", type=Path, required=True)
    stress = commands.add_parser("stress", help="Проверить устойчивость вариантов к сценариям")
    stress.add_argument("--case", required=True)
    stress.add_argument("--input", type=Path, required=True)
    promises = commands.add_parser("promise-audit", help="Проверить основания обещаний и цитаты")
    promises.add_argument("--case", required=True)
    promises.add_argument("--assessments", type=Path)
    claims = commands.add_parser("claims-audit", help="Проверить цитаты и смысловые оценки решающих выводов")
    claims.add_argument("--case", required=True)
    claims.add_argument("--input", type=Path, required=True)
    measurement = commands.add_parser("measure", help="Свести измерения полного прогона и время команд CLI")
    measurement.add_argument("--input", type=Path, required=True)
    routing = commands.add_parser("model-route", help="Сравнить профили модели на одинаковых сценариях")
    routing.add_argument("--input", type=Path, required=True)
    scoring = commands.add_parser("score-contest", help="Свести оценку по четырём конкурсным критериям")
    scoring.add_argument("--input", type=Path, required=True)
    scoring.add_argument("--baseline", type=Path, help="Вторая карточка по тому же профилю и покрытию")
    outcome = commands.add_parser("outcome", help="Сопоставить импортированные результаты с протоколом")
    outcome.add_argument("--case", required=True)
    outcome.add_argument("--experiment", required=True)
    outcome.add_argument("--source", required=True)
    outcome.add_argument("--save", action="store_true")
    return parser


def _write_artifact(case_dir, relative, text):
    if not isinstance(relative, str) or not isinstance(text, str):
        raise ValueError("Артефакт должен содержать строковый путь и текст")
    if "\\" in relative or ":" in relative:
        raise ValueError("Недопустимый путь артефакта")
    parts = Path(relative).parts
    if len(parts) < 2 or parts[0] != "outputs" or ".." in parts:
        raise ValueError("Артефакты разрешены только под outputs/")
    destination = case_dir / relative
    current = case_dir
    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            linked = stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
        else:
            linked = False
        if linked:
            raise ValueError("Символические ссылки в пути артефакта запрещены")
    if not destination.resolve().is_relative_to(case_dir.resolve()):
        raise ValueError("Путь артефакта выходит за кейс")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")


def _report(case_dir, state):
    written = []
    for path, content in state.get("artifacts", {}).items():
        _write_artifact(case_dir, path, content)
        written.append(path)
    summary = ["# Состояние кейса", "", f"Кейс: {state['case_id']}",
               f"Версия: {state['revision']}", f"Стадия: {state['phase']}",
               f"Статус: {state['status']}", "", f"Цель: {state.get('goal', '')}", "",
               f"Следующий шаг: {state.get('next_action', '')}", "",
               "Это локальный экспорт сохранённого состояния. Он не выполняет новое исследование.", "",
               "## Реестры", ""]
    for name, rows in state.get("registries", {}).items():
        summary.append(f"- {name}: {len(rows)}")
    _write_artifact(case_dir, "outputs/status.md", "\n".join(summary) + "\n")
    return {"case_id": state["case_id"], "revision": state["revision"],
            "written_artifacts": written + ["outputs/status.md"],
            "note": "Производный экспорт. Источник состояния — проверенный checkpoint."}


def _run(args):
    root = args.root.resolve()
    if args.command == "kb-search":
        from .knowledge_search import search_knowledge
        return search_knowledge(root, args.query), 0
    if args.command == "cases":
        from .grounds import list_cases
        return list_cases(root), 0
    if args.command == "doctor":
        from .document_extract import adapter_status
        adapters = adapter_status()
        writable = False
        checkpoint_rename = "not_checked"
        storage_hint = None
        try:
            with tempfile.TemporaryFile(dir=root):
                writable = True
            with tempfile.TemporaryDirectory(prefix=".strategist-check-", dir=root) as probe:
                pending = Path(probe) / ".pending-probe"
                pending.mkdir()
                (pending / "state.json").write_text("{}", encoding="utf-8")
                os.rename(pending, Path(probe) / "checkpoint-probe")
                checkpoint_rename = "ok"
        except OSError as exc:
            checkpoint_rename = "failed" if writable else "not_checked"
            storage_hint = (
                f"Проверка сохранения не прошла ({exc.strerror or type(exc).__name__}). "
                "Откройте проект в доступной для записи локальной папке. "
                "Для существующего кейса сначала проверьте последнюю версию через checkpoint "
                "и продолжите с тем же ID в доступной копии проекта."
            )
        supported = sys.version_info >= (3, 11)
        return {"version": __version__, "python": sys.version.split()[0],
                "python_supported": supported, "root": str(root),
                "root_writable": writable, "model_access": "not_checked",
                "checkpoint_rename": checkpoint_rename, "storage_hint": storage_hint,
                "search_access": "not_checked", "api_required": False,
                "formats": ["md", "txt", "csv", "tsv", "html", "json"] + [kind for kind, item in adapters.items() if item["available"]],
                "optional_adapters": adapters,
                "note": "Диагностика использует временный каталог. Доступность записи может меняться. "
                        "Модель и поиск предоставляет среда."}, 0 if checkpoint_rename == "ok" and supported else 2
    if args.command == "calculate":
        from .calculate import calculate_unit_economics
        return calculate_unit_economics(_json_file(args.input)), 0
    if args.command == "pilot-plan":
        from .pilot_plan import check_pilot_plan
        return check_pilot_plan(_json_file(args.input)), 0
    if args.command == "pilot-design":
        from .pilot_plan import plan_binary_pilot
        return plan_binary_pilot(_json_file(args.input)), 0
    if args.command == "operation-feedback":
        from .operation_feedback import check_feedback
        result = check_feedback(_json_file(args.input), require_wording_review=args.require_wording_review)
        return result, 0 if result["ok"] else 3
    if args.command == "revenue-bridge":
        from .revenue_bridge import compare_revenue
        return compare_revenue(_json_file(args.input)), 0
    if args.command == "score-contest":
        from .contest_scoring import compare_scorecards, score_contest
        card = _json_file(args.input)
        rubric_files = (("contest-aligned-v1", "contest-rubric.json"),
                        ("contest-aligned-v2", "contest-rubric-v2.json"))
        filename = next((name for version, name in rubric_files if version == card.get("rubric_id")), None)
        if filename is None:
            raise ValueError("Неизвестный rubric_id. Выберите contest-aligned-v1 или contest-aligned-v2")
        rubric = _json_file(Path(__file__).resolve().parents[2] / "evaluation" / filename)
        result = score_contest(rubric, card)
        if args.baseline is not None:
            baseline = score_contest(rubric, _json_file(args.baseline))
            result["comparison"] = compare_scorecards(result, baseline)
        return result, 0
    if args.command == "model-route":
        from .model_route import rank_profiles
        return rank_profiles(_json_file(args.input)), 0
    if args.command == "audit":
        from .audit import read_events
        if not 1 <= args.limit <= 200:
            raise ValueError("Лимит журнала должен быть от 1 до 200")
        events = read_events(root, args.case)
        return {"events": events[-args.limit:], "total_events": len(events),
                "note": "Только локальные метаданные. Null — неизвестно, не нулевые затраты."}, 0
    if args.command == "measure":
        from .audit import read_events
        from .run_measurement import summarize_measurement
        data = _json_file(args.input)
        case_id = data.get("case_id")
        has_interval = data.get("started_at") is not None and data.get("result_ready_at") is not None
        events = read_events(root, case_id) if data.get("mode") == "strategist" and isinstance(case_id, str) and has_interval else ()
        return summarize_measurement(data, events), 0
    if args.command == "demo":
        from .training import materialize_training
        result = _state_view(materialize_training(root, args.case))
        result.update({"mode": "REPLAY", "new_model_work": False,
                       "note": "Материализация исследования, написанного агентом 17.11.2026. Выбор человека не создаётся."})
        return result, 0
    if args.command == "diff":
        from .changes import load_revision, compare_states
        before = load_revision(root, args.case, args.from_revision)
        if args.to_revision is None:
            from .state import load_case
            current = load_case(root, args.case)
            after = load_revision(root, args.case, current["revision"])
        else:
            after = load_revision(root, args.case, args.to_revision)
        result = compare_states(before, after)
        if args.handoff:
            from .decision_handoff import build_decision_handoff
            result["handoff"] = build_decision_handoff(before, after, result)
        if not args.details:
            result["registries"] = {name: value for name, value in result["registries"].items() if any(value.values())}
            result["affected_ids"] = {name: value for name, value in result["affected_ids"].items() if value}
            dependencies = result["dependencies"]
            result["dependencies"] = {"unknown_count": len(dependencies["unknown"]),
                                      "unresolved_count": len(dependencies["unresolved"]),
                                      "scope": dependencies["scope"]}
        result.update({"from_revision": before["revision"], "to_revision": after["revision"]})
        return result, 0

    from .state import apply_change, case_directory, init_case, load_case, pause_case, resume_case
    if args.command == "init":
        state = init_case(root, args.case, args.goal)
        return _state_view(state), 0
    if args.command == "pause":
        return _state_view(pause_case(root, args.case)), 0
    if args.command == "resume":
        return _state_view(resume_case(root, args.case)), 0
    if args.command == "apply":
        return _state_view(apply_change(root, args.case, _json_file(args.input))), 0
    state = load_case(root, args.case)
    case_dir = case_directory(root, args.case)
    if args.command == "status":
        return _state_view(state), 0
    if args.command == "review":
        from .review import build_review
        return build_review(state), 0
    if args.command == "trace":
        from .grounds import trace
        return trace(state, args.id, args.registry), 0
    if args.command == "critique-packet":
        from .grounds import critique_packet
        return critique_packet(state, _options_hash(state)), 0
    if args.command == "critique-review":
        from .critique_review import compare_critique
        return compare_critique(state, _json_file(args.input)), 0
    if args.command == "table-profile":
        from .table_profile import profile_table
        return profile_table(case_dir, state, args.source, header=args.header), 0
    if args.command == "retention":
        from .retention import compare_retention
        return compare_retention(case_dir, state, args.source, args.before, args.after), 0
    if args.command == "stress":
        from .stress import stress_test
        return stress_test(state, _json_file(args.input)), 0
    if args.command == "promise-audit":
        from .feature_io import promise_texts
        from .promise_audit import audit_promises
        assessments = _json_file(args.assessments) if args.assessments else None
        return audit_promises(state, promise_texts(case_dir, state), assessments), 0
    if args.command == "claims-audit":
        from .claims_audit import audit_claims
        from .errors import ValidationError
        from .feature_io import claim_texts
        if "recovery_warning" in state:
            raise ValidationError("Для аудита текущих выводов нужен проверенный текущий checkpoint")
        data = _json_file(args.input)
        items = data.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 5:
            raise ValueError("Выберите от одного до пяти решающих утверждений")
        claim_ids = {item["claim_id"] for item in items
                     if isinstance(item, dict) and isinstance(item.get("claim_id"), str)}
        return audit_claims(state, claim_texts(case_dir, state, claim_ids), data), 0
    if args.command == "outcome":
        from .feature_io import assess_outcome, save_outcome
        result = assess_outcome(root, case_dir, state, args.experiment, args.source)
        if args.save:
            result.update(save_outcome(root, state, result), read_only=False)
        return result, 0
    if args.command == "ingest":
        from .ingest import prepare_source
        if state["status"] == "paused" or state.get("recovery_warning"):
            raise ValueError("Импорт запрещён при паузе/непринятом восстановлении. Сначала явный resume.")
        metadata = {"synthetic": args.synthetic}
        if args.url:
            metadata["url"] = args.url
        if args.title:
            metadata["title"] = args.title
        prepared = prepare_source(args.file, case_dir, metadata)
        source = prepared["source"]
        if args.synthetic:
            source["synthetic"] = True
        existing = next((s for s in state["registries"]["sources"] if s.get("id") == source["id"]), None)
        if existing:
            source["retrieved_at"] = existing.get("retrieved_at", source["retrieved_at"])
            if not args.title:
                source["title"] = existing.get("title", source["title"])
            if not args.url and existing.get("url"):
                source["url"] = existing["url"]
            source["synthetic"] = bool(args.synthetic or existing.get("synthetic", False))
            if source == existing:
                return {"source": existing, "unchanged": True, "revision": state["revision"],
                        "warnings": prepared.get("warnings", [])}, 0
        changed = apply_change(root, args.case, {"schema_version": 1,
                "operation_id": "import-" + uuid.uuid4().hex,
                "expected_revision": state["revision"], "kind": "patch",
                "payload": {"registries": {"sources": [source]}}})
        return {"source": source, "revision": changed["revision"],
                "warnings": prepared.get("warnings", []),
                "tables": len(prepared.get("tables", []))}, 0
    if args.command == "validate":
        from .validate import validate_state
        result = validate_state(state, case_dir, completion=args.ready)
        return result, 0 if result["ok"] else 3
    if args.command == "checkpoint":
        if "recovery_warning" in state:
            return {"case_id": args.case, "revision": state["revision"],
                    "verified": False, "recovered_verified": True,
                    "mutations_blocked": True, "recovery_warning": state["recovery_warning"]}, 3
        return {"case_id": args.case, "revision": state["revision"],
                "verified": True, "note": "Каждый успешный apply уже сохраняет checkpoint; новый без изменений не создаётся."}, 0
    if args.command == "report":
        result = _report(case_dir, state)
        if args.html:
            from .audit import read_events
            from .html_report import render_html
            from .review import build_review
            from .validate import validate_state
            rendered = render_html(state, validate_state(state, case_dir), review=build_review(state))
            events = read_events(root, args.case)
            rows = []
            for event in events[-30:]:
                cells = [event["action"], event["status"], event.get("elapsed_seconds"), event.get("measured_tokens")]
                rows.append("<tr>" + "".join("<td>" + escape("неизвестно" if v is None else str(v)) + "</td>" for v in cells) + "</tr>")
            journal = ('<section><h2>Журнал локальных действий</h2>'
                '<p>Снимок до формирования этого отчёта. Это время CLI, не полное время модели или человека. '
                'Модель, reasoning и токены неизвестны, если не измерены средой.</p>'
                '<div class="scroll"><table><thead><tr><th>Действие</th><th>Результат</th><th>Секунды CLI</th><th>Токены</th></tr></thead><tbody>'
                + "".join(rows) + '</tbody></table></div></section>')
            rendered = rendered.replace("<footer>", journal + "<footer>", 1)
            _write_artifact(case_dir, "outputs/report.html", rendered)
            result["written_artifacts"].append("outputs/report.html")
        return result, 0
    if args.command == "replay":
        return {"mode": "REPLAY", "label": "ВОСПРОИЗВЕДЕНИЕ СОХРАНЁННОГО СОСТОЯНИЯ",
                "new_model_work": False, "case_id": args.case, "revision": state["revision"],
                "phase": state["phase"], "status": state["status"],
                "operations": state.get("operations", []),
                "artifact_paths": list(state.get("artifacts", {})),
                "note": "Для нового исследования откройте кейс в Codex с доступной моделью."}, 0
    raise ValueError("Неизвестная команда")


def _record_cli_event(args, started_at, elapsed, succeeded, error_type=None):
    """Metadata only: never persist the source content or the user's response."""
    if (args.command not in {"init", "apply", "ingest", "pause", "resume", "demo", "report", "calculate", "validate"}
            and not (args.command == "outcome" and args.save)):
        return {}
    from .audit import record_event
    from .state import case_directory
    from .errors import StrategistError
    root = args.root.resolve()
    case_id = getattr(args, "case", None)
    try:
        if case_id is not None:
            case_directory(root, case_id)
    except StrategistError:
        case_id = None
    paths = []
    for field in ("input", "file"):
        path = getattr(args, field, None)
        if path:
            try:
                relative = Path(path).resolve().relative_to(root).as_posix()
                if not any(part.startswith(".") or part.casefold() in {"auth.json", "credentials", "secrets"} for part in Path(relative).parts):
                    paths.append(relative)
            except (ValueError, OSError):
                pass
    event = {"event_id": "cli-" + uuid.uuid4().hex, "case_id": case_id,
             "action": args.command, "status": "succeeded" if succeeded else "failed",
             "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
             "elapsed_seconds": elapsed, "input_paths": paths, "output_paths": [],
             "error": error_type, "notes": "Local CLI only. Model, reasoning, tokens and human time not observed."}
    try:
        saved = record_event(root, event)
        return {"audit_event_id": saved["event_id"]}
    except (StrategistError, OSError, ValueError) as error:
        return {"audit_warning": type(error).__name__ + ": journal could not be saved; primary operation result is unchanged"}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    if args.command == "mcp":
        from .mcp_server import serve
        serve(args.root.resolve(), sys.stdin.buffer, sys.stdout)
        return 0
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result, code = _run(args)
    except (ValueError, OSError) as error:
        result = {"ok": False, "error": type(error).__name__, "message": str(error)}
        result.update(_record_cli_event(args, started_at, round(time.monotonic()-started, 4), False, type(error).__name__))
        _print(result)
        return 2
    except Exception as error:
        if not isinstance(error, StrategistError):
            raise
        if isinstance(error, CheckpointCommitError):
            result = _checkpoint_error_view(args.root.resolve(), error)
        else:
            result = {"ok": False, "error": type(error).__name__, "message": str(error)}
        result.update(_record_cli_event(args, started_at, round(time.monotonic()-started, 4), False, type(error).__name__))
        _print(result)
        return 2
    result["local_elapsed_seconds"] = round(time.monotonic() - started, 4)
    result.update(_record_cli_event(args, started_at, result["local_elapsed_seconds"], code == 0,
                                   None if code == 0 else "ValidationFailed"))
    _print(result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
