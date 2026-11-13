# GTM crawler API + worker share this image; the process is chosen per
# docker-compose service via `command:` (gtm-api / gtm-worker / alembic).
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md .
COPY gtm_crawler ./gtm_crawler
COPY alembic.ini .
COPY config.toml .

RUN pip install --no-cache-dir .

EXPOSE 8002
