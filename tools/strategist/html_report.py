"""Pure, offline HTML projection of untrusted case data (stdlib only)."""

from html import escape
from hashlib import sha256
import ipaddress
import json
import re
from urllib.parse import urlsplit

from .grounds import LINKS, follow


_ARTIFACTS = (
    ("brief", "Постановка задачи"), ("research-plan", "План исследования"),
    ("evidence", "Доказательства"), ("options", "Варианты"),
    ("decision-memo", "Записка для выбора"), ("product-strategy", "Продуктовая стратегия"),
    ("communication-strategy", "Коммуникационная стратегия"),
    ("validation-plan", "План проверки"), ("executive-summary", "Краткий итог"),
)


def _plain(value):
    if value is None or value == "":
        return "Не указано"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _e(value):
    return escape(_plain(value), quote=True)


def _safe_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        # A URL is only attribution, never fetched. Reject credential, local and
        # service targets as well as executable URL schemes.
        host = (parsed.hostname or "").lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", host):
                return False
        return (parsed.scheme in {"http", "https"} and bool(host)
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 80, 443))
    except ValueError:
        return False


def _inline(text):
    """Small Markdown subset; raw HTML and image loading are never enabled."""
    tokens = re.split(r"(`[^`\n]+`|!?\[[^\]\n]*\]\([^\s()]+\))", text)
    result = []
    for token in tokens:
        if token.startswith("`") and token.endswith("`") and len(token) > 1:
            result.append("<code>" + escape(token[1:-1]) + "</code>")
            continue
        match = re.fullmatch(r"(!?)\[([^\]]*)\]\(([^\s()]+)\)", token)
        if match and not match[1] and _safe_url(match[3]):
            result.append('<a rel="noreferrer noopener" href="' + escape(match[3], quote=True) + '">' + escape(match[2]) + "</a>")
        else:
            result.append(escape(token))
    return "".join(result)


def _markdown(text):
    """Headings, paragraphs, flat lists, pipe tables and fenced code only."""
    lines = text.splitlines()
    out, index = [], 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].lstrip().startswith("```"):
                code.append(lines[index])
                index += 1
            out.append("<pre><code>" + escape("\n".join(code)) + "</code></pre>")
        elif not line.strip():
            pass
        elif re.match(r"^#{1,6} ", line):
            level = min(len(line.split(" ", 1)[0]) + 1, 6)
            out.append(f"<h{level}>" + _inline(line.split(" ", 1)[1]) + f"</h{level}>")
        elif index + 1 < len(lines) and "|" in line and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1]):
            cells = lambda row: row.strip().strip("|").split("|")
            out.append('<div class="scroll"><table><thead><tr>' + "".join("<th>" + _inline(c.strip()) + "</th>" for c in cells(line)) + "</tr></thead><tbody>")
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                out.append("<tr>" + "".join("<td>" + _inline(c.strip()) + "</td>" for c in cells(lines[index])) + "</tr>")
                index += 1
            out.append("</tbody></table></div>")
            continue
        elif re.match(r"^\s*(?:[-*+] |\d+\. )", line):
            ordered = bool(re.match(r"^\s*\d+\. ", line))
            tag = "ol" if ordered else "ul"
            pattern = r"^\s*\d+\. (.*)" if ordered else r"^\s*[-*+] (.*)"
            out.append("<" + tag + ">")
            while index < len(lines) and (match := re.match(pattern, lines[index])):
                out.append("<li>" + _inline(match[1]) + "</li>")
                index += 1
            out.append("</" + tag + ">")
            continue
        else:
            out.append("<p>" + _inline(line) + "</p>")
        index += 1
    return "\n".join(out)


def _record_anchor(registry, identity):
    return "record-" + registry + "-" + sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()


