# «Перемена» — бот-помощник классного руководителя в MAX
# Базовый образ закреплён по версии и дайджесту — сборка воспроизводима.
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

LABEL org.opencontainers.image.title="Перемена"       org.opencontainers.image.description="Бот-помощник классного руководителя в MAX"       org.opencontainers.image.source="https://github.com/dimon0804/peremena"       org.opencontainers.image.authors="dimon0804 <dimongamedeveloper@yandex.ru>"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_URL=sqlite+aiosqlite:////srv/data/peremena.db \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY certs ./certs
COPY alembic.ini .
COPY app ./app
COPY webapp ./webapp
COPY site ./site

RUN useradd --uid 10001 --home-dir /srv --no-create-home peremena \
    && mkdir -p /srv/data \
    && chown -R peremena /srv/data
USER peremena

VOLUME ["/srv/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/health', timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "app"]
CMD ["run"]
