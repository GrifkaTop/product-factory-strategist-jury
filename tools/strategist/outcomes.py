"""Pure, bounded review of binary experiment results; no inference or storage."""

import hashlib
import json
import math
from fractions import Fraction

from .errors import ValidationError


MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
MAX_GUARDRAILS = 200
_PROTOCOL_FIELDS = {"schema_version", "id", "registered_revision", "metric_id", "unit",
                    "observation_window", "direction", "minimum_effect_pp",
                    "min_sample_per_group", "guardrails"}
_RESULT_FIELDS = {"schema_version", "experiment_id", "protocol_id", "metric_id", "unit",
                  "observation_window", "synthetic", "baseline", "variant", "guardrails"}
_INTERVAL_FIELDS = {"level", "method"}
_REPORTED_INTERVAL_FIELDS = _INTERVAL_FIELDS | {"population", "lower_directed_pp", "upper_directed_pp"}
_INTERVAL_GAPS = {
    "missing": "Предусмотренный протоколом доверительный интервал ещё не предоставлен",
    "known_outcomes_only": "Интервал по известным исходам не проверяет порог для всех назначенных",
    "assignments_unknown": "Для проверки интервала нужны числа назначенных в обеих группах",
    "overlaps_threshold": "Доверительный интервал включает эффекты ниже зарегистрированного порога",
}


def _object(value, fields, path):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"Пересмотр: {path} требует ровно поля {', '.join(sorted(fields))}")
    return value


def _text(value, path, limit=160):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValidationError(f"Пересмотр: некорректный текст {path}")
    return value


def _integer(value, path, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise ValidationError(f"Пересмотр: {path} требует безопасное целое >= {minimum}")
    return value


def _number(value, path, minimum=0, maximum=MAX_SAFE_INTEGER, positive=False):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or not math.isfinite(value) or positive and value == 0):
        raise ValidationError(f"Пересмотр: некорректное конечное число {path}")
    return Fraction(str(value))


def _version(value, path):
    if type(value) is not int or value != 1:
        raise ValidationError(f"Пересмотр: {path} требует schema_version=1")


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValidationError("Пересмотр: требуются конечные JSON-данные") from exc


def _state(state):
    if not isinstance(state, dict):
        raise ValidationError("Пересмотр: state должен быть объектом")
    _version(state.get("schema_version"), "state")
    _text(state.get("case_id"), "case_id")
    _integer(state.get("revision"), "revision")
    _canonical(state)
    registries = state.get("registries")
    if not isinstance(registries, dict):
        raise ValidationError("Пересмотр: registries должен быть объектом")
    indices = {}
    for name in ("sources", "experiments"):
        entries = registries.get(name)
        if not isinstance(entries, list) or len(entries) > 10000:
            raise ValidationError(f"Пересмотр: некорректный реестр {name}")
        index = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError(f"Пересмотр: запись {name} должна быть объектом")
            identifier = _text(entry.get("id"), name + ".id")
            if identifier in index:
                raise ValidationError(f"Пересмотр: повтор id в {name}")
            index[identifier] = entry
        indices[name] = index
    return indices


def _guards(value, path, protocol):
    if not isinstance(value, list) or len(value) > MAX_GUARDRAILS:
        raise ValidationError(f"Пересмотр: {path} требует список до {MAX_GUARDRAILS} записей")
    result = {}
    for entry in value:
        fields = {"id", "max_rate"} if protocol else {"id", "events", "total"}
        _object(entry, fields, path)
        identifier = _text(entry["id"], path + ".id")
        if identifier in result:
            raise ValidationError(f"Пересмотр: повтор guardrail {identifier}")
        if protocol:
            _number(entry["max_rate"], path + ".max_rate", maximum=1)
        else:
            _counts(entry["events"], entry["total"], path)
        result[identifier] = entry
    return result


def _interval_definition(value, path, fields):
    _object(value, fields, path)
    level = _number(value["level"], path + ".level", maximum=1, positive=True)
    if level == 1:
        raise ValidationError(f"Пересмотр: {path}.level должен быть меньше 1")
    _text(value["method"], path + ".method", 1000)


