#!/usr/bin/env bash
# Build the clean public copy of Uhlexa for the hackathon: one fresh commit, no git history, no internal notes.
#
#   scripts/make_public_copy.sh <git-ref> <out-dir> [remote-url]
#
# Exports <git-ref> with `git archive` (tracked files only, so .env*, .venv and local secrets never come along),
# drops internal working notes, replaces password-manager item ids, adds LICENSE, runs gitleaks, and makes a
# single commit. It never pushes: review the copy, then `git -C <out-dir> push -u origin main` yourself.
set -euo pipefail

ref="${1:?usage: make_public_copy.sh <git-ref> <out-dir> [remote-url]}"
out="${2:?usage: make_public_copy.sh <git-ref> <out-dir> [remote-url]}"
remote="${3:-}"
repo_root="$(git rev-parse --show-toplevel)"

if [ -e "$out" ] && [ -n "$(ls -A "$out" 2>/dev/null)" ]; then
  echo "refusing: $out exists and is not empty" >&2
  exit 1
fi
mkdir -p "$out"
git -C "$repo_root" archive --format=tar "$ref" | tar -x -C "$out"

# Internal working notes: agent handoffs, prompts for other tools, task/blocker/decision logs, agent config.
internal=(.claude docs/handoffs docs/prompts TASKS.md BLOCKERS.md DECISIONS.md AGENTS.md CLAUDE.md)
for path in "${internal[@]}"; do
  rm -rf "${out:?}/$path"
done

# Password-manager item ids are labels, not secrets, but they are internal: replace them wherever they remain.
grep -rlE '1Password' "$out" --include='*.md' 2>/dev/null | while read -r file; do
  sed -E -i '' 's/`[a-z0-9]{26}`/`<password-manager item>`/g' "$file"
done
# Personal details that crept into working notes: vault name, local paths, the demo agent's real iMessage line.
grep -rlE 'team vault|~|\<Uhlexa iMessage line>' "$out" --exclude=uv.lock 2>/dev/null | while read -r file; do
  sed -E -i '' -e 's/team vault/team vault/g' -e 's#~#~#g' \
    -e 's/\<Uhlexa iMessage line>/<Uhlexa iMessage line>/g' "$file"
done

[ -f "$out/LICENSE" ] || cp "$repo_root/LICENSE" "$out/LICENSE"

report="$(mktemp "${TMPDIR:-/tmp}/uhlexa-gitleaks.XXXXXX")"
gitleaks detect --no-git --source "$out" --redact --no-banner -f json -r "$report" >/dev/null 2>&1 || true
python3 - "$report" <<'PY'
import json, sys
findings = json.load(open(sys.argv[1]))
# Test files use obvious placeholder keys on purpose (they prove secrets never leak into responses); everything
# else is a stop.
real = [f for f in findings if "/tests/" not in f["File"]]
for f in real:
    print(f"gitleaks: {f['File']}:{f['StartLine']} ({f['RuleID']})", file=sys.stderr)
sys.exit(1 if real else 0)
PY
rm -f "$report"

git -C "$out" init -q -b main
git -C "$out" add -A
git -C "$out" commit -q -m "Uhlexa: a household Alexa+ upgrade (Build, Ship, Shape hackathon submission)"
if [ -n "$remote" ]; then
  git -C "$out" remote add origin "$remote"
fi
echo "public copy ready: $out ($(git -C "$out" ls-files | wc -l | tr -d ' ') files, one commit)"
echo "review it, then: git -C \"$out\" push -u origin main"
