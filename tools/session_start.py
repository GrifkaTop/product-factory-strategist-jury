"""Read-only SessionStart hook for a checked-out Strategist workspace."""
import json
from pathlib import Path

from strategist.errors import StrategistError
from strategist.grounds import list_cases


def session_context(root):
    listing = list_cases(root)
    verified = [case for case in listing["cases"] if not case["recovery_warning"]]
    hidden = len(listing["unavailable"]) + len(listing["cases"]) - len(verified)
    lines = ["Стратег: только метаданные проверенных кейсов. Начало сессии не изменяет состояние."]
    for case in verified[:8]:
        lines.append(f"- {case['case_id']} · r{case['revision']} · {case['phase']} / {case['status']}")
    if len(verified) > 8:
        lines.append(f"Ещё проверенных кейсов: {len(verified) - 8}. Для полного списка используйте cases.")
    if hidden:
        lines.append(f"Кейсов с недоступной текущей ревизией: {hidden}. Для диагностики используйте cases и checkpoint.")
    lines.append("Продолжайте только кейс, указанный пользователем. При неоднозначности уточните ID, "
                 "затем проверьте checkpoint и status. Возобновление паузы требует запроса пользователя.")
    return "\n".join(lines)


def main():
    try:
        context = session_context(Path(__file__).resolve().parents[1])
    except (StrategistError, OSError):
        context = "Стратег: загрузка списка кейсов недоступна. Проверьте рабочую папку и выполните cases."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


if __name__ == "__main__":
    main()
