#!/usr/bin/env bash
# Push this directory to github.com/seb7152/finary-mcp
#
# Two paths depending on whether you have `gh` CLI or not.
# Run from inside the finary-mcp/ directory.

set -euo pipefail

REPO="seb7152/finary-mcp"
VISIBILITY="public"   # change to "private" if you prefer

cd "$(dirname "$0")"

# Init git if not already done
if [ ! -d .git ]; then
  git init -b main
  git add -A
  git commit -m "Initial commit: Finary MCP server"
fi

if command -v gh >/dev/null 2>&1; then
  echo "→ Using GitHub CLI"
  # gh repo create handles auth, repo creation, remote and push in one go.
  # If the repo already exists, this will fail — re-run with --source=. --push only:
  #   gh repo create "$REPO" --"$VISIBILITY" --source=. --remote=origin --push
  gh repo create "$REPO" --"$VISIBILITY" --source=. --remote=origin --push --description "Unofficial MCP server exposing Finary patrimoine data, deployed alongside obsidian-headless-mcp"
else
  echo "→ No gh CLI found. Falling back to plain git."
  echo
  echo "Step 1 — create the repo on GitHub manually:"
  echo "  https://github.com/new"
  echo "  Owner: seb7152   Name: finary-mcp   Visibility: $VISIBILITY"
  echo "  Do NOT add a README, .gitignore or license (we have them already)."
  echo
  read -rp "Press Enter once the empty repo is created on GitHub..."
  git remote remove origin 2>/dev/null || true
  git remote add origin "git@github.com:${REPO}.git"
  git branch -M main
  git push -u origin main
fi

echo
echo "✓ Repo pushed: https://github.com/${REPO}"
echo
echo "Next steps on the Hostinger VPS:"
echo "  mkdir -p /docker/finary-mcp && cd /docker/finary-mcp"
echo "  git clone https://github.com/${REPO}.git ."
echo "  cp .env.example .env  # fill DOMAIN, FINARY_EMAIL, FINARY_PASSWORD, API_TOKEN"
echo "  docker compose up -d"
echo "  docker compose exec finary-mcp python -m finary_uapi signin"
echo "  docker compose restart finary-mcp"
