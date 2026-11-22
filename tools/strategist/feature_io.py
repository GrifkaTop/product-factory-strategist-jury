"""Connect stage-six analyses to verified local case data."""

from hashlib import sha256
import json

from . import state as storage
from .changes import load_revision
from .errors import ValidationError
from .outcomes import review_experiment


def _source_data(case_dir, source, *, extracted=False):
    field = "extracted_path" if extracted else "snapshot_path"
    digest = source.get("extracted_sha256" if extracted else "sha256")
    prefix = "extracted/" if extracted else "inputs/"
    size = (20 if extracted else 5) * 1024 * 1024
    path = storage._relative(source[field])
    if not path.startswith(prefix) or not digest:
        raise ValidationError("Источник должен иметь проверяемый путь и хеш")
    with storage._path(case_dir, path).open("rb") as stream:
        data = stream.read(size + 1)
    if len(data) > size or sha256(data).hexdigest() != digest:
        raise ValidationError("Содержимое источника не совпадает с сохранённой версией")
    return data


def _ids(record, key):
    values = record.get(key)
    if not isinstance(values, list):
        raise ValidationError(f"{key} должен быть списком идентификаторов")
    return values


def claim_texts(case_dir, state, claim_ids):
    source_ids = {sid for claim in state["registries"]["claims"] if claim["id"] in claim_ids
                  for sid in _ids(claim, "source_ids") if isinstance(sid, str)}
    return {
        source["id"]: _source_data(case_dir, source, extracted=True).decode("utf-8")
        for source in state["registries"]["sources"]
        if source["id"] in source_ids and source.get("extracted_sha256")
    }


def promise_texts(case_dir, state):
    claims = {cid for promise in state["registries"]["promises"]
              for cid in _ids(promise, "claim_ids") if isinstance(cid, str)}
    return claim_texts(case_dir, state, claims)


def assess_outcome(root, case_dir, state, experiment_id, source_id):
    sources = {item["id"]: item for item in state["registries"]["sources"]}
    experiments = {item["id"]: item for item in state["registries"]["experiments"]}
    if experiment_id not in experiments or source_id not in sources:
        raise ValidationError("Эксперимент или источник отсутствует в кейсе")
    protocol = experiments[experiment_id].get("test_protocol")
    revision = protocol.get("registered_revision") if isinstance(protocol, dict) else None
    prior = None
    if type(revision) is int and 0 <= revision < state["revision"] and "recovery_warning" not in state:
        prior = load_revision(root, state["case_id"], revision)
    return review_experiment(
        state, experiment_id, source_id, _source_data(case_dir, sources[source_id]), prior,
    )


def save_outcome(root, state, result):
    if state["status"] == "paused" or "recovery_warning" in state:
        raise ValidationError("Сохранение требует активного проверенного состояния")
    if not result["preregistration"]["verified"]:
        raise ValidationError("Для сохранения результата нужен проверенный предшествующий протокол")
    identity = "\0".join(result[key] for key in ("experiment_id", "protocol_sha256", "source_id", "source_sha256"))
    digest = sha256(identity.encode("utf-8")).hexdigest()
    path = f"outputs/outcome-{digest}.md"
    operation_id = "outcome-" + digest
    if any(op["operation_id"] == operation_id for op in state["operations"]):
        return {"saved_revision": state["revision"], "artifact": path, "already_saved": True}
    experiment = next(item for item in state["registries"]["experiments"]
                      if item["id"] == result["experiment_id"])
    saved = {**result, "artifact": path}
    claim = {
        "id": "RESULT-" + digest,
        "type": "synthetic" if result["synthetic"] else "fact",
        "synthetic": result["synthetic"],
        "status": "supported",
        "text": (f"Пересчёт импортированного результата {result['experiment_id']} "
                 f"дал {result['status']}. Изменение в п.п. {result['delta_pp']}. "
                 "Поддерживается арифметика по предоставленным счётчикам. "
                 "Происхождение наблюдений и причинный эффект требуют отдельной проверки."),
        "source_ids": [result["source_id"]],
        "locator": result["locator"],
    }
    counts = result["outcome_reconciliation"]
    handoff = result["review_handoff"]
    effect = result["directed_effect_pp"]
    bounds = result.get("missing_outcome_bounds")
    uncertainty = result["statistical_uncertainty"]
    interval = uncertainty["reported"]
    comparison = uncertainty.get("lower_bound_comparison")
    rows = []
    for key, title in (("baseline", "Контроль"), ("variant", "Тест")):
        values = [str(counts[key][field]) if counts[key][field] is not None else "неизвестно"
                  for field in ("assigned", "known", "missing")]
        rows.append("| " + " | ".join([title, *values]) + " |")
    body = "\n".join((
        "# Пересмотр по результатам проверки",
        "",
        "**Ожидает рассмотрения человеком.** " + ("Учебные данные." if result["synthetic"] else ""),
        "",
        f"- Результат правила — `{result['status']}`.",
        f"- Продуктовая стратегия — {result['proposed_product_follow_up']}.",
        f"- Коммуникационная стратегия — {result['proposed_communication_follow_up']}.",
        f"- Владелец решения — {handoff['decision_owner'] or 'роль нужно подтвердить'}.",
        f"- Следующее действие — {handoff['next_action']}",
        "",
        "## Сверка исходов",
        "",
        "| Группа | Назначено | Исход известен | Осталось неизвестных |",
        "|---|---:|---:|---:|",
        *rows,
        "",
        (f"Наблюдаемый эффект в сторону улучшения — {effect:+.2f} п.п. по известным исходам."
         if effect is not None else "Для наблюдаемого эффекта нужны известные исходы в обеих группах."),
        (f"Границы по сценариям пропусков — от {bounds['lower_directed_pp']:+.2f} "
         f"до {bounds['upper_directed_pp']:+.2f} п.п. по всем назначенным."
         if bounds is not None else "Для границ по пропускам нужны числа назначенных в обеих группах."),
        (f"Предоставленный ДИ {interval['level']:.1%} — от {interval['lower_directed_pp']:+} "
         f"до {interval['upper_directed_pp']:+} п.п. База `{interval['population']}`. "
         f"Оценка требования `{uncertainty['status']}`."
         if interval is not None else "Доверительный интервал не предоставлен."),
        *((f"Сравнение нижней границы с порогом — {comparison['lower_directed_pp']:+} п.п. "
           f"{comparison['relation']} {comparison['minimum_effect_pp']:+} п.п.",)
          if comparison is not None else ()),
        "",
        "## Расчёт и происхождение",
        "```json",
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        "```",
    ))
    after = storage.apply_change(root, state["case_id"], {
        "schema_version": 1,
        "operation_id": operation_id,
        "expected_revision": state["revision"],
        "kind": "patch",
        "payload": {
            "registries": {"experiments": [{**experiment, "latest_outcome": saved}], "claims": [claim]},
            "artifacts": {path: body},
            "next_action": handoff["next_action"],
        },
    })
    return {"saved_revision": after["revision"], "artifact": path, "already_saved": False}
