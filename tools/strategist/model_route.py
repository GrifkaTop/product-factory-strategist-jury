"""Compare observed model profiles on the same preregistered scenarios."""
from math import isfinite
from statistics import median

from .errors import ValidationError


def _names(values, label, minimum=1):
    if (not isinstance(values, list) or len(values) < minimum
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or len(set(values)) != len(values)):
        raise ValidationError(f"{label}: нужны уникальные непустые названия (минимум {minimum})")
    return values


def _duration(value):
    if value is not None and (type(value) not in (int, float) or not 0 < value <= 604800 or not isfinite(value)):
        raise ValidationError("elapsed_seconds должен быть положительным числом до 7 суток или null")
    return value


def rank_profiles(data):
    if not isinstance(data.get("task"), str) or not data["task"].strip():
        raise ValidationError("Укажите тип задачи в task")
    scenarios = _names(data.get("scenarios"), "scenarios", 2)
    checks = _names(data.get("required_checks"), "required_checks")
    profiles = _names(data.get("profiles"), "profiles", 2)
    if type(data.get("synthetic")) is not bool or not isinstance(data.get("runs"), list):
        raise ValidationError("Укажите synthetic (true/false) и список runs")
    by_profile = {profile: {} for profile in profiles}
    for run in data["runs"]:
        if not isinstance(run, dict):
            raise ValidationError("Каждый прогон должен быть объектом")
        profile, scenario = run.get("profile"), run.get("scenario")
        if (not isinstance(profile, str) or profile not in by_profile
                or not isinstance(scenario, str) or scenario not in scenarios):
            raise ValidationError("Неизвестный профиль или сценарий прогона")
        if scenario in by_profile[profile]:
            raise ValidationError("Для профиля и сценария разрешена одна запись с полным временем, включая повторы")
        status = run.get("status")
        if status not in ("completed", "interrupted"):
            raise ValidationError("status должен быть completed или interrupted")
        passed = run.get("checks")
        if status == "completed" and (not isinstance(passed, dict) or set(passed) != set(checks)
                                      or any(type(value) is not bool for value in passed.values())):
            raise ValidationError("Завершённый прогон должен содержать true/false по каждому обязательному пункту")
        by_profile[profile][scenario] = {
            "elapsed_seconds": _duration(run.get("elapsed_seconds")),
            "status": status,
            "failed_checks": [name for name in checks if status == "completed" and not passed[name]],
        }

    results = []
    for profile, runs in by_profile.items():
        missing = [scenario for scenario in scenarios if scenario not in runs]
        failures = {scenario: run for scenario, run in runs.items()
                    if run["status"] != "completed" or run["failed_checks"]}
        times = [run["elapsed_seconds"] for run in runs.values()]
        eligible = not missing and not failures
        measured = eligible and None not in times
        results.append({"profile": profile, "quality_eligible": eligible,
                        "missing_scenarios": missing, "failures": failures,
                        "median_seconds": median(times) if measured else None})
    eligible = [result for result in results if result["quality_eligible"]]
    comparable = (bool(eligible) and not any(item["missing_scenarios"] for item in results)
                  and all(item["median_seconds"] is not None for item in eligible))
    fastest = min((item["median_seconds"] for item in eligible), default=None) if comparable else None
    winners = [item["profile"] for item in eligible if comparable and item["median_seconds"] == fastest]
    return {"task": data["task"], "synthetic": data["synthetic"], "profiles": results,
            "fastest_eligible": winners,
            "recommended_profile": winners[0] if len(winners) == 1 and not data["synthetic"] else None,
            "scope": "Локальный расчёт по переданным наблюдениям. На неполных замерах и ничьей "
                     "сохраняйте текущую политику. Квота и деньги проверяются отдельно. "
                     "Небольшая выборка не доказывает превосходство модели."}
