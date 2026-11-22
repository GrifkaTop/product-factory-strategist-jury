"""Read-only sensitivity analysis of explicit linear models, using exact rationals."""

import math
import sys
from decimal import Decimal
from fractions import Fraction
from itertools import combinations

from .errors import ValidationError


MAX_ASSUMPTIONS = 24
MAX_OPTIONS = 24
MAX_SCENARIOS = 64
MAX_NUMBER = 10 ** 100


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{label}: ожидаются только поля {', '.join(fields)}")


def _text(value, label, limit=2000, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValidationError(f"{label}: требуется строка длиной до {limit}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label}: требуется корректный текст UTF-8") from exc
    return value


def _integer(value, label):
    if type(value) is not int or not 0 <= value <= 2 ** 53:
        raise ValidationError(f"{label}: требуется неотрицательное целое")
    return value


def _number(value, label, nullable=False):
    if value is None and nullable:
        return None
    if type(value) not in (int, float):
        raise ValidationError(f"{label}: требуется конечное число, не строка/bool")
    if abs(value) > MAX_NUMBER or (type(value) is float and not math.isfinite(value)):
        raise ValidationError(f"{label}: число вне безопасного диапазона ±1e100")
    return Fraction(Decimal(str(value)))


def _list(value, limit, label, nonempty=False):
    if not isinstance(value, list) or len(value) > limit or (nonempty and not value):
        raise ValidationError(f"{label}: требуется список, максимум {limit}")
    return value


def _json_number(value, *, exact_available=False):
    if value is None:
        return None
    if value.denominator == 1 and abs(value.numerator) <= sys.float_info.max:
        return value.numerator
    try:
        result = float(value)
    except OverflowError:
        result = None
    if result is not None and math.isfinite(result) and result != 0:
        return result
    if exact_available:
        return None
    raise ValidationError("Результат вне диапазона представимых чисел JSON; измените единицы")


def _exact_number(value):
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def _coordinate(key, value):
    approximate = _json_number(value, exact_available=True)
    result = {key: approximate}
    if approximate is None or value != Fraction(Decimal(str(approximate))):
        result[f"exact_{key}"] = _exact_number(value)
    return result


def _leaders(values):
    known = {key: value for key, value in values.items() if value is not None}
    if not known:
        return []
    best = max(known.values())
    return [key for key, value in known.items() if value == best]


def _evaluate(model, values):
    total, coefficients = model
    if total is None:
        return None
    for key, coefficient in coefficients.items():
        # Even an explicit zero coefficient declares this parameter as used.
        if values[key] is None:
            return None
        total += coefficient * values[key]
    return total


def _leadership(boundaries, constants, slopes):
    def at(point):
        return _leaders({key: value + slopes[key] * point for key, value in constants.items()})

    ordered = sorted(boundaries)
    at_points = [at(point) for point in ordered]
    between = [at((start + end) / 2) for start, end in zip(ordered, ordered[1:])]
    significant = [0]
    for index in range(1, len(ordered) - 1):
        if not between[index - 1] == at_points[index] == between[index]:
            significant.append(index)
    if len(ordered) > 1:
        significant.append(len(ordered) - 1)

    points = [{**_coordinate("value", ordered[index]), "leaders": at_points[index]}
              for index in significant]
    intervals = [
        {**_coordinate("low", ordered[start]), **_coordinate("high", ordered[end]),
         "low_inclusive": False, "high_inclusive": False, "leaders": between[start]}
        for start, end in zip(significant, significant[1:])
    ]
    return points, intervals


def stress_test(state: dict, spec: dict) -> dict:
    """Maximize a caller-defined common unit, never select a product option.

    Calculations use exact rational arithmetic from decimal representations of
    JSON numbers. Rounded coordinates retain their exact value alongside the
    numeric projection. Limits bound input and quadratic pairwise output.
    """
    if not isinstance(state, dict):
        raise ValidationError("state: требуется объект проверенного кейса")
    case_id = _text(state.get("case_id"), "case_id", 200)
    revision = _integer(state.get("revision"), "revision")
    registries = state.get("registries")
    if not isinstance(registries, dict):
        raise ValidationError("state.registries: требуется объект")
    options = _list(registries.get("options"), MAX_OPTIONS, "options", True)
    option_ids = []
    for option in options:
        if not isinstance(option, dict):
            raise ValidationError("option: требуется объект")
        key = _text(option.get("id"), "option.id", 200)
        if key in option_ids:
            raise ValidationError("Повтор option.id в кейсе")
        option_ids.append(key)
    _object(spec, ("schema_version", "expected_revision", "title", "value_unit",
                   "reference_option_id", "synthetic", "assumptions", "models", "scenarios"), "spec")
    if _integer(spec["schema_version"], "schema_version") != 1:
        raise ValidationError("Поддерживается schema_version=1")
    if _integer(spec["expected_revision"], "expected_revision") != revision:
        raise ValidationError("expected_revision не совпадает с проверенным кейсом")
    title = _text(spec["title"], "title")
    unit = _text(spec["value_unit"], "value_unit", 200)
    reference = _text(spec["reference_option_id"], "reference_option_id", 200)
    if reference not in option_ids:
        raise ValidationError("Неизвестный reference_option_id")
    if type(spec["synthetic"]) is not bool:
        raise ValidationError("synthetic: требуется bool")

    assumptions = {}
    for item in _list(spec["assumptions"], MAX_ASSUMPTIONS, "assumptions"):
        _object(item, ("id", "unit", "base", "low", "high", "note"), "assumption")
        key = _text(item["id"], "assumption.id", 200)
        if key in assumptions:
            raise ValidationError("Повтор assumption.id")
        assumption_unit = _text(item["unit"], "assumption.unit", 200)
        assumption_note = _text(item["note"], "assumption.note", empty=True)
        base = _number(item["base"], "base", True)
        low, high = _number(item["low"], "low"), _number(item["high"], "high")
        if low > high or (base is not None and not low <= base <= high):
            raise ValidationError("Ожидается low <= base <= high (base может быть null)")
        assumptions[key] = (base, low, high, assumption_unit, assumption_note)

    models = {}
    for item in _list(spec["models"], MAX_OPTIONS, "models", True):
        _object(item, ("option_id", "intercept", "coefficients", "note"), "model")
        key = _text(item["option_id"], "model.option_id", 200)
        if key not in option_ids or key in models:
            raise ValidationError("Неизвестный или повторный model.option_id")
        _text(item["note"], "model.note", empty=True)
        coefficients = item["coefficients"]
        if not isinstance(coefficients, dict) or len(coefficients) > MAX_ASSUMPTIONS:
            raise ValidationError("coefficients: требуется ограниченный объект")
        if any(type(k) is not str or k not in assumptions for k in coefficients):
            raise ValidationError("Неизвестный assumption_id в coefficients")
        models[key] = (_number(item["intercept"], "intercept", True),
                       {k: _number(v, "coefficient") for k, v in coefficients.items()})
    if set(models) != set(option_ids):
        raise ValidationError("models обязаны покрывать все текущие option IDs")
    # Stable order follows the case registry, not user model order.
    models = {key: models[key] for key in option_ids}
    base_values = {key: value[0] for key, value in assumptions.items()}
    scenario_inputs = [("base", base_values, "Базовые значения допущений")]
    scenario_ids = {"base"}
    for item in _list(spec["scenarios"], MAX_SCENARIOS, "scenarios"):
        _object(item, ("id", "values", "note"), "scenario")
        key = _text(item["id"], "scenario.id", 200)
        if key in scenario_ids:
            raise ValidationError("Повтор scenario.id или зарезервированный id base")
        scenario_ids.add(key)
        _text(item["note"], "scenario.note", empty=True)
        overrides = item["values"]
        if not isinstance(overrides, dict) or len(overrides) > MAX_ASSUMPTIONS:
            raise ValidationError("scenario.values: требуется ограниченный объект")
        values = dict(base_values)
        for axis, raw in overrides.items():
            if type(axis) is not str or axis not in assumptions:
                raise ValidationError("Неизвестный assumption_id в scenario")
            value = _number(raw, "scenario.value", True)
            if value is not None and not assumptions[axis][1] <= value <= assumptions[axis][2]:
                raise ValidationError("Значение сценария вне диапазона")
            values[axis] = value
        scenario_inputs.append((key, values, item["note"]))

    scenarios = []
    for key, values, note in scenario_inputs:
        evaluated = {key: _evaluate(model, values) for key, model in models.items()}
        complete = all(value is not None for value in evaluated.values())
        leaders = _leaders(evaluated)
        reference_value = evaluated[reference]
        scenarios.append({"id": key, "note": note,
                          "values": {k: _json_number(v) for k, v in values.items()},
                          "option_values": {k: _json_number(v) for k, v in evaluated.items()},
                          "differences_from_reference": {
                              k: _json_number(v - reference_value) if v is not None and reference_value is not None else None
                              for k, v in evaluated.items()},
                          "leaders_among_evaluable": leaders,
                          "complete_comparison": complete, "leaders": leaders if complete else []})

    axes = []
    for axis, (_, low, high, assumption_unit, assumption_note) in assumptions.items():
        # Evaluate the constant part with this axis set to a known zero,
        # independent of whether zero lies in the user-supplied domain.
        other_values = {**base_values, axis: Fraction(0)}
        constants = {key: _evaluate(model, other_values) for key, model in models.items()}
        unknown = [key for key, value in constants.items() if value is None]
        result = {"assumption_id": axis, "unit": assumption_unit, "note": assumption_note,
                  "low": _json_number(low), "high": _json_number(high),
                  "complete_comparison": not unknown, "unknown_option_ids": unknown,
                  "pairwise_thresholds": [], "leadership_intervals": [], "leadership_points": []}
        if unknown:
            result["reason"] = "Неизвестны intercept или используемые base других допущений"
            axes.append(result)
            continue
        slopes = {key: model[1].get(axis, Fraction(0)) for key, model in models.items()}
        boundaries = {low, high}
        for a, b in combinations(option_ids, 2):
            difference = slopes[a] - slopes[b]
            pair = {"option_ids": [a, b]}
            if difference == 0:
                pair.update(relation="equal_everywhere" if constants[a] == constants[b] else "parallel",
                            threshold=None, in_range=False,
                            dominant_option_id=None if constants[a] == constants[b] else a if constants[a] > constants[b] else b)
            else:
                crossing = (constants[b] - constants[a]) / difference
                inside = low <= crossing <= high
                pair.update(relation="crossing", threshold=_json_number(crossing, exact_available=True),
                            in_range=inside, exact_threshold=_exact_number(crossing))
                if inside:
                    boundaries.add(crossing)
            result["pairwise_thresholds"].append(pair)
        result["leadership_points"], result["leadership_intervals"] = _leadership(boundaries, constants, slopes)
        axes.append(result)
    return {"schema_version": 1, "read_only": True, "case_id": case_id, "revision": revision,
            "title": title, "value_unit": unit, "reference_option_id": reference,
            "synthetic": spec["synthetic"], "objective": "maximize",
            "scenarios": scenarios, "axes": axes, "human_review_required": True,
            "product_choice": None,
            "limitations": ["Лидер общей целевой/экономической величины не является продуктовым выбором.",
                "Линейность и единицы заданы автором модели; неучтённые факторы и цена ошибки требуют отдельной проверки.",
                "synthetic=false не доказывает эмпиричность модели или достоверность исходных данных.",
                "Каждая ось меняется отдельно при base остальных; совместные изменения проверяются сценариями.",
                "Сравнения точные. Поля exact_* хранят точные пороги и округлённые границы в виде дробей. "
                "Числовое поле порога или границы равно null, когда значение представимо только точной дробью.",
                "Продуктовый выбор и допустимую цену ошибки определяет человек."]}
