#!/usr/bin/env bash
# Friday installer for Linux, macOS, and Windows (inside WSL; start from
# scripts/get.ps1 on Windows). Run from the folder you cloned Friday into:
#
#   scripts/install.sh          # asks before each step that changes your system
#   scripts/install.sh --yes    # accepts every recommended answer
#
# Answers can also be given up front (an AI agent installing Friday for
# someone passes what they chose; anything not given is asked, or with
# --yes takes the recommended answer):
#   --model=house-qwen|qwen3.6:35b-a3b|…|none   which local AI model, see --help
#   --autostart=yes|no    start Friday whenever the computer starts
#   --rename=yes|no       name the computer "friday" (for friday.local)
#   --docker=yes|no       install Docker if it's missing
#   --matter=yes|no       set up Matter lights (Linux only)
#
# Safe to run again: it skips whatever is already done and never overwrites
# config.toml, .env, or anything in data/. Works with the bash 3.2 that
# macOS ships, so no bash-4 features below.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$PWD"
YES=0
OPT_MODEL="" OPT_AUTOSTART="" OPT_RENAME="" OPT_DOCKER="" OPT_MATTER=""
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --model=*) OPT_MODEL="${arg#*=}" ;;
    --autostart=*) OPT_AUTOSTART="${arg#*=}" ;;
    --rename=*) OPT_RENAME="${arg#*=}" ;;
    --docker=*) OPT_DOCKER="${arg#*=}" ;;
    --matter=*) OPT_MATTER="${arg#*=}" ;;
    -h|--help)
      awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"
      if [[ -x .venv/bin/python ]]; then
        echo "Models:"
        .venv/bin/python -c 'from house_agent.providers import LOCAL_MODELS
for m in LOCAL_MODELS: print(f"  {m.name:16} {m.base:18} {m.size_gb:g} GB, {m.note}")'
      fi
      exit 0 ;;
    *) echo "Unknown option: $arg (see --help)"; exit 1 ;;
  esac
done
# Catch a mistyped model before anything is installed.
if [[ -n "$OPT_MODEL" && "$OPT_MODEL" != none ]] && ! grep -qF "\"$OPT_MODEL\"" src/house_agent/providers.py; then
  echo "Unknown --model=$OPT_MODEL (see --help)"; exit 1
fi
# Piped from curl with no terminal to ask on: take the recommended answers.
if [[ ! -t 0 ]] && ! { true </dev/tty; } 2>/dev/null; then YES=1; fi

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
ask() {  # ask "Question?" y|n [preset yes|no]  -> true for yes
  local answer default="${2:-y}" preset="${3:-}" hint="y/N"
  case "$preset" in [Yy]*) return 0 ;; [Nn]*) return 1 ;; esac
  [[ "$default" == y ]] && hint="Y/n"
  if (( YES )); then [[ "$default" == y ]]; return; fi
  read -r -p "  $1 [$hint] " answer </dev/tty || true
  answer="${answer:-$default}"
  case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}
sedi() {  # in-place sed on both GNU and BSD (macOS) sed
  local expr="$1" file="$2"
  sed "$expr" "$file" > "$file.tmp" && cat "$file.tmp" > "$file" && rm -f "$file.tmp"
}
local_ip() {
  if [[ "$OS" == mac ]]; then ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true
  else hostname -I 2>/dev/null | awk '{print $1}'; fi
}

# ---------------------------------------------------------------- 1. system
bold "1/9  Checking this computer"
case "$(uname -s)" in
  Linux) OS=linux ;;
  Darwin) OS=mac ;;
  *) echo "Unsupported system. On Windows, run scripts/get.ps1 in PowerShell instead."; exit 1 ;;
esac
WSL=0
grep -qi microsoft /proc/version 2>/dev/null && WSL=1
APT=0
have apt-get && APT=1
if [[ "$OS" == mac ]]; then ok "macOS $(sw_vers -productVersion) on $(uname -m)"
elif (( WSL )); then ok "Windows (WSL): ${WSL_DISTRO_NAME:-Linux}"
else ok "Linux: $(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-}")"; fi

if [[ "$OS" == mac ]] && ! have brew; then
  if ask "Homebrew (the standard macOS package manager) is needed. Install it from brew.sh?" y; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/tty
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
  else
    echo "Install Homebrew from https://brew.sh and run this again."; exit 1
  fi
fi

find_python() {
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if have "$candidate" && "$candidate" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      PY="$candidate"; return 0
    fi
  done
  return 1
}

