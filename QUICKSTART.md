# Быстрый запуск webarhive на сервере (v3.0)

Версия деплоя: **v3.0**. После запуска видна справа в шапке UI.

ZIP-архив содержит исходный код, миграции, Docker-конфиг — всё что нужно.
Истории git внутри нет (легче).

## Что нужно перед запуском

1. **Сервер** (любой Linux/macOS) с установленным Docker / Docker Desktop.
2. **API-ключ от LLM**:
   - **OpenRouter** (https://openrouter.ai/keys) — единый шлюз ко
     множеству провайдеров, формат `sk-or-v1-...`. **Дефолт.**
   - **ИЛИ OpenAI** (https://platform.openai.com/api-keys) — напрямую
     к OpenAI, формат `sk-...` или `sk-proj-...`.
   - Достаточно ОДНОГО из двух — провайдер выбирается в `/settings`
     после деплоя.
3. (Опционально) **WhoisJSON-токен** (https://whoisjson.com) — для
   реальных дат регистрации доменов. Бесплатный план: 1000 запросов/мес.

## Вариант 1 — Docker (рекомендуется)

```bash
# 1) распаковать
unzip webarhive-v3.0-DATE.zip && cd webarhive

# 2) создать .env из примера и вписать ключи
cp .env.example .env
nano .env
# Минимум: вписать ключ для выбранного провайдера —
# OPENROUTER_API_KEY ИЛИ OPENAI_API_KEY (или оба, переключение в /settings).

# 3) запустить
docker compose up -d --build

# 4) проверить, что живой
curl -fsS http://127.0.0.1:8000/help >/dev/null && echo OK
docker compose logs -f webarhive    # хвост логов
```

Откройте `http://127.0.0.1:8000` в браузере — справа в шапке должно быть
`v3.0`. Если не v3.0 — Docker не пересобрал образ, см. раздел «Обновление».

Контейнер слушает только на `127.0.0.1:8000` — наружу публикуется через
Cloudflare Tunnel (см. ниже). Том `./data` хранит SQLite-БД с прогонами;
**не удаляйте его**, иначе потеряете историю.

Остановить:
```bash
docker compose down
```

## Обновление поверх существующей установки

```bash
# распаковать поверх, сохраняя .env и data/
unzip -o webarhive-vX.Y-DATE.zip -x 'webarhive/.env' 'webarhive/data/*'
cd webarhive
docker compose build --no-cache webarhive   # ⚠ важно: --no-cache!
docker compose up -d
```

**Флаг `--no-cache` критичен** — без него Docker может переиспользовать
кэшированные слои и не подхватить новые `.py`-файлы. Проверьте по
номеру версии в шапке UI, что обновление прошло.

## Вариант 2 — без Docker (для разработки / отладки)

Требуется Python 3.11+.

```bash
unzip webarhive-v3.0-DATE.zip && cd webarhive
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env && nano .env
alembic upgrade head
uvicorn webarhive.web.app:create_app --factory \
        --host 127.0.0.1 --port 8000 \
        --proxy-headers --forwarded-allow-ips='*'
```

Под systemd:
```ini
# /etc/systemd/system/webarhive.service
[Unit]
Description=webarhive
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/webarhive
EnvironmentFile=/opt/webarhive/.env
ExecStart=/opt/webarhive/.venv/bin/uvicorn webarhive.web.app:create_app \
          --factory --host 127.0.0.1 --port 8000 \
          --proxy-headers --forwarded-allow-ips=*
Restart=on-failure
User=webarhive

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now webarhive
```

## Cloudflare Tunnel + Access (закрываем периметр)

Дашборд **не публичный** — встроенной авторизации нет, доступ
ограничивается на стороне Cloudflare.

1. Установить `cloudflared` на тот же сервер.
2. `cloudflared tunnel login` → авторизоваться в свой аккаунт CF.
3. `cloudflared tunnel create webarhive` → получаете UUID туннеля.
4. Создать конфиг `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: <UUID>
   credentials-file: /root/.cloudflared/<UUID>.json
   ingress:
     - hostname: checker.mycompany.com
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```
5. `cloudflared tunnel route dns webarhive checker.mycompany.com`
6. `cloudflared tunnel run webarhive` (или установить как сервис:
   `cloudflared service install`).
7. В Cloudflare Dashboard → Zero Trust → Access → Applications:
   создать Self-Hosted Application для `checker.mycompany.com`,
   разрешить доступ только своим email/IDP.
8. В `.env` поставить:
   ```
   APP_DOMAIN=checker.mycompany.com
   TRUST_PROXY_HEADERS=true
   ```
   и перезапустить контейнер: `docker compose restart webarhive`.

Дальше — открываете `https://checker.mycompany.com`, Cloudflare Access
просит залогиниться, пускает только своих, дашборд внутри.

## Postgres вместо SQLite (необязательно)

Если ждёте тяжёлой нагрузки или нескольких операторов одновременно —
переключитесь на Postgres:

```env
# .env
DATABASE_URL=postgresql+asyncpg://webarhive:pass@db:5432/webarhive
```

Добавить сервис в `docker-compose.yml`:
```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: webarhive
      POSTGRES_PASSWORD: pass
      POSTGRES_DB: webarhive
    volumes:
      - ./pgdata:/var/lib/postgresql/data
  webarhive:
    depends_on: [db]
    # ... остальное как было
```

При первом запуске `alembic upgrade head` поднимет схему.

## Что проверить после деплоя

1. `http://127.0.0.1:8000` (или ваш домен) → видна главная (пустой список прогонов).
2. Версия в шапке справа = ожидаемая (`v3.0`).
3. `/help` → справка открывается.
4. `/settings` → форма редактируется; сохранение пишет `data/settings.json`.
5. В `/settings` выбран нужный `LLM_PROVIDER` (openrouter / openai) и
   статус соответствующего ключа = «✓ задан».
6. Загрузить 1-2 домена через главную → запустить прогон → убедиться,
   что в логах CDX/тематика/вердикт прокатываются, прогресс в карточке
   обновляется live.

## Если что-то не работает

- Логи контейнера: `docker compose logs --tail=200 webarhive`.
- Старая версия в шапке после деплоя → пересоберите `docker compose build --no-cache webarhive`.
- Размер БД и место: `du -sh data/`.
- Проверить, что ключ валиден: `/settings` → внизу блок «развёртывание»
  → «статус OpenRouter/OpenAI ключа» = «✓ задан».
- Если CDX отдаёт 429 — увеличьте `IA_BACKOFF` или уменьшите
  `IA_RATE_LIMIT` / `CONCURRENCY` в настройках.
- Если карточка пустая и `verdict=null` — скорее всего выключен
  `ENABLE_VERDICT`. Включается в `/settings`.
- Если прогон долгий и упирается в LLM — проверьте тарифную сетку в
  `/settings` и выберите более дешёвую модель для `classification`
  (она вызывается до 40× на домен).

## Минимально необходимый набор переменных в `.env`

```ini
# LLM-провайдер (один из двух)
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
# ИЛИ:
# LLM_PROVIDER=openai
# OPENAI_API_KEY=sk-...

# Деплоймент
APP_DOMAIN=checker.mycompany.com
TRUST_PROXY_HEADERS=true
```

Остальное имеет разумные дефолты и редактируется уже из UI.