def _records(title, records, registry=None, targets=frozenset()):
    out = ["<section><h2>" + escape(title) + "</h2>"]
    if not isinstance(records, list) or not records:
        out.append('<p class="muted">Записи отсутствуют.</p>')
    else:
        for item in records:
            identity = item.get("id") if isinstance(item, dict) else None
            anchor = f' id="{_record_anchor(registry, identity)}"' if registry and isinstance(identity, str) else ""
            out.append(f'<article class="record"{anchor}>')
            if isinstance(item, dict):
                if item.get("synthetic") is True or item.get("type") == "synthetic":
                    out.append('<span class="badge">SYNTHETIC · учебные данные</span>')
                if item.get("status") in ("draft", "needs_review", "unverified", "planned"):
                    out.append('<span class="badge">' + _e(item["status"]) + " · требует проверки</span>")
                out.append("<dl>")
                for key, value in item.items():
                    rendered = _e(value)
                    if key == "url" and _safe_url(value):
                        rendered = '<a rel="noreferrer noopener" href="' + escape(value, quote=True) + '">' + _e(value) + "</a>"
                    elif registry and key in LINKS:
                        linked = follow(key, value)
                        if linked:
                            rendered = ", ".join(
                                f'<a href="#{_record_anchor(*target)}">{_e(target[1])}</a>' if target in targets
                                else _e(target[1]) + ' <span class="muted">(цель ссылки отсутствует)</span>'
                                for target in linked
                            )
                    out.append("<dt>" + _e(key) + "</dt><dd>" + rendered + "</dd>")
                out.append("</dl>")
            else:
                out.append("<p>" + _e(item) + "</p>")
            out.append("</article>")
    return "\n".join(out) + "</section>"


_REVIEW_LABELS = {
    "owner/responsible": "ответственный", "review_condition/decision_rule": "условие пересмотра / правило решения",
    "owner": "владелец", "responsible": "ответственный", "metric": "метрика",
    "timeframe": "срок", "guardrails": "ограничения", "review_condition": "условие пересмотра",
    "decision_rule": "правило решения", "missing_fields": "незаполненные поля",
    "stale": "устарело", "retracted": "отозвано", "contested": "оспаривается",
    "unverified": "не проверено", "unknown": "неизвестно", "open": "открыт",
    "needs_review": "требует пересмотра", "high": "высокий", "normal": "обычный",
    "fact": "факт", "assumption": "допущение", "hypothesis": "гипотеза",
    "forecast": "прогноз", "synthetic": "учебные данные",
}


def _review_text(value):
    """Localize known review vocabulary, keeping arbitrary content plain text."""
    if isinstance(value, str):
        for key in sorted(_REVIEW_LABELS, key=len, reverse=True):
            value = re.sub(r"(?<![\w/])" + re.escape(key) + r"(?![\w/])",
                           lambda match: _REVIEW_LABELS[key], value)
    return _e(value)


