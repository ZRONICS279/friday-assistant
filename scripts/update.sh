#!/usr/bin/env bash
# Update Friday to the latest version:
#
#   scripts/update.sh
#
# Pulls the new code, installs any new Python packages, and restarts Friday.
# config.toml, .env and data/ are yours and never touched.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "You have local changes to Friday's code:"
  git status --short --untracked-files=no
  echo "Commit or stash them first (git stash), then run this again."
  exit 1
fi

before="$(git rev-parse HEAD)"
git pull --ff-only
after="$(git rev-parse HEAD)"
if [[ "$before" == "$after" ]]; then
  echo "Already up to date."
  exit 0
fi

echo "Updating $(git rev-parse --short "$before") → $(git rev-parse --short "$after")"
git --no-pager log --oneline "$before..$after" | head -20

.venv/bin/pip install --quiet -e .


if git diff --name-only "$before" "$after" | grep -q '^config.example.toml$'; then
  echo "Note: config.example.toml changed. Compare it with your config.toml for new options:"
  echo "  diff config.toml config.example.toml"
fi

if scripts/autostart.sh status 2>/dev/null | grep -q '^Autostart: on'; then
  scripts/autostart.sh restart
else
  echo "Restart Friday to use the new version (Ctrl-C it, then .venv/bin/house-agent)."
fi
