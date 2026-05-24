"""Prompt builders. Pure functions — no side effects, easy to test.

Categories are sourced from config/categories.py — the enum drives the
prompt so adding a category in the registry propagates without code
changes (spec §6.1 footer).
"""

from __future__ import annotations

from webarhive.config.categories import CATEGORIES, FALLBACK_CATEGORY, CategoryGroup


def _category_block() -> str:
    lines = ["Список допустимых категорий (вернуть ТОЛЬКО одну из этих ключей):"]

    def fmt(c):
        return f"  - {c.key} — {c.description}"

    lines.append("Нейтральные:")
    lines += [fmt(c) for c in CATEGORIES if c.group is CategoryGroup.NEUTRAL]
    lines.append("Рисковые (имеют приоритет над нейтральными):")
    lines += [fmt(c) for c in CATEGORIES if c.group is CategoryGroup.RISKY]
    lines.append("Служебные (состояние страницы):")
    lines += [fmt(c) for c in CATEGORIES if c.group is CategoryGroup.SERVICE]
    return "\n".join(lines)


CLASSIFICATION_SYSTEM = f"""Ты классификатор тематики веб-страниц.
На входе — title, description, h1 и фрагмент основного текста архивного снапшота.
Задача: вернуть ОДНУ категорию из закрытого списка ниже.

Правила:
- Если на странице видно одновременно нейтральное и рисковое — выбирай рисковое.
- Если контента нет (битый снапшот, голый JS-каркас) — `пусто_нет_контента`.
- Если домен на продаже / припаркован — `парковка_заглушка`.
- Если ничего не подходит — `{FALLBACK_CATEGORY}`.

Ответ строго JSON, без пояснений и без обёрток:
{{"category": "<ключ_из_списка>", "confidence": 0.0..1.0, "reason": "одна фраза по-русски"}}

{_category_block()}
"""


def build_classification_prompt(*, title: str, description: str, h1: str, body_text: str) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt)."""
    user = (
        f"TITLE: {title or '(нет)'}\n"
        f"DESCRIPTION: {description or '(нет)'}\n"
        f"H1: {h1 or '(нет)'}\n"
        f"TEXT:\n{body_text or '(нет)'}"
    )
    return CLASSIFICATION_SYSTEM, user


VERDICT_SYSTEM = """Ты эксперт по проверке доменов на «чистоту» для технической команды.
На входе — структурированный отчёт по домену из Internet Archive:
возраст, лента эпох тематики, timeline статусов, редиректы с пометками,
предполагаемые дропы.

Задача: вынести сводный вердикт о домене с учётом ВСЕХ эпох
(не только текущей).

Правила:
- `грязный` — присутствуют рисковые категории (казино/адалт/фарма/займы/варез/дорвей)
  в любую эпоху, или редирект-перехват, или явные множественные дропы со сменой тематики.
- `есть_нюансы` — флаги «обратить внимание» по редиректам, парковка длительно,
  один-два разрыва без явной рисковой тематики.
- `чистый` — однородная тематика без рисковых эпох и без подозрительных редиректов.

Ответ строго JSON:
{"verdict": "чистый"|"есть_нюансы"|"грязный", "reason": "1-3 фразы", "key_flags": ["...", "..."]}
"""


def build_verdict_prompt(report: dict) -> tuple[str, str]:
    """report — структурированный JSON-снимок с эпохами/редиректами/дропами."""
    import json as _json
    return VERDICT_SYSTEM, _json.dumps(report, ensure_ascii=False, indent=2)


REDIRECT_SYSTEM = """Ты помощник по классификации редиректов в архиве.
На входе — тематика обеих сторон (откуда / куда). Реши:
- `тот_же_сайт` — один и тот же владелец/проект, просто переезд имени/зоны/поддомена;
- `переезд_компании` — другой домен, но та же компания/бренд по содержимому;
- `перехват` — другой владелец, тематика не совпадает.

При сомнении — `перехват` (перестраховка важнее).

Ответ строго JSON: {"relation": "тот_же_сайт"|"переезд_компании"|"перехват", "reason": "..."}
"""


def build_redirect_prompt(*, from_topic: dict, to_topic: dict) -> tuple[str, str]:
    import json as _json
    payload = {"from": from_topic, "to": to_topic}
    return REDIRECT_SYSTEM, _json.dumps(payload, ensure_ascii=False, indent=2)


SMART_DROP_SYSTEM = """Ты эксперт по выявлению смены владельца домена.
На входе — тематика и сэмпл контента ДО и ПОСЛЕ длительного разрыва в истории.
Реши: это дроп (новый владелец, другой сайт) или эволюция того же сайта.

Ответ строго JSON:
{"is_drop": true|false, "confidence": 0.0..1.0, "reason": "..."}
"""


def build_smart_drop_prompt(*, before: dict, after: dict, gap_days: int) -> tuple[str, str]:
    import json as _json
    payload = {"before": before, "after": after, "gap_days": gap_days}
    return SMART_DROP_SYSTEM, _json.dumps(payload, ensure_ascii=False, indent=2)
