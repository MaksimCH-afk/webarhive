"""Closed enum of topic categories (spec §6.1).

Editable registry — promt to the LLM is generated from this list.
Risky categories take priority over neutral ones (if a page shows both,
mark as risky).

`icon` names match the Lucide-style inline SVG library declared in
web/templates/_icons.html. Add a new key there if you add a new icon
to a category.
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
    label_ru: str         # long label for cards / details
    short_ru: str         # compact label for the epoch bar
    description: str
    icon: str             # lucide icon key (see _icons.html)


CATEGORIES: tuple[Category, ...] = (
    # Neutral / content
    Category("информационный_контентный", CategoryGroup.NEUTRAL, "Информационный",
             "Контент", "Блоги, СМИ, новости, справочники, вики", "FileText"),
    Category("коммерция_магазин", CategoryGroup.NEUTRAL, "Магазин",
             "Магазин", "Интернет-магазины, товары", "ShoppingCart"),
    Category("услуги_бизнес", CategoryGroup.NEUTRAL, "Услуги",
             "Услуги", "Компания продаёт услуги", "Briefcase"),
    Category("корпоративный_брендовый", CategoryGroup.NEUTRAL, "Корпоративный",
             "Корп.", "Сайт-визитка компании/бренда без явной продажи", "Building2"),
    Category("технический_сервис", CategoryGroup.NEUTRAL, "Сервис",
             "Сервис", "SaaS, онлайн-сервис, приложение, API", "Cog"),
    # Risky (priority over neutral)
    Category("гемблинг_казино", CategoryGroup.RISKY, "Гемблинг",
             "Казино", "Слоты, ставки, casino, bet, покер", "Dice5"),
    Category("адалт", CategoryGroup.RISKY, "Адалт",
             "Адалт", "18+, порно-контент", "Eye"),
    Category("фарма", CategoryGroup.RISKY, "Фарма",
             "Фарма", "Таблетки, дженерики, аптека без лицензии", "Pill"),
    Category("займы_фин_спам", CategoryGroup.RISKY, "Финансы",
             "Финансы", "Микрозаймы, бинарные опционы, форекс-разводы, крипто-удвоители", "Percent"),
    Category("варез_пиратство", CategoryGroup.RISKY, "Пиратка",
             "Пиратка", "Кряки, торренты, keygen, серийники", "Download"),
    Category("дорвей_спам_фарм", CategoryGroup.RISKY, "Дорвей",
             "Дорвей", "SEO-простыни, мусорный набор ключевиков, накрутка ссылок", "Spam"),
    # Service / state (not a topic)
    Category("парковка_заглушка", CategoryGroup.SERVICE, "Парковка",
             "Парк.", "Домен припаркован/на продаже (Sedo, GoDaddy parking, buy this domain)", "ParkingSquare"),
    Category("пусто_нет_контента", CategoryGroup.SERVICE, "Пусто",
             "Пусто", "Страница есть, текста нет (битый снапшот, голый JS-каркас)", "FileX2"),
    Category("не_определено", CategoryGroup.SERVICE, "Не определено",
             "?", "Контент есть, но не классифицируется", "HelpCircle"),
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
