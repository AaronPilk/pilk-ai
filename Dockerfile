FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY core ./core
COPY agents ./agents
COPY scripts ./scripts

RUN pip install --upgrade pip \
    && pip install .

RUN useradd --create-home --uid 10001 pilk \
    && mkdir -p /data \
    && chown -R pilk:pilk /app /data

USER pilk

ENV PILK_HOME=/data \
    PILK_HOST=0.0.0.0 \
    PILK_PORT=8080 \
    PILK_CLOUD=1

EXPOSE 8080

# Railway injects $PORT at runtime (dynamic per deploy). Settings.port
# treats $PORT as higher priority than $PILK_PORT, so `python -m
# core.main` binds to whichever platform sets the env. Shell form is
# required for the default-expansion syntax.
CMD python -m core.main
