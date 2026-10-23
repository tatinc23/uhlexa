FROM python:3.12-slim

# uv 0.13.4 (image tag verified on ghcr.io 2026-10-22)
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY . .

RUN useradd --uid 10001 --no-create-home uhlexa
USER uhlexa

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('APP_PORT', '8000'), timeout=2)"]

CMD ["scripts/entrypoint.sh"]