def _check_review(state, review):
    """Reject malformed or unrelated snapshots before producing any HTML."""
    def require(condition):
        if not condition:
            raise ValueError("review must be a matching build_review snapshot with valid fields")

    require(isinstance(review, dict))
    try:
        json.dumps(review, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("review must contain finite JSON data") from exc
    require(isinstance(review.get("case_id"), str) and review["case_id"] == state.get("case_id"))
    require(type(review.get("revision")) is int and type(state.get("revision")) is int
            and review["revision"] >= 0 and review["revision"] == state["revision"])
    require(type(review.get("schema_version")) is int and review["schema_version"] == 1)
    require(review.get("mode") == "snapshot" and review.get("read_only") is True)
    require(type(review.get("mutations_blocked")) is bool)
    for field in ("limitation", "phase", "status"):
        require(isinstance(review.get(field), str))
    require(review["phase"] == state.get("phase") and review["status"] == state.get("status"))
    require(review.get("blocked_reason") is None or isinstance(review["blocked_reason"], str))
    approval = review.get("approval")
    require(isinstance(approval, dict))
    require(type(approval.get("current")) is bool and isinstance(approval.get("reason"), str))
    for field in ("decision_id", "selected_option_id"):
        require(field in approval and (approval[field] is None or isinstance(approval[field], str)))
        if approval["current"]:
            require(isinstance(approval[field], str) and bool(approval[field].strip()))
    schemas = {
        "decisions_now": {"id": str, "priority": str, "kind": str, "message": str},
        "evidence_attention": {"id": str, "type": (str, type(None)), "status": str, "source_ids": list},
        "open_gaps": {"id": str, "priority": str, "status": str, "question": (str, type(None))},
        "commitments": {"id": str, **{key: (str, type(None)) for key in
            ("owner", "responsible", "metric", "timeframe", "review_condition", "decision_rule")},
            "guardrails": (str, list, type(None)), "missing_fields": list},
    }
    for name, fields in schemas.items():
        require(isinstance(review.get(name), list))
        for item in review[name]:
            require(isinstance(item, dict))
            for key, types in fields.items():
                require(key in item and isinstance(item[key], types))
            for key in ("source_ids", "missing_fields", "guardrails"):
                if isinstance(item.get(key), list):
                    require(all(isinstance(value, str) for value in item[key]))
            if "related_id" in item:
                require(isinstance(item["related_id"], str))


def _decision_front(state, review, options):
    status = state.get("status")
    approval = review["approval"]
    if (state.get("phase") not in {"options", "strategies", "validation", "complete"}
            and status not in {"paused", "needs_review"} and not approval["current"]):
        return ""
    candidates = options if isinstance(options, list) else []
    choice = None
    label = "Рекомендация"
    decision_now = state.get("next_action")
    if status == "needs_review" or state.get("recovery_warning"):
        recommendation = "Требуется пересмотр"
        risk = "Изменились основания или доступна только восстановленная версия"
        decision_now = "Проверить актуальность оснований в обзоре и пересмотреть выбор"
    else:
        if approval["current"]:
            label = "Согласованный выбор"
            choice = next((v for v in candidates if isinstance(v, dict)
                           and v.get("id") == approval["selected_option_id"]), None)
        elif status == "awaiting_human":
            proposed = [v for v in candidates if isinstance(v, dict) and v.get("recommended") is True]
            if len(proposed) == 1:
                choice = proposed[0]
        recommendation = (choice.get("title") or choice.get("id")) if choice else None
        risk = choice.get("objection") if choice else None
        if review["mutations_blocked"]:
            decision_now = review["blocked_reason"]
    fields = (
        (label, _e(recommendation) if recommendation else '<a href="#decision-memo">Открыть записку для выбора</a>'),
        ("Главный риск", _e(risk) if risk else '<a href="#options">Открыть варианты и возражения</a>'),
        ("Действие сейчас", _e(decision_now)),
    )
    return ('<div class="decision-front"><h2>Для руководителя</h2><dl>'
            + ''.join(f'<dt>{key}</dt><dd>{value}</dd>' for key, value in fields)
            + '</dl><p><a href="#evidence">Доказательства</a> · <a href="#validation-plan">План проверки</a> · '
            '<a href="#decision-overview">Обзор решения</a></p></div>')


def _overview(review):
    out = ['<section id="decision-overview"><h2>Что требует внимания</h2>',
           '<p>Снимок сохранённого состояния · только чтение. Это не мониторинг выполнения и не уведомления.</p>',
           '<p>' + _e(review["limitation"]) + '</p>']
    if review["status"] == "paused":
        out.append('<p class="badge">Кейс на паузе. Изменения запрещены до явного возобновления.</p>')
    if review["mutations_blocked"]:
        out.append('<p class="badge">Изменения заблокированы. ' + _e(review["blocked_reason"]) + '</p>')
    approval = review["approval"]
    out.append('<h3>Согласование текущего выбора</h3><p>' +
               ('Актуальный выбор записан.' if approval["current"] else 'Актуального согласования нет.') +
               ' ' + _e(approval["reason"]) + '</p>')
    if approval["current"]:
        out.append('<p>Решение: ' + _e(approval["decision_id"]) +
                   '; выбранный вариант: ' + _e(approval["selected_option_id"]) + '</p>')
    out.append('<p class="muted">Отчёт не создаёт согласование и не подтверждает личность автора ответа.</p>')
    out.append('<h3>Вопросы и ближайшие действия</h3>')
    if not review["decisions_now"]:
        out.append('<p>В переданном снимке ближайшие действия не отмечены.</p>')
    else:
        out.append('<ul>')
        for item in review["decisions_now"]:
            related = ' · запись: ' + _e(item["related_id"]) if "related_id" in item else ''
            out.append('<li>' + _review_text(item["message"]) + ' <span class="muted">Приоритет: ' +
                       _review_text(item["priority"]) + related + '</span></li>')
        out.append('</ul>')
    out.append('<h3>Основания, требующие проверки</h3>')
    if not review["evidence_attention"]:
        out.append('<p>В снимке нет отмеченных оснований. Это не доказывает их актуальность.</p>')
    for item in review["evidence_attention"]:
        out.append('<p>Утверждение ' + _e(item["id"]) + ': ' + _review_text(item["status"]) +
                   '; тип: ' + _review_text(item["type"]) + '; источники: ' +
                   (_e(', '.join(item["source_ids"])) if item["source_ids"] else 'Не указано') + '</p>')
    out.append('<h3>Открытые пробелы</h3>')
    if not review["open_gaps"]:
        out.append('<p>В снимке открытые пробелы не отмечены.</p>')
    for item in review["open_gaps"]:
        out.append('<p>' + _e(item["id"]) + ': ' + _e(item["question"]) +
                   ' · приоритет: ' + _review_text(item["priority"]) +
                   ' · статус: ' + _review_text(item["status"]) + '</p>')
    out.append('<h3>План обязательств и проверки результата</h3>')
    if not review["commitments"]:
        out.append('<p>В снимке обязательства не записаны.</p>')
    else:
        out.append('<div class="scroll"><table><thead><tr>' + ''.join('<th>' + label + '</th>' for label in
            ('Проверка', 'Владелец', 'Ответственный', 'Метрика', 'Срок', 'Правило решения',
             'Условие пересмотра', 'Ограничения', 'Что не заполнено')) + '</tr></thead><tbody>')
        for item in review["commitments"]:
            out.append('<tr>')
            for key in ('id', 'owner', 'responsible', 'metric', 'timeframe', 'decision_rule',
                        'review_condition', 'guardrails', 'missing_fields'):
                value = item[key]
                if isinstance(value, list):
                    value = '; '.join(value) if value else ('Нет' if key == 'missing_fields' else None)
                out.append('<td>' + (_review_text(value) if key == 'missing_fields' else _e(value)) + '</td>')
            out.append('</tr>')
        out.append('</tbody></table></div>')
    out.append('</section>')
    return '\n'.join(out)


def render_html(state: dict, validation: dict | None = None, review: dict | None = None) -> str:
    """Return an escaped static projection; perform no IO and no validation IO.

    Optional review must be a matching build_review snapshot. Without it no
    executive overview is inferred, including for older partial states.
    No approval, missing business facts or validation results are inferred.
    Markdown is a documented limited subset, not full CommonMark.
    """
    if not isinstance(state, dict) or validation is not None and not isinstance(validation, dict):
        raise ValueError("state and optional validation must be dictionaries")
    if review is not None:
        _check_review(state, review)
    registry = state.get("registries") if isinstance(state.get("registries"), dict) else {}
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    decision_front = _decision_front(state, review, registry.get("options")) if review is not None else ""
    header_fields = (("goal", "Цель"), ("phase", "Стадия"), ("status", "Статус"),
                     ("revision", "Ревизия"), ("updated_at", "Время состояния"))
    if not decision_front:
        header_fields += (("next_action", "Следующий шаг"),)
    primary_paths = {f"outputs/{slug}.md" for slug, _ in _ARTIFACTS}
    extra_markdown = sorted((path, content) for path, content in artifacts.items()
                            if isinstance(path, str) and path.startswith("outputs/")
                            and path.endswith(".md") and path not in primary_paths
                            and isinstance(content, str))
    out = ['<!doctype html><html lang="ru"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;; form-action &#39;none&#39;">',
           '<title>Отчёт · ' + _e(state.get("case_id")) + '</title><style>',
           'body{margin:0;background:#f4f7f5;color:#20352b;font:16px/1.65 system-ui,sans-serif}main{max-width:1080px;margin:auto;padding:32px 20px}header,section{background:white;padding:24px;margin-bottom:20px;border:1px solid #dce7df;border-radius:12px}h1,h2,h3{line-height:1.3}h1{margin:10px 0}h2{color:#226346}a{color:#176442;overflow-wrap:anywhere}nav{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}.badge{display:inline-block;background:#fff0ca;color:#654900;padding:4px 10px;border-radius:6px;margin:4px}.muted{color:#647269}.record{border-top:1px solid #dce7df;padding:15px 0}dl{display:grid;grid-template-columns:minmax(120px,180px) 1fr;gap:6px 16px}dt{font-weight:600}dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}th,td{text-align:left;border:1px solid #dce7df;padding:8px}th{background:#edf5ef}pre{background:#eef3ef;padding:16px;overflow:auto}code{font-size:.9em}.scroll{overflow:auto}.decision-front{background:#eef6f0;border-left:4px solid #226346;padding:12px 18px;margin:14px 0}.decision-front h2{margin:0 0 10px}.decision-front p{margin-bottom:0}@media(max-width:600px){dl{display:block}dd{margin-bottom:12px}section,header{padding:16px}}',
           '</style></head><body><main><header><p class="muted">Стратег Фабрики продуктов · локальная проекция</p><h1>' + _e(state.get("case_id")) + '</h1>',
           '<span class="badge">ЧЕРНОВИК · требует человеческой проверки</span>',
           decision_front,
           '<p>Неофициальный отчёт. Структурная полнота не доказывает истинность данных, спрос или личность автора ответа. SYNTHETIC означает учебный материал.</p>',
           '<dl>' + ''.join('<dt>' + label + '</dt><dd>' + _e(state.get(key)) + '</dd>' for key, label in header_fields) + '</dl>',
           '<p>needs_review — требуется пересмотр. Запись решения ниже отражает сохранённые данные; отчёт не создаёт согласование.</p></header><nav aria-label="Артефакты">']
    if review is not None:
        out.append('<a href="#decision-overview">Что требует внимания</a>')
    for slug, label in _ARTIFACTS:
        out.append(f'<a href="#{slug}">{label}</a>')
    if extra_markdown:
        out.append('<a href="#additional-materials">Дополнительные материалы</a>')
    out.append('</nav>')
    if state.get("recovery_warning"):
        out.append('<section><h2>Восстановление · needs_review</h2><p>' + _e(state["recovery_warning"]) + '</p></section>')
    if review is not None:
        out.append(_overview(review))
    for slug, label in _ARTIFACTS:
        content = artifacts.get(f"outputs/{slug}.md")
        out.append(f'<section id="{slug}"><h2>{label}</h2>')
        out.append(_markdown(content) if isinstance(content, str) and content.strip() else '<p class="muted">Артефакт не заполнен.</p>')
        out.append('</section>')
    if extra_markdown:
        out.append('<section id="additional-materials"><h2>Дополнительные материалы</h2>')
        for path, content in extra_markdown:
            out.append('<article class="record"><h3>' + _e(path) + '</h3>' + _markdown(content) + '</article>')
        out.append('</section>')
    targets = {(key, item["id"]) for key, rows in registry.items() if isinstance(rows, list)
               for item in rows if isinstance(item, dict) and isinstance(item.get("id"), str)}
    for key, title in (("sources", "Источники и происхождение"), ("claims", "Утверждения и локаторы"),
                       ("options", "Варианты: status_quo / change / alternative / closure"),
                       ("decisions", "Сохранённые решения"), ("gaps", "Пробелы"),
                       ("hypotheses", "Гипотезы"), ("promises", "Обещания и подтверждения"), ("experiments", "Эксперименты")):
        out.append(_records(title, registry.get(key), key, targets))
    out.append('<section><h2>Структурная проверка</h2>')
    if validation is None:
        out.append('<p>Результат проверки не передан. Отчёт не запускал валидатор.</p>')
    else:
        out.append('<p>Переданный результат ok: ' + _e(validation.get("ok")) + '</p>')
        for key, title in (("errors", "Ошибки"), ("warnings", "Предупреждения"), ("checks", "Проверки")):
            out.append(_records(title, validation.get(key)))
    out.append('</section><footer><p>Время, расходы и сведения, отсутствующие в состоянии, неизвестны. Исходные материалы остаются недоверенными данными. Отчёт не заменяет canonical checkpoint.</p></footer></main></body></html>')
    return '\n'.join(out)
