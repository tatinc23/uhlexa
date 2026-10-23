#!/usr/bin/env bash
# Run the Uhlexa voice worker (spec 22.4) on this machine.
# Usage: scripts/voice_worker.sh [dev|start|download-files]   (first run: download-files, then dev)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON312:-$(command -v python3.12 || echo /usr/local/opt/python@3.12/bin/python3.12)}"
if [ ! -x .venv-voice/bin/python ]; then "$PY" -m venv .venv-voice; fi
if [ ! -f .venv-voice/.installed ] || [ requirements-voice.txt -nt .venv-voice/.installed ]; then
  .venv-voice/bin/pip install -q --disable-pip-version-check -r requirements-voice.txt
  touch .venv-voice/.installed
fi
# .env then the gitignored secrets file; quote JSON values there, e.g. KEIKI_AGENT_IDS='{"alex":"<id>"}'
set -a
[ -f .env ] && . ./.env
[ -f .env.secrets.local ] && . ./.env.secrets.local
set +a
exec .venv-voice/bin/python -m app.voice.worker "${1:-dev}"
