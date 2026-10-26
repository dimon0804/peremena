# «Перемена» — бот-помощник классного руководителя в MAX
FROM python:3.12-slim

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

RUN useradd --system --uid 10001 --home /srv peremena \
    && mkdir -p /srv/data \
    && chown -R peremena /srv/data
USER peremena

VOLUME ["/srv/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/health', timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "app"]
CMD ["run"]