def _protocol(value):
    with_interval = isinstance(value, dict) and "confidence_interval" in value
    _object(value, _PROTOCOL_FIELDS | {"confidence_interval"} if with_interval else _PROTOCOL_FIELDS, "test_protocol")
    _version(value["schema_version"], "test_protocol")
    for field in ("id", "metric_id", "observation_window"):
        _text(value[field], "test_protocol." + field, 1000 if field == "observation_window" else 160)
    _integer(value["registered_revision"], "registered_revision")
    _integer(value["min_sample_per_group"], "min_sample_per_group", 1)
    _number(value["minimum_effect_pp"], "minimum_effect_pp", positive=True)
    if value["unit"] != "rate" or value["direction"] not in ("higher", "lower"):
        raise ValidationError("Пересмотр: unit=rate и direction=higher/lower обязательны")
    if with_interval:
        _interval_definition(value["confidence_interval"], "test_protocol.confidence_interval", _INTERVAL_FIELDS)
    return _guards(value["guardrails"], "test_protocol.guardrails", True)


def _assess_interval(required, reported, has_assignments, threshold):
    if reported is None:
        return {"status": "missing" if required is not None else "not_evaluated",
                "required": required, "reported": None}
    if required is None:
        raise ValidationError("Пересмотр: confidence_interval требует зарегистрированное правило в test_protocol")
    path = "results.confidence_interval"
    _interval_definition(reported, path, _REPORTED_INTERVAL_FIELDS)
    lower = _number(reported["lower_directed_pp"], path + ".lower_directed_pp", minimum=-100, maximum=100)
    upper = _number(reported["upper_directed_pp"], path + ".upper_directed_pp", minimum=-100, maximum=100)
    population = reported["population"]
    if lower > upper or population not in ("all_assigned", "known_outcomes"):
        raise ValidationError(f"Пересмотр: неверные границы или population в {path}")
    if population == "known_outcomes":
        status = "known_outcomes_only"
    else:
        if any(reported[key] != required[key] for key in _INTERVAL_FIELDS):
            raise ValidationError(f"Пересмотр: уровень и метод {path} должны совпадать с протоколом")
        if not has_assignments:
            status = "assignments_unknown"
        elif lower >= threshold:
            status = "meets_threshold"
        elif upper < threshold:
            status = "below_threshold"
        else:
            status = "overlaps_threshold"
    return {
        "status": status, "required": required, "reported": reported,
        "lower_bound_comparison": {
            "lower_directed_pp": reported["lower_directed_pp"],
            "relation": "<" if lower < threshold else ">=",
            "minimum_effect_pp": float(threshold),
        },
    }


def _counts(events, total, path):
    for key, number in (("events/successes", events), ("total", total)):
        if number is not None:
            _integer(number, path + "." + key)
    if events is not None and total is not None and events > total:
        raise ValidationError(f"Пересмотр: число событий превышает total в {path}")
    return Fraction(events, total) if events is not None and total not in (None, 0) else None


def _assignment_bounds(groups, direction):
    bounds = {}
    for name, group in groups.items():
        assigned = group["assigned_total"]
        successes = group["successes"]
        bounds[name] = (Fraction(successes, assigned),
                        Fraction(successes + group["missing_total"], assigned))
    low = (bounds["variant"][0] - bounds["baseline"][1]) * 100
    high = (bounds["variant"][1] - bounds["baseline"][0]) * 100
    return (-high, -low) if direction == "lower" else (low, high)


def _parse(source_bytes):
    if type(source_bytes) is not bytes or len(source_bytes) > MAX_SOURCE_BYTES:
        raise ValidationError("Пересмотр: источник должен быть bytes размером до 5 MiB")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("повтор ключа JSON")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("нечисловая константа JSON: " + value)

    try:
        value = json.loads(source_bytes.decode("utf-8-sig"), object_pairs_hook=pairs,
                           parse_constant=constant)
        pending = [(value, 0)]
        nodes = 0
        while pending:
            node, depth = pending.pop()
            nodes += 1
            if depth > 64 or nodes > 10000:
                raise ValueError("слишком большой или вложенный JSON")
            if isinstance(node, float) and not math.isfinite(node):
                raise ValueError("неконечное число JSON")
            children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
            if nodes + len(pending) + len(children) > 10000:
                raise ValueError("слишком большой JSON")
            pending.extend((child, depth + 1) for child in children)
        _canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValidationError("Пересмотр: некорректный UTF-8 JSON: " + str(exc)) from exc


