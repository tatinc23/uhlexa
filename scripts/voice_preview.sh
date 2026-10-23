#!/usr/bin/env bash
# Local app for trying the voice pages (spec 22): SQLite file DB, migrations, seeded Parker family, voice env loaded.
# Usage: scripts/voice_preview.sh [port]   then open http://localhost:<port>/voice/echo (worker: scripts/voice_worker.sh dev)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
[ -f .env ] && . ./.env
[ -f .env.secrets.local ] && . ./.env.secrets.local
set +a
export APP_ENV=development SEED_DEMO_DATA=true EMBEDDED_WORKER="${EMBEDDED_WORKER:-true}"
export DATABASE_URL="${VOICE_PREVIEW_DATABASE_URL:-sqlite:///./voice-preview.db}"
.venv/bin/python -c "from app.db.migrate import upgrade_to_head; upgrade_to_head()"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${1:-8765}"
