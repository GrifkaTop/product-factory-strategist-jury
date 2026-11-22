"""Compare conditional annual gross profit scenarios against a decision target."""
from .errors import ValidationError


def _amount(row, field, minimum=0):
    value = row.get(field)
    if type(value) is not int or not minimum <= value <= 10**12:
        raise ValidationError(f"{field}: ожидается целое число от {minimum} до 10¹²")
    return value


def _scenario_months(scenario):
    if "cohorts" not in scenario:
        return _amount(scenario, "additional_paid_months"), None
    if "additional_paid_months" in scenario:
        raise ValidationError("Укажите cohorts или additional_paid_months")
    cohorts = scenario["cohorts"]
    if not isinstance(cohorts, list) or not 1 <= len(cohorts) <= 100:
        raise ValidationError("Нужны 1–100 непересекающихся когорт")
    breakdown = [0] * 12
    seen = set()
    for row in cohorts:
        if not isinstance(row, dict):
            raise ValidationError("Когорта должна быть объектом")
        identity = row.get("id")
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValidationError("Когорты должны иметь уникальные непустые id")
        seen.add(identity)
        start = _amount(row, "first_month", 1)
        eligible = _amount(row, "eligible_unique_numbers", 1)
        shares = row.get("incremental_share_bps_by_month")
        if (start > 12 or not isinstance(shares, list) or not 1 <= len(shares) <= 13 - start
                or any(type(share) is not int or not -10_000 <= share <= 10_000 for share in shares)):
            raise ValidationError("Укажите месяцы в границах года и разницу долей относительно контроля в б. п.")
        for month, share in enumerate(shares, start - 1):
            breakdown[month] += eligible * share
    # Round down once, after aggregating expected number-months over the year.
    return sum(breakdown) // 10_000, [value / 10_000 for value in breakdown]


def compare_revenue(data):
    baseline = _amount(data, "baseline_gross_profit")
    target = _amount(data, "target_gross_profit")
    margin = _amount(data, "margin_per_paid_month", minimum=1)
    if type(data.get("synthetic")) is not bool:
        raise ValidationError("Укажите synthetic: true или false")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 20:
        raise ValidationError("Нужны 2–20 явно заданных сценариев")
    result = {}
    for row in scenarios:
        identity = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identity, str) or not identity.strip() or identity in result:
            raise ValidationError("У сценариев должны быть уникальные непустые id")
        months, monthly = _scenario_months(row)
        expenses = _amount(row, "additional_direct_cost")
        contribution = months * margin - expenses
        required = max(0, (target - baseline + expenses + margin - 1) // margin)
        result[identity] = {"gross_profit": baseline + contribution,
                            "incremental_profit_before_development": contribution,
                            "target_gap": max(0, target - baseline - contribution),
                            "required_paid_months": required, "paid_months_shortfall": max(0, required - months),
                            "max_development_payback_in_period": max(0, contribution)}
        if monthly is not None:
            result[identity]["additional_paid_months"] = months
            result[identity]["monthly_expected_paid_months"] = monthly
    return {"synthetic": data["synthetic"], "scenarios": result, "read_only": True,
            "scope": "Условные сценарии за один период в целых денежных единицах. "
                     "Оплаченные месяцы считаются сверх контроля в границах периода. "
                     "Модель не оценивает причинность, спрос или реальную окупаемость."}