def _registration(state, registered_state, experiment_id, source_id, source_hash, protocol):
    reasons = []
    revision = protocol["registered_revision"]
    if revision >= state["revision"]:
        reasons.append("Регистрация протокола должна предшествовать текущей revision результатов")
    if "recovery_warning" in state:
        reasons.append("Текущий checkpoint восстановлен; требуется проверка целостности")
    if registered_state is None:
        reasons.append("Нет проверенной исторической версии регистрации протокола")
    else:
        indices = _state(registered_state)
        if "recovery_warning" in registered_state:
            reasons.append("Историческая версия восстановлена и не подтверждает регистрацию")
        if registered_state["case_id"] != state["case_id"]:
            reasons.append("Историческая версия принадлежит другому кейсу")
        if registered_state["revision"] != revision:
            reasons.append("Историческая revision не совпадает с registered_revision")
        previous = indices["experiments"].get(experiment_id)
        if previous is None or _canonical(previous.get("test_protocol")) != _canonical(protocol):
            reasons.append("В исторической версии нет идентичного протокола эксперимента")
        if source_id in indices["sources"] or any(
                entry.get("sha256") == source_hash for entry in indices["sources"].values()):
            reasons.append("Источник результатов уже присутствовал при регистрации протокола")
    return {"verified": not reasons, "registered_revision": revision, "reasons": reasons}


