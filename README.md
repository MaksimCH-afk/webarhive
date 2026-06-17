# Web Archive Checker

Серверный инструмент для технической команды: на вход список доменов, на
выходе по каждому — возраст по архиву, лента тематических эпох, история
кодов ответа, классифицированные редиректы, эвристика дропов и
опциональный финальный вердикт (чистый / есть нюансы / грязный) на основе
данных Internet Archive Wayback Machine + LLM-классификации.

Текущая версия: **v3.0** (отображается справа в шапке UI).

---

## Зачем

Сценарий — массовая проверка доменов «что это было раньше и нет ли там
тёмного прошлого». Под капотом — Wayback Machine как источник истории и
LLM как классификатор тематики каждого исторического слепка.

Главное отличие от «погуглить wayback» — полностью автономный пайплайн
с прогрессивным сохранением, кэшированием, повторяемостью и UI для
сравнения десятков-сотен доменов разом.

## Что умеет

- **CDX Server API** — снимает индекс всех захватов домена через
  Wayback CDX (`http://web.archive.org/cdx/search/cdx`) с серверной
  фильтрацией (`statuscode:200|3..|404`, `mimetype:text/html`,
  `collapse=urlkey+digest`), gzip-сжатием и HTTP/2.
- **Cross-run CDX-кэш** — повторные прогоны того же домена в течение
  TTL (24ч по умолчанию) идут с кэш-хитом и нулевыми обращениями к IA.
- **Тематика по эпохам** — для каждого захвата главной (`/`,
  `/index.html`) light-fetch (Range 16KB) + LLM-классификация по
  словарю из 16 категорий (нейтральные / рисковые / служебные). После
  классификации соседние одинаковые версии склеиваются в эпохи.
- **Детектор дропа** — gap-сигналы (длинные разрывы + смена тематики)
  по эвристике; опционально усиление LLM на подозрительных гэпах.
- **Классификатор редиректов** — `same_site` / `company_move` / `review`
  / `technical` через registrable-root (PSL) и Location header;
  опциональное LLM-уточнение пограничных REVIEW c CDX-обогащением цели.
- **WHOIS** — реальная дата регистрации через WhoisJSON (опционально),
  с кэшем 90 дней.
- **Best snapshot per epoch** — для каждой длинной эпохи ищет самый
  полный архивный слепок главной по доле сохранённых ресурсов
  (CSS/JS/img через Availability API).
- **Финальный вердикт** — LLM собирает всю картину (возраст, эпохи,
  редиректы, дропы) и выносит `clean` / `nuanced` / `dirty` с
  обоснованием.

## Архитектура

```
domains.xlsx ─► loader / normaliser ─► run record (SQLite/Postgres)
                                            │
                                            ▼
                              per-domain pipeline (semaphore-bounded):
                              CDX → digest dedup → home-page filter
                                ↓
                              light fetch (Range=16KB, batched)
                                ↓
                              shift detection → LLM classification
                                ↓
                              redirects fetch → classification → LLM-refine
                                ↓
                              gap detection → smart-drop LLM
                                ↓
                              WHOIS lookup
                                ↓
                              best snapshot per epoch
                                ↓
                              verdict LLM
                                ↓
                              persist → UI canvas + per-domain card
```

Все запросы к IA делят один shared throttle (по умолчанию 8 req/s),
LLM-вызовы параллелятся через семафор (по умолчанию 16).

## Структура кода

| Пакет             | Назначение |
|-------------------|------------|
| `config/`         | Settings (env + UI-overrides), словарь категорий |
| `domains/`        | Загрузка списка + нормализация через PSL |
| `cdx/`            | CDX Server API клиент, IA throttle, gzip+HTTP/2 |
| `fetcher/`        | Wayback snapshot fetcher (`id_`-flavour, Range, encodings) |
| `llm/`            | OpenAI-compatible chat-клиент, prompt-шаблоны |
| `analysis/`       | history, topics, redirects, drops, best_snapshot, verdict |
| `orchestrator/`   | Пер-доменный пайплайн + run scheduler |
| `db/`             | SQLAlchemy 2.0 async, alembic-миграции, repo-слой |
| `web/`            | FastAPI + Jinja2 UI: список прогонов, канва, карточка домена, настройки |
| `clients/`        | WhoisJSON клиент |
| `logging_/`       | Per-domain tracer с debounced-flush в БД |
| `tests/`          | pytest, httpx MockTransport, 90 тестов |