# The few system packages Friday needs, installed only with permission.
if [[ "$OS" == mac ]]; then
  missing=""
  find_python || missing="$missing python@3.12"
  have git || missing="$missing git"
  have mkcert || missing="$missing mkcert"
  if [[ -n "$missing" ]] && ask "Install$missing with Homebrew?" y; then
    # shellcheck disable=SC2086
    brew install $missing
  fi
elif (( APT )); then
  missing=""
  have git || missing="$missing git"
  have curl || missing="$missing curl"
  have mkcert || missing="$missing mkcert libnss3-tools"
  have openssl || missing="$missing openssl"
  if (( ! WSL )) && ! have avahi-daemon; then missing="$missing avahi-daemon"; fi
  if ! find_python; then
    # Ubuntu 22.04 ships 3.10; 3.11 is in its standard repositories.
    if apt-cache show python3.11-venv >/dev/null 2>&1 && ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      missing="$missing python3.11 python3.11-venv"
    else
      missing="$missing python3 python3-venv"
    fi
  fi
  if [[ -n "$missing" ]] && ask "Install$missing with apt (needs your password)?" y; then
    sudo apt-get update -qq
    # shellcheck disable=SC2086
    sudo apt-get install -y -qq $missing
  fi
fi
find_python || { echo "Python 3.11 or newer is needed. Install it, then run this again."; exit 1; }
ok "Python: $($PY --version)"

# ---------------------------------------------------------------- 2. GPU + Docker
bold "2/9  Graphics card and Docker"
GPU=0 VRAM_GB=0
if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
  GPU=1
  VRAM_GB=$(( $(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' ') / 1024 ))
  ok "NVIDIA GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1) (${VRAM_GB} GB)"
elif [[ "$OS" == mac && "$(uname -m)" == arm64 ]]; then
  ok "Apple Silicon: the AI model runs on the built-in GPU"
else
  warn "No NVIDIA GPU found. The local AI will be slow; a cloud model can be picked in Settings."
fi

if ! have docker; then
  if [[ "$OS" == mac ]]; then
    if ask "Docker Desktop runs Friday's voice services. Install it with Homebrew?" y "$OPT_DOCKER"; then
      brew install --cask docker
      open -a Docker || true
      warn "Docker Desktop is opening. Accept its terms, then continue."
    fi
  elif ask "Docker runs Friday's voice services. Install it with Docker's official script (get.docker.com)?" y "$OPT_DOCKER"; then
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    NEW_DOCKER_GROUP=1
  fi
fi
DOCKER="docker"
if have docker && ! docker info >/dev/null 2>&1 && sudo -n true 2>/dev/null && sudo docker info >/dev/null 2>&1; then
  DOCKER="sudo docker"   # just added to the docker group; takes effect at next login
fi
if have docker && $DOCKER info >/dev/null 2>&1; then
  ok "Docker is running"
else
  warn "Docker isn't running yet. Voice replies need it; start Docker, then Friday."
fi

# GPU speech needs the NVIDIA Container Toolkit; otherwise speech runs on the CPU.
GPU_SPEECH=0
if (( GPU )) && have docker; then
  if $DOCKER info 2>/dev/null | grep -qi 'runtimes:.*nvidia'; then
    GPU_SPEECH=1
  elif (( APT )) && ask "Install the NVIDIA Container Toolkit so voice recognition uses the GPU?" y; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
    sudo apt-get update -qq && sudo apt-get install -y -qq nvidia-container-toolkit
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker 2>/dev/null || sudo service docker restart || true
    GPU_SPEECH=1
  fi
fi

# ---------------------------------------------------------------- 3. Python
bold "3/9  Friday itself"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .
ok "Installed into .venv"

# ---------------------------------------------------------------- 4. settings
bold "4/9  Settings files"
if [[ -f config.toml ]]; then ok "config.toml already exists (kept)"
else
  cp config.example.toml config.toml
  if (( ! GPU_SPEECH )); then
    sedi 's/"docker-compose.tts.yml"/"docker-compose.tts-cpu.yml"/' config.toml
  fi
  ok "Created config.toml"
fi
if [[ -f .env ]]; then ok ".env already exists (kept)"
else
  cp .env.example .env
  chmod 600 .env
  if (( ! GPU_SPEECH )); then
    sedi 's#^STT_MODEL=.*#STT_MODEL=Systran/faster-distil-whisper-small.en#' .env
  fi
  ok "Created .env (readable only by you)"
