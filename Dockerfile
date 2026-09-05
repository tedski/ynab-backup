# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY ynab_backup/ ./ynab_backup/
RUN uv sync --frozen --no-dev

RUN groupadd --system --gid 10001 ynab && \
    useradd  --system --uid 10001 --gid ynab --no-create-home --shell /usr/sbin/nologin ynab

USER 10001:10001

ENTRYPOINT ["/app/.venv/bin/python", "-m", "ynab_backup"]
CMD ["backup"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os; os.kill(1, 0)" || exit 1
