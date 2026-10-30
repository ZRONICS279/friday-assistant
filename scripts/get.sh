#!/usr/bin/env bash
# One-line install for Linux and macOS:
#
#   curl -fsSL https://raw.githubusercontent.com/knightlie-code/friday/main/scripts/get.sh | bash
#
# Downloads Friday into ~/friday (or $FRIDAY_DIR), then runs
# scripts/install.sh, which asks before every step that changes your system.
# Add "-s -- --yes" after "bash" to accept the recommended answers.
# Run it again to update an existing copy and re-check the setup.
set -euo pipefail
REPO="${FRIDAY_REPO:-https://github.com/knightlie-code/friday.git}"
DIR="${FRIDAY_DIR:-$HOME/friday}"

if ! command -v git >/dev/null 2>&1; then
  echo "Friday needs git."
  if [[ "$(uname -s)" == Darwin ]]; then
    echo "Installing Apple's command-line tools (includes git). Finish that window, then run this again."
    xcode-select --install || true
    exit 1
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq git
  else
    echo "Install git with your package manager, then run this again."; exit 1
  fi
fi

if [[ -d "$DIR/.git" ]]; then
  echo "Updating Friday in $DIR"
  git -C "$DIR" pull --ff-only
else
  echo "Downloading Friday into $DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi

# Hand over a real terminal so the installer can ask its questions, even
# though this script itself arrived through a pipe.
if { true </dev/tty; } 2>/dev/null; then
  exec "$DIR/scripts/install.sh" "$@" </dev/tty
else
  exec "$DIR/scripts/install.sh" --yes "$@"
fi