fi
if grep -q '^compose_files.*tts-cpu' config.toml; then ok "Voice recognition runs on the CPU"; fi
mkdir -p data/models data/certs data/mosquitto/config data/zigbee2mqtt
[[ -f data/mosquitto/config/mosquitto.conf ]] || cp scripts/templates/mosquitto.conf data/mosquitto/config/
[[ -f data/zigbee2mqtt/configuration.yaml ]] || cp scripts/templates/zigbee2mqtt.yaml data/zigbee2mqtt/configuration.yaml

# ---------------------------------------------------------------- 5. voice ID
bold "5/9  Voice recognition model (26 MB)"
MODEL=data/models/wespeaker_en_voxceleb_resnet34_LM.onnx
if [[ -f "$MODEL" ]]; then ok "Already downloaded"
elif curl -fL --progress-bar -o "$MODEL.part" \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx; then
  mv "$MODEL.part" "$MODEL"; ok "Downloaded"
else
  warn "Download failed; Friday works, but won't tell voices apart until $MODEL exists"
fi

# ---------------------------------------------------------------- 6. AI model
bold "6/9  Local AI model"
# The offered models come from house_agent/providers.py (LOCAL_MODELS), the
# same list Settings > Assistant model offers to download.
MODELS=()
while IFS= read -r line; do MODELS+=("$line"); done < <(.venv/bin/python -c '
from house_agent.providers import LOCAL_MODELS
for m in LOCAL_MODELS:
    print("|".join([m.name, m.base, f"{m.size_gb:g}", m.needs, m.note]))')
COUNT=${#MODELS[@]}
NONE=$(( COUNT + 1 ))
field() { echo "$1" | cut -d'|' -f"$2"; }

# What fits this machine: the first model (qwen3:14b) wants ~12 GB of GPU
# memory or a Mac with 24 GB+; anything smaller gets the second (qwen3:8b).
RECOMMEND=1
if [[ "$OS" == mac ]]; then
  MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  (( MEM_GB < 24 )) && RECOMMEND=2
elif (( ! GPU )) || (( VRAM_GB < 12 )); then
  RECOMMEND=2
fi
HAVE_MODEL=""
if have ollama && ollama list >/dev/null 2>&1; then
  HAVE_MODEL="$(ollama list | awk '/^house-/ {print $1}' | head -1)"
fi

CHOICE=$NONE
if [[ -n "$OPT_MODEL" ]]; then
  # Chosen up front: a house-* name, its base model, or "none".
  CHOICE=""
  if [[ "$OPT_MODEL" == none ]]; then CHOICE=$NONE; fi
  i=1
  for m in "${MODELS[@]}"; do
    if [[ "$OPT_MODEL" == "$(field "$m" 1)" || "$OPT_MODEL" == "$(field "$m" 2)" ]]; then CHOICE=$i; fi
    i=$(( i + 1 ))
  done
  [[ -n "$CHOICE" ]] || { echo "Unknown --model=$OPT_MODEL (see --help)"; exit 1; }
elif [[ -n "$HAVE_MODEL" ]]; then
  ok "A Friday model is already installed: $HAVE_MODEL"
  ask "Download a different model as well?" n && CHOICE=""
else
  CHOICE=""
fi
if [[ -z "$CHOICE" ]]; then
  echo "  Friday's AI runs on this computer through Ollama (free, private). Which model?"
  i=1
  for m in "${MODELS[@]}"; do
    extra=""
    [[ $i == "$RECOMMEND" ]] && extra=$'  \033[32m← recommended for this computer\033[0m'
    printf '    %d) %-16s %4s GB  %s; needs a %s%b\n' "$i" "$(field "$m" 2)" "$(field "$m" 3)" \
      "$(field "$m" 5)" "$(field "$m" 4)" "$extra"
    i=$(( i + 1 ))
  done
  echo "    $NONE) None for now: use a cloud model in Settings, or download one there later"
  (( ! GPU )) && [[ "$OS" != mac ]] && warn "Without a GPU every local model is slow; $NONE plus a cloud model may suit you better."
  if (( YES )); then CHOICE=$RECOMMEND
  else
    while :; do
      read -r -p "  Choose 1-$NONE [$RECOMMEND]: " CHOICE </dev/tty || CHOICE=$NONE
      CHOICE="${CHOICE:-$RECOMMEND}"
      if [[ "$CHOICE" =~ ^[0-9]+$ ]] && (( CHOICE >= 1 && CHOICE <= NONE )); then break; fi
      echo "  Type a number from 1 to $NONE."
    done
  fi
fi

if (( CHOICE < NONE )); then
  PICK="${MODELS[$(( CHOICE - 1 ))]}"
  NAME="$(field "$PICK" 1)" BASE="$(field "$PICK" 2)"
  if ! have ollama; then
    echo "  Installing Ollama (ollama.com), which runs the model."
    if [[ "$OS" == mac ]]; then brew install ollama && brew services start ollama
    else curl -fsSL https://ollama.com/install.sh | sh; fi
  fi
  for _ in 1 2 3 4 5 6 7 8 9 10; do ollama list >/dev/null 2>&1 && break; sleep 2; done
  if ! ollama list >/dev/null 2>&1; then
    warn "Ollama isn't running. Start it (the Ollama app, or 'ollama serve'), then run this again."
  else
    ollama pull "$BASE"
    # Friday's build: the same model with a 32k-token window (Ollama's default
    # is too small for Friday's prompt and tools, and it cuts the front off).
    printf 'FROM %s\nPARAMETER num_ctx 32768\n' "$BASE" > "data/$NAME.Modelfile"
    ollama create "$NAME" -f "data/$NAME.Modelfile"
    if grep -q '^LLM_MODEL=' .env; then sedi "s/^LLM_MODEL=.*/LLM_MODEL=$NAME/" .env
    else echo "LLM_MODEL=$NAME" >> .env; fi
    ok "Friday will use $NAME ($BASE). Switch any time in Settings > Assistant model."
  fi
elif [[ -z "$HAVE_MODEL" ]]; then
  warn "No local model. Choose a cloud model, or download one, in Settings > Assistant model."
fi

# ---------------------------------------------------------------- 7. network name + HTTPS
bold "7/9  friday.local and HTTPS"
if [[ "$OS" == mac ]]; then
  if [[ "$(scutil --get LocalHostName 2>/dev/null)" != friday ]] \
    && ask "Name this Mac 'friday' on the network, so phones can open https://friday.local:8787?" y "$OPT_RENAME"; then
    sudo scutil --set LocalHostName friday && ok "This Mac is now friday.local"
  fi
elif (( WSL )); then
  warn "On Windows, open Friday at the PC's IP address (https://<ip>:8787); see the README."
else
  if [[ "$(hostname)" != friday ]] \
    && ask "Rename this computer to 'friday', so phones can open https://friday.local:8787?" y "$OPT_RENAME"; then
    sudo hostnamectl set-hostname friday && ok "Hostname is now friday"
  fi
  if have systemctl && ! systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    warn "avahi-daemon isn't running, so friday.local won't work: sudo apt install avahi-daemon"
  fi
fi
if [[ -f data/certs/cert.pem ]]; then ok "Certificate already made"
else scripts/make-cert.sh; fi

# ---------------------------------------------------------------- 8. lights
bold "8/9  Smart lights"
if [[ "$OS" == linux ]] && (( ! WSL )); then
  if ask "Set up Matter now (most new Wi-Fi smart bulbs and plugs)?" n "$OPT_MATTER"; then
    have uv || curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    uv tool install --python 3.12 "python-matter-server[server]"
    [[ -d /data ]] || { sudo mkdir -p /data && sudo chown "$USER" /data; }
    sedi '/^\[matter\]/,/^\[/ s/^enabled = false/enabled = true/' config.toml
    ok "Matter enabled"
  fi
  echo "  Zigbee needs a USB dongle: see 'Zigbee' in the README."
else
  echo "  Matter and Zigbee lights need a Linux computer for now (see the README)."
fi

# ---------------------------------------------------------------- 9. autostart
bold "9/9  How should Friday run?"
echo "  • Automatically: starts by itself whenever this computer starts, and restarts if it crashes."
echo "  • By hand: only when you run .venv/bin/house-agent in this folder."
if ask "Start Friday automatically?" y "$OPT_AUTOSTART"; then
  scripts/autostart.sh on || warn "Couldn't set up autostart here; run Friday by hand instead."
  AUTO=1
else
  AUTO=0
  ok "Run it by hand. You can turn on autostart later with: scripts/autostart.sh on"
fi

bold "Done!"
IP="$(local_ip)"
[[ -n "$IP" ]] || IP="THIS-COMPUTERS-IP"
if (( AUTO )); then echo "  Friday is starting. Logs: scripts/autostart.sh logs"
else echo "  Start Friday:  .venv/bin/house-agent"; fi
if (( WSL )); then echo "  Then open:     https://$IP:8787"
else echo "  Then open:     https://friday.local:8787   (or https://$IP:8787)"; fi
echo "  Pairing code:  shown in the log on first start, and saved in .env as API_TOKEN"
if [[ -n "${NEW_DOCKER_GROUP:-}" ]]; then
  warn "Log out and back in (WSL: run 'wsl --shutdown' in Windows) so Docker works without sudo."
fi
