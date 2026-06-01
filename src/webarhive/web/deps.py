"""Template + dependency wiring."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from webarhive.config.categories import (
    CATEGORIES,
    CATEGORY_BY_KEY,
    is_risky,
)
from webarhive.db.engine import get_session

# Версия деплоя — увеличиваем при каждой правке кода. Шапка показывает
# это значение справа от «настройки» — чтобы оператор видел, что
# именно крутится в Docker'е, и не путался при пересборках.
APP_VERSION = "2.0"


def templates_for(directory: Path) -> Jinja2Templates:
    t = Jinja2Templates(directory=str(directory))
    # Expose category metadata + helpers to templates.
    t.env.globals["CATEGORIES"] = CATEGORIES
    t.env.globals["CATEGORY_BY_KEY"] = CATEGORY_BY_KEY
    t.env.globals["is_risky"] = is_risky
    t.env.filters["category_icon"] = lambda key: (CATEGORY_BY_KEY.get(key).icon if key in CATEGORY_BY_KEY else "help-circle")
    t.env.filters["category_label"] = lambda key: (CATEGORY_BY_KEY.get(key).label_ru if key in CATEGORY_BY_KEY else key)
    t.env.filters["category_group"] = lambda key: (CATEGORY_BY_KEY.get(key).group.value if key in CATEGORY_BY_KEY else "unknown")
    t.env.globals["app_version"] = APP_VERSION
    return t


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


# Re-export session ctx manager for convenience.
__all__ = ["get_session", "templates_for", "get_templates"]
