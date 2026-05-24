FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV APP_HOST=0.0.0.0 APP_PORT=8000
EXPOSE 8000

# Apply migrations then serve. uvicorn proxy-headers ensures spec §17:
# Cloudflare provides X-Forwarded-{For,Proto}; without --proxy-headers
# we'd see Cloudflare IPs and build `http://` absolute URLs.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn webarhive.web:create_app --factory --host $APP_HOST --port $APP_PORT --proxy-headers --forwarded-allow-ips=*"]
