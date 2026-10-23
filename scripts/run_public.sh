#!/usr/bin/env bash
# Run Uhlexa on this Mac behind the Inkbox tunnel (docs/HOSTING.md). Replaces the Manufact deploy.
#   scripts/run_public.sh            app on Neon (NEON_DATABASE_URL) + tunnel https://uhlexa.inkboxwire.com
#   DATABASE_URL=sqlite:///./voice-preview.db scripts/run_public.sh   local SQLite instead (rehearsal)
# Keys come from .env and the gitignored .env.secrets.local. Ctrl-C stops both processes.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
[ -f .env ] && . ./.env
[ -f .env.secrets.local ] && . ./.env.secrets.local
set +a

export INKBOX_TUNNEL_HANDLE="${INKBOX_TUNNEL_HANDLE:-uhlexa}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://${INKBOX_TUNNEL_HANDLE}.inkboxwire.com}"
export DATABASE_URL="${DATABASE_URL:-${NEON_DATABASE_URL:-}}"
export APP_PORT="${APP_PORT:-8000}"
export EMBEDDED_WORKER="${EMBEDDED_WORKER:-true}"  # texts are answered by a job; run it in-process
if [ -z "$DATABASE_URL" ]; then
  echo "No database: put NEON_DATABASE_URL (Neon pooled URL) in .env.secrets.local." >&2
  exit 1
fi
if [ -z "${DEMO_ACCESS_TOKEN:-}" ]; then
  DEMO_ACCESS_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))')"
  ( umask 077; printf '\n# Access link token for the public URL (scripts/run_public.sh)\nDEMO_ACCESS_TOKEN=%s\n' "$DEMO_ACCESS_TOKEN" >> .env.secrets.local )
  export DEMO_ACCESS_TOKEN
fi

.venv/bin/python -c "from app.db.migrate import upgrade_to_head; upgrade_to_head()"
.venv/bin/uvicorn app.hosting.public:app --host 127.0.0.1 --port "$APP_PORT" --proxy-headers --forwarded-allow-ips 127.0.0.1 &
APP_PID=$!
trap 'kill "$APP_PID" 2>/dev/null || true' EXIT

echo
echo "Public URL:   $PUBLIC_BASE_URL"
echo "Access link:  $PUBLIC_BASE_URL/access?token=$DEMO_ACCESS_TOKEN&next=/voice/hub.html"
echo "Local:        http://127.0.0.1:$APP_PORT/access?token=$DEMO_ACCESS_TOKEN&next=/voice/hub.html"
echo "Keiki MCP:    $PUBLIC_BASE_URL/mcp"
echo
.venv/bin/python -m app.hosting.tunnel
