"""Closed enum of topic categories (spec §6.1).

Editable registry — promt to the LLM is generated from this list.
Risky categories take priority over neutral ones (if a page shows both,
mark as risky).
"""

from dataclasses import dataclass
from enum import Enum


class CategoryGroup(str, Enum):
    NEUTRAL = "neutral"
    RISKY = "risky"
    SERVICE = "service"


@dataclass(frozen=True)
class Category:
    key: str
    group: CategoryGroup
    label_ru: str
    description: str
    icon: str  # lucide icon name


CATEGORIES: tuple[Category, ...] = (
    # Neutral / content
    Category("информационный_контентный", CategoryGroup.NEUTRAL, "Информационный/контентный",
             "Блоги, СМИ, новости, справочники, вики", "file-text"),
    Category("коммерция_магазин", CategoryGroup.NEUTRAL, "Магазин",
             "Интернет-магазины, товары", "shopping-cart"),
    Category("услуги_бизнес", CategoryGroup.NEUTRAL, "Услуги/бизнес",
             "Компания продаёт услуги", "briefcase"),
    Category("корпоративный_брендовый", CategoryGroup.NEUTRAL, "Корпоративный/брендовый",
             "Сайт-визитка компании/бренда без явной продажи", "building-2"),
    Category("технический_сервис", CategoryGroup.NEUTRAL, "Технический сервис",
             "SaaS, онлайн-сервис, приложение, API", "code-2"),
    # Risky (priority over neutral)
    Category("гемблинг_казино", CategoryGroup.RISKY, "Гемблинг/казино",
             "Слоты, ставки, casino, bet, покер", "dice-5"),
    Category("адалт", CategoryGroup.RISKY, "Адалт",
             "18+, порно-контент", "eye-off"),
    Category("фарма", CategoryGroup.RISKY, "Фарма",
             "Таблетки, дженерики, аптека без лицензии", "pill"),
    Category("займы_фин_спам", CategoryGroup.RISKY, "Займы/фин-спам",
             "Микрозаймы, бинарные опционы, форекс-разводы, крипто-удвоители", "coins"),
    Category("варез_пиратство", CategoryGroup.RISKY, "Варез/пиратство",
             "Кряки, торренты, keygen, серийники", "download-cloud"),
    Category("дорвей_спам_фарм", CategoryGroup.RISKY, "Дорвей/спам-ферма",
             "SEO-простыни, мусорный набор ключевиков, накрутка ссылок", "spam"),
    # Service / state (not a topic)
    Category("парковка_заглушка", CategoryGroup.SERVICE, "Парковка/заглушка",
             "Домен припаркован/на продаже (Sedo, GoDaddy parking, buy this domain)", "parking-circle"),
    Category("пусто_нет_контента", CategoryGroup.SERVICE, "Пусто/нет контента",
             "Страница есть, текста нет (битый снапшот, голый JS-каркас)", "file-x"),
    Category("не_определено", CategoryGroup.SERVICE, "Не определено",
             "Контент есть, но не классифицируется", "help-circle"),
)

CATEGORY_BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}
CATEGORY_KEYS: tuple[str, ...] = tuple(c.key for c in CATEGORIES)

RISKY_CATEGORIES: tuple[Category, ...] = tuple(c for c in CATEGORIES if c.group is CategoryGroup.RISKY)
NEUTRAL_CATEGORIES: tuple[Category, ...] = tuple(c for c in CATEGORIES if c.group is CategoryGroup.NEUTRAL)
SERVICE_CATEGORIES: tuple[Category, ...] = tuple(c for c in CATEGORIES if c.group is CategoryGroup.SERVICE)

FALLBACK_CATEGORY = "не_определено"


def is_risky(key: str) -> bool:
    cat = CATEGORY_BY_KEY.get(key)
    return cat is not None and cat.group is CategoryGroup.RISKY