def review_experiment(state: dict, experiment_id: str, source_id: str,
                      source_bytes: bytes, registered_state: dict | None = None) -> dict:
    """Review a verified case projection and snapshot bytes without I/O or mutation.

    ``registered_state`` must be supplied by the caller's verified load_revision.
    Ordering in repository history does not prove independent preregistration.
    The caller must admit only own/open anonymized data under the data policy.
    """
    indices = _state(state)
    _text(experiment_id, "experiment_id")
    _text(source_id, "source_id")
    if experiment_id not in indices["experiments"] or source_id not in indices["sources"]:
        raise ValidationError("Пересмотр: experiment_id/source_id отсутствует в текущем кейсе")
    source = indices["sources"][source_id]
    if source.get("kind") != "json" or source.get("source_id", source_id) != source_id:
        raise ValidationError("Пересмотр: требуется зарегистрированный JSON-источник с согласованным ID")
    encoding = source.get("encoding", "utf-8")
    if not isinstance(encoding, str) or encoding.casefold() not in ("utf-8", "utf-8-sig"):
        raise ValidationError("Пересмотр: JSON-результаты требуют UTF-8 источник")
    if type(source.get("synthetic", False)) is not bool:
        raise ValidationError("Пересмотр: source.synthetic должен быть bool")
    if type(source_bytes) is not bytes or len(source_bytes) > MAX_SOURCE_BYTES:
        raise ValidationError("Пересмотр: source_bytes требует bytes размером до 5 MiB")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    if source.get("sha256") != source_hash:
        raise ValidationError("Пересмотр: SHA256 результатов не совпадает с зарегистрированным snapshot")
    experiment = indices["experiments"][experiment_id]
    decision_owner = experiment.get("decision_owner")
    if decision_owner is not None:
        _text(decision_owner, "experiment.decision_owner")
    protocol = experiment.get("test_protocol")
    expected_guards = _protocol(protocol)
    payload = _parse(source_bytes)
    with_interval = isinstance(payload, dict) and "confidence_interval" in payload
    _object(payload, _RESULT_FIELDS | {"confidence_interval"} if with_interval else _RESULT_FIELDS, "results")
    _version(payload["schema_version"], "results")
    for field, expected in (("experiment_id", experiment_id), ("protocol_id", protocol["id"]),
                            ("metric_id", protocol["metric_id"]), ("unit", "rate"),
                            ("observation_window", protocol["observation_window"])):
        if payload[field] != expected or not isinstance(payload[field], str):
            raise ValidationError(f"Пересмотр: results.{field} не совпадает с протоколом")
    if type(payload["synthetic"]) is not bool:
        raise ValidationError("Пересмотр: results.synthetic должен быть bool")
    groups = {}
    rates = {}
    reasons = []
    with_assignment = any(isinstance(payload[name], dict) and "assigned_total" in payload[name]
                          for name in ("baseline", "variant"))
    group_fields = {"successes", "total", "assigned_total"} if with_assignment else {"successes", "total"}
    for name in ("baseline", "variant"):
        group = _object(payload[name], group_fields, "results." + name)
        rate = _counts(group["successes"], group["total"], name)
        rates[name] = rate
        groups[name] = {**group, "rate": float(rate) if rate is not None else None}
        if with_assignment:
            assigned = _integer(group["assigned_total"], name + ".assigned_total", 1)
            if group["successes"] is None or group["total"] is None or group["total"] > assigned:
                raise ValidationError(f"Пересмотр: {name} с assigned_total требует известные successes и total ≤ assigned_total")
            groups[name]["missing_total"] = assigned - group["total"]
        if rate is None:
            reasons.append(f"{name}: отсутствуют наблюдения или total=0; это не нулевая частота")
        elif group["total"] < protocol["min_sample_per_group"]:
            reasons.append(f"{name}: объём ниже min_sample_per_group; уверенный отрицательный вывод невозможен")
    actual_guards = _guards(payload["guardrails"], "results.guardrails", False)
    if set(actual_guards) - set(expected_guards):
        raise ValidationError("Пересмотр: результаты содержат неизвестный guardrail")
    guardrails = []
    breached = False
    for identifier, expected in expected_guards.items():
        observation = actual_guards.get(identifier, {"events": None, "total": None})
        rate = _counts(observation["events"], observation["total"], "guardrail." + identifier)
        harm = rate > Fraction(str(expected["max_rate"])) if rate is not None else None
        breached = breached or harm is True
        guardrails.append({"id": identifier, "events": observation["events"],
                           "total": observation["total"], "rate": float(rate) if rate is not None else None,
                           "max_rate": expected["max_rate"], "breached": harm})
        if harm:
            reasons.append(f"Guardrail {identifier}: наблюдаемая частота превышает max_rate")
        elif rate is None:
            reasons.append(f"Guardrail {identifier}: данных недостаточно; отсутствие вреда не установлено")
        elif observation["total"] < protocol["min_sample_per_group"]:
            reasons.append(f"Guardrail {identifier}: малый объём; отсутствие вреда не установлено")
    preregistration = _registration(state, registered_state, experiment_id, source_id, source_hash, protocol)
    reasons.extend(preregistration["reasons"])
    delta = ((rates["variant"] - rates["baseline"]) * 100
             if all(v is not None for v in rates.values()) else None)
    effect = -delta if delta is not None and protocol["direction"] == "lower" else delta
    threshold = Fraction(str(protocol["minimum_effect_pp"]))
    lower, upper = _assignment_bounds(groups, protocol["direction"]) if with_assignment else (effect, effect)
    if lower is not None and lower < threshold <= upper:
        reasons.append("Неизвестные исходы могут изменить вывод о выполнении порога")
    uncertainty = _assess_interval(protocol.get("confidence_interval"), payload.get("confidence_interval"),
                                   with_assignment, threshold)
    uncertainty_status = uncertainty["status"]
    if uncertainty_status in _INTERVAL_GAPS:
        reasons.append(_INTERVAL_GAPS[uncertainty_status])
    if breached:
        status = "guardrail_breached"
    elif reasons:
        status = "insufficient_data"
    elif uncertainty_status == "below_threshold":
        status = "threshold_not_met"
        reasons.append("Верхняя граница доверительного интервала ниже зарегистрированного порога")
    elif lower >= threshold:
        status = "threshold_met"
        reasons.append("Зарегистрированное правило эффекта выполнено")
    else:
        status = "threshold_not_met"
        reasons.append("Наблюдаемый эффект ниже порога; это не доказательство отсутствия полезного эффекта")
    synthetic = source.get("synthetic", False) or payload["synthetic"]
    product = {
        "guardrail_breached": "Передать человеку пересмотр пилота и меры против наблюдаемого вреда до расширения",
        "insufficient_data": "Дополнить измерения и проверить регистрацию протокола; сохранить неопределённость выбора",
        "threshold_met": "Предложить человеку ограниченный следующий этап проверки с учётом цены ошибки и альтернатив",
        "threshold_not_met": "Предложить человеку пересмотр механизма, повторную проверку, status quo или отказ от запуска",
    }[status]
    communication = {
        "guardrail_breached": "Сообщить, какой защитный порог превышен, и предложить приостановить обещание безопасного расширения",
        "insufficient_data": "Указать пробелы из отчёта и отложить публичное обещание улучшения метрики",
        "threshold_met": "Описать наблюдаемую разницу и окно проверки; проверить обещание на следующей группе",
        "threshold_not_met": "Указать наблюдаемый эффект и зарегистрированный порог; пересмотреть количественное обещание",
    }[status]
    if synthetic:
        product = "Учебный результат. " + product + ". Реальные продуктовые решения требуют реальных данных"
        communication = "Обозначить расчёт как учебный (synthetic). " + communication
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    line_count = len(rendered.splitlines())
    locator = f"L000001-L{line_count:06d}"
    return {
        "schema_version": 1, "read_only": True, "case_id": state["case_id"],
        "revision": state["revision"], "experiment_id": experiment_id, "source_id": source_id,
        "status": status, "synthetic": synthetic, "protocol_id": protocol["id"],
        "protocol_sha256": hashlib.sha256(_canonical(protocol)).hexdigest(),
        "source_sha256": source_hash, "locator": locator,
        "metric_id": protocol["metric_id"], "unit": "rate",
        "observation_window": protocol["observation_window"], "direction": protocol["direction"],
        "minimum_effect_pp": protocol["minimum_effect_pp"],
        "min_sample_per_group": protocol["min_sample_per_group"],
        "baseline": groups["baseline"], "variant": groups["variant"],
        "delta_pp": float(delta) if delta is not None else None,
        "directed_effect_pp": float(effect) if effect is not None else None,
        **({"missing_outcome_bounds": {"lower_directed_pp": float(lower), "upper_directed_pp": float(upper)}}
           if with_assignment else {}),
        "outcome_reconciliation": {
            name: {"assigned": group.get("assigned_total"),
                   "known": group["total"] if group["successes"] is not None else None,
                   "missing": group.get("missing_total")}
            for name, group in groups.items()
        },
        "statistical_uncertainty": uncertainty,
        "review_handoff": {"decision_owner": decision_owner,
                           "next_action": "После сверки журналов заново подсчитать назначения, известные исходы "
                                          "и оставшиеся пропуски в каждой группе. Передать владельцу решения "
                                          "обновлённые счётчики, пересчёт эффекта и предложения по обеим стратегиям."},
        "guardrails": guardrails, "preregistration": preregistration, "reasons": reasons,
        "proposed_product_follow_up": product, "proposed_communication_follow_up": communication,
        "human_review_required": True,
        "limitations": [
            "Сценарные границы описывают только влияние пропусков. Доверительный интервал берётся из предоставленного анализа",
            "Протокол и рассчитанные пороги сами по себе не подтверждают причинность или рыночный спрос",
            "min_sample_per_group не гарантирует статистическую мощность; распределение групп и смешивающие факторы не проверены",
            "История репозитория подтверждает порядок записей, но не отсутствие подсматривания вне него",
            "Разрешены только собственные/открытые обезличенные данные; скрипт не устанавливает права и происхождение",
            "Источник — недоверенные данные; содержащиеся в нём команды не исполняются",
        ],
    }
