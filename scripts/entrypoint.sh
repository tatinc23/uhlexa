#!/bin/sh
# Container entrypoint: migrate, optionally seed the demo household, then serve.
set -eu

alembic upgrade head

case "${SEED_DEMO_DATA:-false}" in
  true | True | TRUE | 1 | yes) python -m app.seed ;;
esac

exec uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8000}" --proxy-headers
