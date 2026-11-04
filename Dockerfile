# «Перемена» — бот-помощник классного руководителя в MAX
# Базовый образ закреплён по версии и дайджесту — сборка воспроизводима.
#
# Семантический поиск по справочнику (опционально): docker build --build-arg WITH_EMBEDDINGS=1 .
# Тогда в образ добавляются onnxruntime и tokenizers и модель rubert-tiny2 в ONNX (/srv/models/rubert-tiny2).
# По умолчанию WITH_EMBEDDINGS=0 — образ такой же, как без этой функции.
ARG PY_IMAGE=python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ARG WITH_EMBEDDINGS=0

# --- стадия model: rubert-tiny2 → ONNX (torch нужен только здесь и в итоговый образ не попадает)
FROM ${PY_IMAGE} AS model
ARG WITH_EMBEDDINGS
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HF_HOME=/tmp/hf HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /build
COPY requirements-ml.txt requirements-ml-build.txt ./
RUN if [ "$WITH_EMBEDDINGS" = "1" ]; then pip install -r requirements-ml-build.txt; fi
COPY scripts/export_rubert_onnx.py ./scripts/
COPY app/__init__.py ./app/
COPY app/services/__init__.py app/services/search.py app/services/semantic.py ./app/services/
RUN mkdir -p /models/rubert-tiny2 \
    && if [ "$WITH_EMBEDDINGS" = "1" ]; then python scripts/export_rubert_onnx.py /models/rubert-tiny2; fi

# --- образ сервиса
FROM ${PY_IMAGE}
ARG WITH_EMBEDDINGS

LABEL org.opencontainers.image.title="Перемена"       org.opencontainers.image.description="Бот-помощник классного руководителя в MAX"       org.opencontainers.image.source="https://github.com/dimon0804/peremena"       org.opencontainers.image.authors="dimon0804 <dimongamedeveloper@yandex.ru>"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_URL=sqlite+aiosqlite:////srv/data/peremena.db \
    EMBEDDINGS_MODEL_DIR=/srv/models/rubert-tiny2 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /srv

COPY requirements.txt requirements-ml.txt ./
RUN pip install -r requirements.txt \
    && if [ "$WITH_EMBEDDINGS" = "1" ]; then pip install -r requirements-ml.txt; fi
# при WITH_EMBEDDINGS=0 папка пустая
COPY --from=model /models /srv/models

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