## LLM-провайдер

Поддерживаются два режима:

- **`openrouter`** (по умолчанию) — единый шлюз ко множеству провайдеров.
  Ключ: `OPENROUTER_API_KEY` / поле в `/settings`. Формат моделей:
  `провайдер/модель` (`openai/gpt-4o-mini`, `anthropic/claude-sonnet-4.5`,
  `google/gemini-2.0-flash-001`).
- **`openai`** — напрямую к `api.openai.com/v1/chat/completions`. Ключ:
  `OPENAI_API_KEY` / поле в `/settings`. Формат моделей: голое имя
  (`gpt-4o-mini`, `gpt-4o`, `gpt-4.1-mini`, `o3-mini`, `gpt-3.5-turbo`).

Переключение — селектор `LLM_PROVIDER` в UI. В `/settings` есть
тарифная сетка для оценки бюджета на глаз ДО запуска (на 100 доменов
с `gpt-4o-mini` — около $1-2, с `claude-sonnet-4.5` — $30-60).

Модель для каждой роли (`classification`, `verdict`, `smart_drop`,
`redirect`) настраивается независимо.

## Установка и запуск

### Через Docker (рекомендуется)

```bash
unzip webarhive-vX.Y-DATE.zip
cd webarhive
cp .env.example .env
nano .env                       # вписать ключи
docker compose up -d --build
```

UI на `http://127.0.0.1:8000`. Alembic-миграции накатываются автоматически
при старте контейнера.

### Локально (для разработки)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
uvicorn webarhive.web.app:create_app --factory --host 127.0.0.1 --port 8000
```

### Обновление поверх существующей установки

```bash
unzip -o webarhive-vX.Y-DATE.zip -x 'webarhive/.env' 'webarhive/data/*'
cd webarhive
docker compose build --no-cache app    # форсируем пересборку
docker compose up -d
```

Флаг `--no-cache` критичен — без него Docker может переиспользовать
кэшированные слои и не подхватить новые `.py`-файлы.

После рестарта в шапке должна обновиться версия (`vX.Y` справа от
«настройки»). Если показывает старую — образ не пересобрался.

## Конфигурация

Все параметры доступны двумя путями:

1. **`.env`** — стартовые значения (читаются один раз при первом запуске)
2. **`/settings`** — UI-оверрайды (хранятся в `data/settings.json`,
   gitignore). Имеют приоритет над `.env`.

### Ключи и провайдер

| Ключ                  | Назначение |
|-----------------------|------------|
| `LLM_PROVIDER`        | `openrouter` (дефолт) или `openai` |
| `OPENROUTER_API_KEY`  | для `openrouter`, формат `sk-or-v1-...` |
| `OPENAI_API_KEY`      | для `openai`, формат `sk-...` или `sk-proj-...` |
| `WHOIS_API_KEY`       | токен с whoisjson.com (опционально) |
| `DATABASE_URL`        | по умолчанию SQLite; для Postgres: `postgresql+asyncpg://...` |

### Модели по ролям

| Поле                      | Когда вызывается               | Рекомендация |
|---------------------------|--------------------------------|--------------|
| `MODEL_CLASSIFICATION`    | до 40 раз на домен             | дешёвая: `gpt-4o-mini` |
| `MODEL_VERDICT`           | 1 раз на домен                 | можно умную: `claude-sonnet-4.5` |
| `MODEL_SMART_DROP`        | 1-5 раз на домен (на гэпах)    | средняя |
| `MODEL_REDIRECT`          | 1-30 раз (на REVIEW)           | средняя |

### Тюнинг производительности

| Параметр                              | Дефолт | Что делает |
|---------------------------------------|--------|-----------|
| `CONCURRENCY`                         | 4      | Параллельных доменов |
| `IA_RATE_LIMIT`                       | 8.0    | Общий троттл req/s ко всему IA |
| `LLM_PARALLELISM`                     | 16     | Параллельных LLM-вызовов на домене |
| `PER_DOMAIN_TIMEOUT_SEC`              | 1800   | Жёсткий потолок на один домен |
| `LIGHT_FETCH_CAP`                     | 120    | Сэмпл версий главной для тематики |
| `REDIRECT_CAP`                        | 150    | Сэмпл 3xx для классификации |
| `REDIRECT_LLM_REVIEW_CAP`             | 30     | Сколько REVIEW обрабатывает LLM-refine |
| `MAX_LLM_CALLS_PER_DOMAIN`            | 40     | Бюджет LLM на один домен (classification) |
| `TEXT_LIMIT`                          | 1000   | Символов body text в промпт |
| `CDX_CACHE_ENABLED` / `_TTL_HOURS`    | true / 24 | Cross-run кэш |

