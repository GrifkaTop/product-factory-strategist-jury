"""Account for per-model tokens at explicitly supplied rates."""
from decimal import Decimal, InvalidOperation
import re

from .errors import ValidationError


_COUNTS = ("input_tokens", "cached_input_tokens", "output_tokens")
_RATES = ("input", "cached_input", "output")
_ATTEMPTS = ("initial", "retry", "rework")


def _count(row, field):
    value = row.get(field)
    if value is not None and (type(value) is not int or not 0 <= value <= 10**12):
        raise ValidationError(f"{field}: ожидается число токенов до 10¹² или null")
    return value


def _rate(value):
    if value is None:
        return None
    if type(value) not in (int, float, str) or len(str(value)) > 32:
        raise ValidationError("Тариф должен быть числом, десятичной строкой или null")
    try:
        rate = Decimal(str(value))
    except InvalidOperation as error:
        raise ValidationError("Укажите корректный десятичный тариф") from error
    if not rate.is_finite() or not 0 <= rate <= 10**9 or (rate != 0 and rate.adjusted() < -9):
        raise ValidationError("Тариф должен быть от 0 до 10⁹, минимальный ненулевой тариф 10⁻⁹")
    if not rate:
        return Decimal(0)
    if rate != rate.quantize(Decimal("0.000000001")):
        raise ValidationError("Тариф поддерживает до девяти знаков после запятой")
    return rate


def _money(value):
    return None if value is None else format(value.normalize(), "f")


def _priced(count, rate):
    if count == 0:
        return Decimal(0)
    return Decimal(count) * rate / 1_000_000 if count is not None and rate is not None else None


def _required_locator(row, field, required):
    value = row.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValidationError(f"Укажите локатор {field}")
    return value


def summarize_token_usage(data):
    if not isinstance(data, dict):
        raise ValidationError("token_usage должен быть объектом")
    currency, basis = data.get("currency"), data.get("basis")
    if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
        raise ValidationError("Укажите валюту тарифа, например USD")
    if basis not in ("api_estimate", "synthetic") or data.get("coverage") not in ("complete", "partial"):
        raise ValidationError("Укажите basis (api_estimate/synthetic) и coverage (complete/partial)")
    rows = data.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValidationError("Укажите 1–1000 записей использования")
    seen, details, token_totals, costs = set(), [], [], []
    for row in rows:
        identity = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 160 or identity in seen:
            raise ValidationError("Записи использования должны иметь уникальный непустой id")
        seen.add(identity)
        model, attempt = row.get("model"), row.get("attempt")
        if not isinstance(model, str) or not model.strip() or attempt not in _ATTEMPTS:
            raise ValidationError("Укажите модель и attempt (initial/retry/rework)")
        total_input, cached, output = (_count(row, field) for field in _COUNTS)
        if cached is not None and (total_input is None or cached > total_input):
            raise ValidationError("cached_input_tokens входит в input_tokens и не может превышать его")
        if total_input == 0:
            cached = 0
        uncached = total_input - cached if cached is not None else None
        supplied = row.get("rates_per_million", {})
        if not isinstance(supplied, dict) or set(supplied) - set(_RATES):
            raise ValidationError("rates_per_million принимает input, cached_input и output")
        rates = [_rate(supplied.get(kind)) for kind in _RATES]
        real = basis == "api_estimate"
        usage_locator = _required_locator(row, "usage_locator", real and any(item is not None for item in (total_input, output)))
        rate_locator = _required_locator(row, "rate_locator", real and any(rate is not None for rate in rates))
        priced = [_priced(count, rate) for count, rate in zip((uncached, cached, output), rates)]
        cost = sum(priced, Decimal(0)) if None not in priced else None
        tokens = total_input + output if total_input is not None and output is not None else None
        token_totals.append(tokens)
        costs.append(cost)
        details.append({"id": identity, "model": model, "attempt": attempt,
                        "input_tokens": total_input, "cached_input_tokens": cached, "output_tokens": output,
                        "tokens": tokens, "estimated_cost": _money(cost),
                        "rates_per_million": dict(zip(_RATES, map(_money, rates))),
                        "usage_locator": usage_locator, "rate_locator": rate_locator})

    complete = data["coverage"] == "complete"
    known_costs = [cost for cost in costs if cost is not None]
    known_tokens = [tokens for tokens in token_totals if tokens is not None]
    def group(indices):
        return {
            "rows": len(indices),
            "tokens": sum(token_totals[i] for i in indices) if complete and all(token_totals[i] is not None for i in indices) else None,
            "estimated_cost": _money(sum((costs[i] for i in indices), Decimal(0)))
            if complete and all(costs[i] is not None for i in indices) else None,
        }
    groups = {}
    for index, row in enumerate(details):
        groups.setdefault(row["model"], []).append(index)
    return {"currency": currency, "basis": basis, "coverage": data["coverage"], "rows": details,
            "tokens": sum(known_tokens) if complete and len(known_tokens) == len(rows) else None,
            "estimated_cost": _money(sum(known_costs, Decimal(0))) if complete and len(known_costs) == len(rows) else None,
            "known_tokens_subtotal": sum(known_tokens) if known_tokens else None,
            "known_cost_subtotal": _money(sum(known_costs, Decimal(0))) if known_costs else None,
            "by_model": {key: group(indices) for key, indices in groups.items()},
            "by_attempt": {key: group([index for index, row in enumerate(details) if row["attempt"] == key])
                           for key in _ATTEMPTS},
            "scope": "Расчёт по переданным токенам и тарифам. Суммы представлены десятичными строками. "
                     "Оценка по API-тарифу не определяет счёт или расход квоты подписки."}


def compare_token_usage(current, baseline):
    if current is None or baseline is None:
        return {"difference_tokens": None, "difference_estimated_cost": None,
                "reason": "нет обоих замеров"}
    same_basis = current["basis"] == baseline["basis"]
    counts = current["tokens"], baseline["tokens"]
    costs = current["estimated_cost"], baseline["estimated_cost"]
    comparable_cost = same_basis and current["currency"] == baseline["currency"] and None not in costs
    return {"difference_tokens": counts[0] - counts[1] if same_basis and None not in counts else None,
            "difference_estimated_cost": _money(Decimal(costs[0]) - Decimal(costs[1])) if comparable_cost else None,
            "currency": current["currency"] if comparable_cost else None,
            "reason": None if comparable_cost else "неполные данные либо различаются основание расчёта или валюта"}
