"""Check the calendar and stated decision rule of a quantitative pilot."""
from math import ceil
import re
from statistics import NormalDist

from .errors import ValidationError


_DAYS = {"recruitment_days": 1, "observation_days": 1, "analysis_days": 0, "decision_horizon_days": 1}
_RULES = {"estimate_meets_minimum_and_interval_excludes_zero", "lower_bound_meets_minimum"}
_MAX_AMOUNT = 10**12


def _check_budget(budget):
    if not isinstance(budget, dict) or set(budget) != {"currency", "limit_minor", "items"}:
        raise ValidationError("budget должен содержать currency, limit_minor и items")
    currency, limit, items = budget["currency"], budget["limit_minor"], budget["items"]
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValidationError("budget.currency должен быть трёхбуквенным кодом валюты")
    if type(limit) is not int or not 0 <= limit <= _MAX_AMOUNT:
        raise ValidationError("budget.limit_minor должен быть целым числом от 0 до 10¹²")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValidationError("budget.items должен содержать от 1 до 100 статей")
    names, lines, unknown_items = set(), [], []
    known_total = 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "quantity", "unit_cost_minor"}:
            raise ValidationError("Статья бюджета должна содержать name, quantity и unit_cost_minor")
        name = item["name"].strip() if isinstance(item["name"], str) else ""
        if not 1 <= len(name) <= 160 or name.casefold() in names:
            raise ValidationError("Укажите непустые уникальные названия статей бюджета")
        names.add(name.casefold())
        quantity, unit_cost = item["quantity"], item["unit_cost_minor"]
        for field, value in (("quantity", quantity), ("unit_cost_minor", unit_cost)):
            if value is not None and (type(value) is not int or not 0 <= value <= _MAX_AMOUNT):
                raise ValidationError(f"budget.items.{field} должен быть целым от 0 до 10¹² или null")
        if quantity == 0 or unit_cost == 0:
            amount = 0
        elif quantity is None or unit_cost is None:
            amount = None
            unknown_items.append(name)
        else:
            amount = quantity * unit_cost
        if amount is not None:
            known_total += amount
        lines.append({"name": name, "quantity": quantity, "unit_cost_minor": unit_cost, "amount_minor": amount})

    headroom = limit - known_total
    fits = False if headroom < 0 else None if unknown_items else True
    return {"currency": currency, "limit_minor": limit, "items": lines, "known_total_minor": known_total,
            "headroom_after_known_minor": headroom, "unknown_items": unknown_items, "fits": fits}


def check_pilot_plan(plan):
    if not isinstance(plan, dict):
        raise ValidationError("pilot_plan должен быть объектом")
    for field, minimum in _DAYS.items():
        value = plan.get(field)
        if type(value) is not int or not minimum <= value <= 3650:
            raise ValidationError(f"{field} должен быть целым числом от {minimum} до 3650")
    confidence = plan.get("confidence_level")
    if type(confidence) not in (int, float) or not 0 < confidence < 1:
        raise ValidationError("confidence_level должен быть долей от 0 до 1, например 0.95")
    rule = plan.get("interval_rule")
    if not isinstance(rule, str) or rule not in _RULES:
        raise ValidationError("Укажите interval_rule для двустороннего доверительного интервала")
    for field in ("target_total", "eligible_per_week"):
        value = plan.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000_000):
            raise ValidationError(f"{field} должен быть целым числом от 0 до 1 млрд или null")
    if plan.get("target_total") == 0:
        raise ValidationError("target_total должен быть больше нуля или null")

    recruitment, observation, analysis, horizon = (plan[field] for field in _DAYS)
    slack = horizon - recruitment - observation - analysis
    target, flow = plan.get("target_total"), plan.get("eligible_per_week")
    projection = flow * recruitment // 7 if flow is not None else None
    sample_headroom = projection - target if target is not None and projection is not None else None
    sample_gap = max(0, -sample_headroom) if sample_headroom is not None else None
    extra = {"budget": _check_budget(plan["budget"])} if "budget" in plan else {}
    return {"calendar_fits": slack >= 0, "decision_slack_days": slack,
            "earliest_decision_day": recruitment + observation + analysis,
            "projected_eligible": projection, "sample_shortfall": sample_gap, "sample_headroom": sample_headroom,
            "confidence_level": confidence, "interval_rule": rule, "read_only": True,
            **extra,
            "scope": "Календарные дни от старта набора. Окно последнего участника входит в срок. "
                     "Поток проецируется линейно; выборка, метод интервала и эффект требуют обоснования."}


def plan_binary_pilot(data):
    """Conservative normal planning for independent binary outcomes, 1:1."""
    if not isinstance(data, dict) or type(data.get("synthetic")) is not bool:
        raise ValidationError("Укажите объект проекта и synthetic")
    calendar = data.get("calendar")
    if not isinstance(calendar, dict) or calendar.get("interval_rule") != "lower_bound_meets_minimum":
        raise ValidationError("Календарь должен использовать правило lower_bound_meets_minimum")
    if calendar.get("target_total") is not None:
        raise ValidationError("target_total рассчитывается из требуемого эффекта и мощности")
    check_pilot_plan(calendar)
    confidence = calendar["confidence_level"]
    power = data.get("power")
    values = [data.get(key) for key in ("baseline_rate", "planning_reduction", "minimum_reduction")]
    if (any(type(value) not in (int, float) or not 0 < value < 1 for value in values)
            or type(power) not in (int, float) or not 0.5 < power <= 0.9999
            or not 0.5 < confidence <= 0.9999):
        raise ValidationError("Укажите доли для baseline_rate, planning_reduction и minimum_reduction; "
                              "power и confidence_level должны быть больше 0,5 и не выше 0,9999")
    baseline, reduction, minimum = values
    separation = reduction - minimum
    if not 0 < minimum < reduction < baseline or separation < 0.00001:
        raise ValidationError("Нужно 0 < minimum_reduction < planning_reduction < baseline_rate "
                              "и не менее 0,00001 между порогом и плановым эффектом")
    normal = NormalDist()
    quantile = normal.inv_cdf((1 + confidence) / 2) + normal.inv_cdf(power)
    per_arm = ceil(0.5 * (quantile / separation) ** 2)
    if per_arm * 2 > 1_000_000_000:
        raise ValidationError("Требуемая выборка превышает поддерживаемый размер")
    result = check_pilot_plan({**calendar, "target_total": per_arm * 2})
    outcomes = (baseline, baseline - reduction)
    return {**result, "synthetic": data["synthetic"], "target_per_arm": per_arm, "target_total": per_arm * 2,
            "minimum_eligible_per_week": ceil(per_arm * 14 / calendar["recruitment_days"]),
            "expected_small_cells": any(min(rate, 1 - rate) * per_arm < 10 for rate in outcomes),
            "planning_formula": "ceil(0.5 * ((z((1+confidence)/2) + z(power)) / (planning_reduction - minimum_reduction))²)",
            "scope": "Приближение нормальным распределением, набор 1:1 и один независимый бинарный исход на единицу. "
                     "Проверяйте предпосылки, фактическую базу, пропуски и мощность выбранного метода до запуска."}
