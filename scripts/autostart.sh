#!/usr/bin/env bash
# Run Friday automatically at startup, or only when you start it yourself.
#
#   scripts/autostart.sh on        start Friday now and every time the computer starts
#   scripts/autostart.sh off       stop it and don't start it automatically any more
#   scripts/autostart.sh status    is it set up, and is it running?
#   scripts/autostart.sh restart   restart the automatic copy (after changing settings)
#   scripts/autostart.sh logs      follow its log
#
# Linux (and Windows through WSL) use a systemd user service; macOS uses a
# launchd agent. With autostart off, run Friday by hand from this folder:
#   .venv/bin/house-agent
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$PWD"
ACTION="${1:-status}"

LABEL="friday.house-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
MAC_LOG="$HOME/Library/Logs/Friday/house-agent.log"
UNIT="$HOME/.config/systemd/user/house-agent.service"
WSL=0
grep -qi microsoft /proc/version 2>/dev/null && WSL=1

mac_plist() {
  local path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/.venv/bin/house-agent</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$path</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$MAC_LOG</string>
  <key>StandardErrorPath</key><string>$MAC_LOG</string>
</dict>
</plist>
EOF
}

case "$(uname -s)" in
Darwin)
  case "$ACTION" in
  on)
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$MAC_LOG")"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    mac_plist > "$PLIST"
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Friday now starts automatically when you log in, and is starting now."
    echo "Also open Docker Desktop > Settings > General > 'Start Docker Desktop when you sign in'."
    echo "To start at power-on without anyone logging in, turn on automatic login in System Settings > Users & Groups."
    ;;
  off)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Friday stopped and won't start automatically. Run it by hand with: .venv/bin/house-agent"
    ;;
  restart) launchctl kickstart -k "gui/$(id -u)/$LABEL" && echo "Friday restarted." ;;
  logs) touch "$MAC_LOG"; tail -f "$MAC_LOG" ;;
  status)
    if [[ -f "$PLIST" ]]; then
      echo "Autostart: on"
      launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E '^\s*state =' || echo "Not running"
    else
      echo "Autostart: off (run by hand with .venv/bin/house-agent)"
    fi
    ;;
  *) echo "Usage: $0 on|off|status|restart|logs"; exit 1 ;;
  esac
  ;;
Linux)
  if ! command -v systemctl >/dev/null || [[ ! -d /run/systemd/system ]]; then
    echo "This system doesn't run systemd, so autostart can't be set up here."
    (( WSL )) && echo "In WSL, add '[boot]' and 'systemd=true' to /etc/wsl.conf, then run 'wsl --shutdown' in Windows."
    exit 1
  fi
  case "$ACTION" in
  on)
    mkdir -p "$(dirname "$UNIT")"
    sed "s#@FRIDAY_DIR@#$DIR#g" scripts/house-agent.service > "$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable --now house-agent
    # Start at boot, not only once somebody logs in.
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" || true
    echo "Friday now starts automatically when the computer starts, and is running now."
    if (( WSL )); then
      echo "On Windows, WSL itself must also start at sign-in. scripts/get.ps1 sets that up,"
      echo "or run in PowerShell:  wsl -d ${WSL_DISTRO_NAME:-Ubuntu} --exec sleep infinity"
    fi
    ;;
  off)
    systemctl --user disable --now house-agent 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Friday stopped and won't start automatically. Run it by hand with: .venv/bin/house-agent"
    ;;
  restart) systemctl --user restart house-agent && echo "Friday restarted." ;;
  logs) journalctl --user -u house-agent -f ;;
  status)
    if [[ -f "$UNIT" ]]; then
      echo "Autostart: on ($(systemctl --user is-active house-agent 2>/dev/null || true))"
    else
      echo "Autostart: off (run by hand with .venv/bin/house-agent)"
    fi
    ;;
  *) echo "Usage: $0 on|off|status|restart|logs"; exit 1 ;;
  esac
  ;;
*) echo "Unsupported system: $(uname -s)"; exit 1 ;;
esac
