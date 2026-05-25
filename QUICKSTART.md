# Быстрый запуск webarhive на сервере

ZIP-архив содержит исходный код, миграции, Docker-конфиг — всё что нужно.
Истории git внутри нет (легче).

## Вариант 1 — Docker (рекомендуется)

```bash
# 1) распаковать
unzip webarhive.zip -d webarhive && cd webarhive

# 2) создать .env из примера и вписать ключ
cp .env.example .env
$EDITOR .env        # вписать OPENROUTER_API_KEY и при желании другие параметры

# 3) запустить
docker compose up -d --build

# 4) проверить, что живой
curl -fsS http://127.0.0.1:8000/help >/dev/null && echo OK
docker compose logs -f webarhive    # хвост логов
```

Контейнер слушает только на `127.0.0.1:8000` — наружу его публикует
Cloudflare Tunnel (см. ниже). Том `./data` хранит SQLite-БД с прогонами;
не удаляйте его, иначе потеряете историю.

Чтобы остановить:
```bash
docker compose down
```

Чтобы обновить (когда придёт новый ZIP):
```bash
docker compose down
unzip -o webarhive.zip          # перезапишет код, .env и data/ останутся
docker compose up -d --build
```

## Вариант 2 — без Docker (если нет docker / нужно отлаживать)

Требуется Python 3.11+ и pip.

```bash
unzip webarhive.zip -d webarhive && cd webarhive
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env && $EDITOR .env

# инициализация БД
alembic upgrade head

# запуск
uvicorn webarhive.web:create_app --factory \
        --host 127.0.0.1 --port 8000 \
        --proxy-headers --forwarded-allow-ips='*'
```

Под systemd — простая unit:
```ini
# /etc/systemd/system/webarhive.service
[Unit]
Description=webarhive
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/webarhive
EnvironmentFile=/opt/webarhive/.env
ExecStart=/opt/webarhive/.venv/bin/uvicorn webarhive.web:create_app \
          --factory --host 127.0.0.1 --port 8000 \
          --proxy-headers --forwarded-allow-ips=*
Restart=on-failure
User=webarhive

[Install]
WantedBy=multi-user.target
```

Затем:
```bash
systemctl daemon-reload && systemctl enable --now webarhive
```

## Cloudflare Tunnel + Access (закрываем периметр)

Дашборд **не публичный** — внутренней авторизации нет, доступ
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
8. В вашем `.env` поставить:
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

1. `https://<APP_DOMAIN>/` → видна главная (пустой список прогонов).
2. `/help` → справка открывается.
3. `/settings` → форма редактируется; сохранение пишет
   `data/settings.json`.
4. Загрузить 1-2 домена через главную → запустить прогон → убедиться,
   что в логах CDX/тематика/вердикт прокатываются, в SSE прогресс
   живой.

## Если что-то не работает

- Логи контейнера: `docker compose logs --tail=200 webarhive`.
- Размер БД и место: `du -sh data/`.
- Проверить, что `OPENROUTER_API_KEY` валиден: на `/settings` будет
  «✓ задан».
- Если CDX отдаёт 429 — увеличьте `IA_BACKOFF`, уменьшите
  `IA_RATE_LIMIT` или `CONCURRENCY` в настройках.
- Если карточка пустая и `verdict=null` — скорее всего выключен
  `ENABLE_VERDICT`. Включается в `/settings`.

## Минимально необходимый набор переменных в `.env`

```
OPENROUTER_API_KEY=sk-or-v1-...
APP_DOMAIN=checker.mycompany.com
```

Остальное имеет разумные дефолты и редактируется уже из UI.