### Роли LLM (тоггл вкл/выкл)

- `ENABLE_VERDICT` — финальный вердикт. Без него — только флаги.
- `ENABLE_SMART_DROP` — LLM-усиление дроп-эвристики.
- `ENABLE_REDIRECT_LLM` — LLM по пограничным редиректам.

### Best snapshot

- `ENABLE_BEST_SNAPSHOT` — включить поиск лучших слепков на эпоху
- `BEST_SNAPSHOT_TOP_N` (3) — кандидатов на эпоху
- `BEST_SNAPSHOT_MAX_RESOURCES` (8) — ресурсов для Availability-проверки
- `BEST_SNAPSHOT_MIN_EPOCH_DAYS` (30) — пропуск коротких эпох
- `BEST_SNAPSHOT_MAX_EPOCHS` (10) — топ-N длинных эпох
- `BEST_SNAPSHOT_PER_EPOCH_TIMEOUT_SEC` (90) — таймаут на эпоху
- `BEST_SNAPSHOT_EPOCH_PARALLELISM` (3) — параллельных эпох

### WHOIS

- `WHOIS_ENABLED` — включить запросы к WhoisJSON
- `WHOIS_RATE_LIMIT` (0.33 = 20/мин) — req/s
- `WHOIS_CACHE_TTL_DAYS` (90)
- `WHOIS_MONTHLY_FLOOR` (10) — когда осталось меньше N запросов в
  месячном лимите, перестаём ходить в API

## Темы UI

Пять встроенных тем (переключатель в шапке, сохраняется в localStorage):
**стандарт**, **лён**, **nord**, **сланец**, **океан**.

## Что внутри пайплайна (детали)

### CDX-фильтры

Для 200-бакета применяются серверные фильтры
`statuscode:200` + `mimetype:text/html` — RSS, JSON-API, ассеты режутся
на стороне CDX. Дальше клиент-side digest-дедуп и фильтр по home-page
URL — анализ тематики строится **только по захватам главной**.

### Параллелизация

- 3 CDX-бакета (200/3xx/404) тянутся параллельно
- LLM-вызовы классификации идут через `asyncio.gather` + семафор
- Эпохи best-snapshot обрабатываются параллельно
- Per-LLM-call wall-clock timeout 45с от зависших ответов

### Resilience

- Дебаунсированный flush трассы (раз в 0.5с) — оператор видит прогресс
  live, но БД не перегружается
- Persist промежуточных данных в `finally`-блоке: при ошибке в середине
  пайплайна карточка домена всё равно показывает CDX-историю, эпохи,
  редиректы — а не пустоту
- Stale-run reaper в lifespan-хуке: если контейнер упал во время
  прогона, оставшиеся `running`-домены при рестарте помечаются
  aborted и не висят навечно

## Файлы данных и логи

- `data/webarhive.db` — основная БД (SQLite по умолчанию)
- `data/settings.json` — UI-оверрайды настроек
- per-domain trace доступен в карточке домена и через
  `/runs/{id}/log.txt`
- LLM-аудит (`llm_calls` таблица) хранит все вызовы с raw output и
  cost для пост-анализа

## Безопасность

- Сервис слушает на `127.0.0.1:8000` — наружу выводится через
  Cloudflare Tunnel + Access. Встроенной аутентификации нет.
- `.env` и `data/` — в `.gitignore`. Ключи только в `.env` или
  `/settings` (последний → `data/settings.json`).
- Если ключ случайно засветился где-либо: немедленно отзывайте
  (`platform.openai.com/api-keys` / `openrouter.ai/keys`) и
  генерируйте новый.

## Тесты

```bash
pytest tests/ -v
```

90 тестов, всё через httpx MockTransport — без реальных запросов к
IA или LLM-провайдеру.

## Стек

- Python 3.11
- FastAPI + Uvicorn + Jinja2
- SQLAlchemy 2.0 async + Alembic
- httpx (HTTP/2) + tenacity
- selectolax (HTML parsing), tldextract (PSL)
- openpyxl (xlsx импорт)
- pydantic-settings
- pytest + pytest-asyncio + pytest-httpx
