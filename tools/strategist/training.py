"""Materialize the disclosed, agent-authored training case; not new LLM work."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re

from .calculate import calculate_unit_economics
from .errors import ValidationError
from .ingest import prepare_source
from .state import apply_change, case_directory, init_case, load_case


GOAL = "[УЧЕБНЫЙ КЕЙС] Выбрать способ проверки помощи в управлении цифровыми подписками; не официальный бриф"
FINAL_OPERATION = "training-seed-options-v1"
SOURCE_CONFIG = (
    ("MTS", "mts-notes.md", "Конспект официальной справки МТС", "https://support.mts.ru/mts_mobilnaya_svyaz/upravlenie-uslugami/kak-upravlyat-uslugami"),
    ("GOOGLE", "google-notes.md", "Конспект официальной справки Google Play", "https://support.google.com/googleplay/answer/7018481?hl=ru"),
    ("YANDEX", "yandex-notes.md", "Конспект официальной справки Яндекс Плюса", "https://yandex.ru/support/plus/ru/manage/unsubscribe.html"),
    ("COSTS", "cost-scenarios.csv", "Полностью искусственные сценарии затрат", None),
)


def _line(text, marker):
    for line in text.splitlines():
        if marker in line:
            match = re.match(r"\[L(\d+)\]", line)
            if match:
                return "line:" + str(int(match[1]))
    raise ValidationError("Training source marker missing: " + marker)


def _change(root, case_id, state, operation_id, payload):
    if any(op["operation_id"] == operation_id for op in state["operations"]):
        return state
    return apply_change(root, case_id, {"schema_version": 1, "operation_id": operation_id,
        "expected_revision": state["revision"], "kind": "patch", "payload": payload})


def materialize_training(project_root: Path, case_id: str = "training-subscriptions") -> dict:
    """Replay stored research through the real state API without inventing approval.

    The source reading and prose were authored in Codex on 17 November 2026.
    This function performs only deterministic import, arithmetic and persistence.
    """
    root = Path(project_root).resolve()
    case = case_directory(root, case_id)
    source_root = Path(__file__).resolve().parents[2] / "examples" / "training-subscriptions"
    if case.exists():
        state = load_case(root, case_id)
        if state["goal"] != GOAL:
            raise ValidationError("Refusing to overwrite an existing case with a different goal")
        if any(op["operation_id"] == FINAL_OPERATION for op in state["operations"]):
            return state
        if state["status"] == "paused" or state.get("recovery_warning"):
            raise ValidationError("Training replay cannot modify a paused/recovered case")
    else:
        state = init_case(root, case_id, GOAL)

    prepared = {}
    for alias, filename, title, url in SOURCE_CONFIG:
        metadata = {"title": title, "synthetic": alias == "COSTS",
                    "author": "Агент: учебная таблица" if alias == "COSTS" else "Агент: конспект проверенной первичной страницы",
                    "redistribution": "Own paraphrase or explicitly synthetic fixture, not a full copy of the source page",
                    "independence_group": alias,
                    "limitations": ["Not demand evidence. Synthetic inputs remain synthetic." if alias == "COSTS" else
                        "Agent-authored notes from a verified public page; the saved hash covers notes, not the original HTML."]}
        if url:
            metadata["url"] = url
        item = prepare_source(source_root / "materials" / filename, case, metadata)
        old = next((s for s in state["registries"]["sources"] if s["id"] == item["source"]["id"]), None)
        if old:
            item["source"] = old
        prepared[alias] = item
    ids = {alias: item["source"]["id"] for alias, item in prepared.items()}
    artifacts = {}
    for path in sorted((source_root / "drafts").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for alias, identity in ids.items():
            text = text.replace("{{SRC_" + alias + "}}", identity)
        artifacts["outputs/" + path.name] = text

    calculations = []
    with (source_root / "materials" / "cost-scenarios.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            inputs = {key: float(row[key]) for key in ("users", "revenue_per_user", "variable_cost_per_user", "fixed_cost")}
            inputs["synthetic"] = True
            calculations.append({"scenario_id": row["scenario_id"], "period": row["period"],
                "currency": row["currency"], "source_id": ids["COSTS"],
                "inputs": inputs, "result": calculate_unit_economics(inputs)})
    artifacts["outputs/calculations.json"] = json.dumps(calculations, ensure_ascii=False, indent=2, allow_nan=False)

    gaps = [
        {"id": "GAP-DEMAND", "text": "Частота, тяжесть задачи и спрос не исследованы", "status": "open"},
        {"id": "GAP-BASELINE", "text": "Исходная успешность прямых справок неизвестна", "status": "open"},
        {"id": "GAP-INTEGRATION", "text": "Интеграционные полномочия и технические возможности неизвестны", "status": "open"},
        {"id": "GAP-ECON", "text": "Настоящие затраты и доходы неизвестны", "status": "open"},
        {"id": "GAP-REGION", "text": "Региональная применимость платформенных процедур не проверена", "status": "open"},
        {"id": "GAP-HUMAN-CHOICE", "text": "Пользователь ещё не выбрал учебный вариант", "status": "open"},
    ]
    state = _change(root, case_id, state, "training-seed-plan-v1", {
        "phase": "research_plan", "next_action": "Сопоставить сохранённые публичные справки и пробелы",
        "registries": {"sources": [item["source"] for item in prepared.values()], "gaps": gaps},
        "artifacts": {key: value for key, value in artifacts.items() if key.endswith(("/brief.md", "/research-plan.md"))}})

    facts = [
        ("F-MTS-1", "MTS", "Справка МТС описывает управление услугами в приложении и личном кабинете"),
        ("F-MTS-2", "MTS", "Для некоторых каналов оформления МТС Premium справка указывает отдельное место отмены"),
        ("F-GOOGLE-1", "GOOGLE", "По справке Google Play удаление приложения пользователем не заменяет отмену"),
        ("F-YANDEX-1", "YANDEX", "Справка Плюса различает способы отмены по каналу оформления"),
        ("F-YANDEX-2", "YANDEX", "В партнёрском разделе справки Плюса есть ссылка на кабинет МегаФона"),
    ]
    claims = [{"id": identity, "text": text, "type": "fact", "status": "supported",
               "source_ids": [ids[alias]], "locator": _line(prepared[alias]["text"], identity + "."),
               "scope": "Описание опубликованной инструкции, не спроса или реальной доступности интеграции"}
              for identity, alias, text in facts]
    claims += [
        {"id": "H-NAV", "text": "Навигатор повышает долю правильно определённых путей управления", "type": "hypothesis", "status": "unverified", "source_ids": []},
        {"id": "H-SEGMENT", "text": "В предполагаемом семейном сегменте наблюдается значимая задача", "type": "hypothesis", "status": "unverified", "source_ids": []},
        {"id": "SYN-ECON", "text": "Учебная арифметика на искусственных входах, не бизнес-прогноз", "type": "synthetic", "status": "supported", "synthetic": True,
         "source_ids": [ids["COSTS"]], "locator": "records:2-5; columns:scenario_id,users,revenue_per_user,variable_cost_per_user,fixed_cost"},
    ]
    state = _change(root, case_id, state, "training-seed-evidence-v1", {
        "phase": "evidence", "next_action": "Сравнить четыре решения и сильные возражения",
        "registries": {"claims": claims, "hypotheses": [dict(c) for c in claims if c["type"] == "hypothesis"]},
        "artifacts": {"outputs/evidence.md": artifacts["outputs/evidence.md"], "outputs/calculations.json": artifacts["outputs/calculations.json"]}})

    options = [
        {"id": "O-STATUS", "kind": "status_quo", "title": "Оставить существующие пути",
         "mechanism": "Без нового интерфейса использовать справки поставщиков и наблюдать сигналы проблемы",
         "objection": "Межсервисный барьер может оставаться незамеченным", "claim_ids": ["F-MTS-1", "F-YANDEX-1"]},
        {"id": "O-PILOT", "kind": "change", "title": "Ограниченный пилот навигатора",
         "mechanism": "Проверить определение места оформления и переход к инструкции без автоматической отмены",
         "objection": "Новый слой может ухудшить уже достаточный путь; спрос не доказан", "claim_ids": ["H-NAV", "F-MTS-2", "F-YANDEX-1"]},
        {"id": "O-PLATFORM", "kind": "alternative", "title": "Отдельный семейный центр",
         "mechanism": "Персональный добровольный список, напоминания и будущие подтверждённые интеграции",
         "objection": "Не доказаны спрос, полномочия, охват и экономика; риски доступа к данным близких", "claim_ids": ["H-SEGMENT", "H-NAV"]},
        {"id": "O-CLOSE", "kind": "closure", "title": "Архивировать направление",
         "mechanism": "Прекратить исследовательские вложения до нового наблюдаемого сигнала",
         "objection": "Можно потерять возможность обнаружить узкую полезную задачу", "claim_ids": ["H-NAV"]},
    ]
    promises = [
        {"id": "P-NAV", "text": "Разберитесь, где управлять подпиской, и перейдите к инструкции поставщика",
         "capability": "Навигатор по ограниченному проверенному набору маршрутов", "capability_status": "planned", "status": "draft", "claim_ids": ["H-NAV", "F-YANDEX-1"]},
        {"id": "P-AUTO", "text": "Автоматически отключим все подписки", "capability": "Универсальная автоматическая отмена", "capability_status": "unknown", "status": "blocked", "claim_ids": []},
        {"id": "P-SAVE", "text": "Гарантируем экономию 30%", "capability": "Измеренная экономия", "capability_status": "unknown", "status": "blocked", "claim_ids": []},
    ]
    experiments = [
        {"id": "EXP-DISCOVERY", "hypothesis": "H-SEGMENT: задача существует в предполагаемом сегменте",
         "expected_change": "Получить наблюдаемые причины затруднения или опровергнуть необходимость нового слоя",
         "baseline": None, "baseline_plan": "Наблюдать текущий путь участников с добровольным согласием",
         "metric": "Качественные эпизоды задачи и причины неуспеха; не оценка доли рынка",
         "timeframe": "D4–D10 после согласования при наличии участников; сессии ещё не проведены",
         "guardrails": ["Не собирать секреты, контакты или чужие данные в repo", "Согласие и обезличенный разрешённый конспект"]},
        {"id": "EXP-NAV", "hypothesis": "H-NAV: навигатор улучшает определение правильного маршрута",
         "expected_change": "Предварительный полезный ориентир +20 п.п. успешных задач; не прогноз",
         "baseline": None, "baseline_plan": "Сначала измерить выполнение задач с прямыми справками и затем зафиксировать размер/протокол сравнения",
         "metric": "Доля задач с правильным местом управления и следующим шагом без подсказки",
         "timeframe": "D0 протокол; D1–D3 прототип; после baseline назначить длительность и объём сравнения",
         "guardrails": ["Нет ложного ожидания, что отмена уже выполнена", "Никаких операций за пользователя", "Не ухудшать p90 времени более 20% относительно измеренного baseline"]},
    ]
    return _change(root, case_id, state, FINAL_OPERATION, {
        "phase": "options", "status": "awaiting_human",
        "next_action": "Получить выбор пользователя: O-STATUS / O-PILOT (рекомендация) / O-PLATFORM / O-CLOSE. Стратегии пока условные черновики.",
        "registries": {"options": options, "promises": promises, "experiments": experiments}, "artifacts": artifacts})
