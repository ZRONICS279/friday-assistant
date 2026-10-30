---
name: install-friday
description: Install, set up and verify Friday, a private self-hosted home assistant, on the user's own computer (Linux, macOS, or Windows through WSL). Use when the user asks to install, set up, update or configure Friday, or to connect their own AI model or agent as Friday's brain.
---

# Install Friday for the user

Friday is a private home assistant (voice, chat, TVs, lights, calendar, lists)
that runs on the user's own computer. Repository:
https://github.com/knightlie-code/friday. The README there is the full manual;
this file is the short path for an agent.

## Ground rules

- **Ask before changing the system.** Installing packages, Docker, Ollama or a
  multi-GB model, renaming the computer, and adding a startup service all need
  the user's yes. Collect the answers in one round of questions (step 2), not
  one at a time.
- **Keys are secrets.** Write API keys only into `~/friday/.env` (git ignores
  it). Never echo them back, commit them, or paste them into logs.
- **Never expose Friday to the internet.** Don't forward port 8787 or open it on
  a public firewall. For access away from home, suggest a VPN such as Tailscale.
- **Report what actually happened.** If a step fails, show the error and stop
  rather than guessing.

## 1. Look at the computer

Run these and note the results. Nothing here changes anything.

```bash
uname -s; uname -m
cat /etc/os-release 2>/dev/null | head -2          # Linux
sw_vers 2>/dev/null                                # macOS
grep -qi microsoft /proc/version 2>/dev/null && echo "WSL"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null
sysctl -n hw.memsize 2>/dev/null                   # macOS RAM in bytes
command -v docker ollama git python3
```

- **Windows (not inside WSL):** Friday runs inside WSL. Ask the user to open
  **Terminal (Admin)** and run the command below themselves, because it needs
  an administrator prompt and may need a restart. Then continue from step 3
  *inside Ubuntu*. Or stop there, since `get.ps1` runs the whole installer.
  ```powershell
  irm https://raw.githubusercontent.com/knightlie-code/friday/main/scripts/get.ps1 | iex
  ```
- **Unsupported:** anything that isn't Linux, macOS or WSL. Say so.

## 2. Ask the user (one message)

Recommend a model from the hardware: an NVIDIA GPU with 12 GB+ or a Mac with
24 GB+ RAM means `house-qwen`, anything smaller means `house-qwen-8b`. With no
GPU on Linux, local models are slow, so suggest `none` plus a cloud model or
the user's own endpoint (step 6).

| `--model=` | Base model | Download | Needs |
|---|---|---|---|
| `house-qwen` | qwen3:14b | 9.3 GB | 12 GB+ GPU / 24 GB+ Mac. Best balance, the default |
| `house-qwen-8b` | qwen3:8b | 5.2 GB | 8 GB+ GPU / 16 GB+ Mac |
| `house-qwen3.6` | qwen3.6:35b-a3b | 22 GB | 24 GB+ GPU / 32 GB+ Mac. Mixture of experts, quick for its size |
| `house-qwen3.8` | qwen3.8:27b | 17 GB | 20 GB+ GPU / 32 GB+ Mac. Newest, a bit slow |
| `house-gpt` | gpt-oss:20b | 13 GB | 16 GB+ GPU / 32 GB+ Mac |
| `house-qwen-32b` | qwen3:32b | 20 GB | 24 GB+ GPU / 48 GB+ Mac |
| `none` | | | Use a cloud model or their own endpoint instead |

Ask:
1. Which model (give your recommendation and why).
2. Start Friday automatically when the computer starts, or only by hand?
3. Rename the computer to `friday` so phones can open `https://friday.local:8787`? (Not on WSL; there they use the PC's IP address.)
4. Install Docker if it's missing? It's needed for voice replies. Say yes unless they object.
5. Linux only: set up Matter smart lights now?
6. Optional features and keys they already have or want: movie search (TMDB,
   Watchmode), live traffic (TomTom), flights (LetsFG), their Google/iCloud/Outlook
   calendar links, DoorDash. Mention that without a key Friday can answer
   those from a web search instead (a per-feature switch in Settings), so none
   of this is required.
7. Whether they want an AI service or agent they already use to be Friday's
   brain (step 6).

## 3. Install

The installer needs `sudo` for system packages. If your shell can't answer a
password prompt, ask the user to run `sudo -v` in their own terminal first
(which caches it for a few minutes), or to run the command below themselves.

```bash
curl -fsSL https://raw.githubusercontent.com/knightlie-code/friday/main/scripts/get.sh \
  | bash -s -- --yes --model=<choice> --autostart=<yes|no> --rename=<yes|no> --docker=<yes|no> --matter=<yes|no>
```

This clones Friday into `~/friday` (or updates it) and runs
`scripts/install.sh`. `--yes` takes the recommended answer for anything not
given as a flag. `~/friday/scripts/install.sh --help` lists every option. The
run takes 10 to 30 minutes, mostly the model download. Let it finish.

If it stops with an error, read the last lines, fix the cause if it's
clear (a missing package, Docker not running), and run the same command
again. It skips what's already done.

## 4. Keys and settings (only what the user gave you)

Edit `~/friday/.env`: set a value on its existing line, or add the line.

| Feature | Line in `.env` |
|---|---|
| Movie & show search, recommendations | `TMDB_READ_ACCESS_TOKEN=` (the long "API Read Access Token" from themoviedb.org → Settings → API) |
| Open titles straight in the streaming app | `WATCHMODE_API_KEY=` (api.watchmode.com) |
| Live traffic | `TOMTOM_API_KEY=` (developer.tomtom.com) |
| Their calendars (read-only) | `CALENDAR_ICS_URLS=` (comma-separated private iCal links) |
| Wi-Fi for new Matter devices | `HOUSE_WIFI_SSID=` and `HOUSE_WIFI_PASSWORD=` |
| Home location (else Friday asks once) | `HOUSE_LATITUDE=` and `HOUSE_LONGITUDE=` |

- **Flights:** run `~/friday/.venv/bin/pip install letsfg && ~/friday/.venv/bin/letsfg auth`. It's interactive, so hand it to the user.
- **DoorDash:** see the README section "DoorDash". It needs the user to sign in through a browser.

Then restart: `~/friday/scripts/autostart.sh restart` if autostart is on;
otherwise start it with `cd ~/friday && .venv/bin/house-agent` (leave it
running in the background or in a terminal the user can see).

## 5. Check it works, then hand over

```bash
curl -sk https://127.0.0.1:8787/api/health        # expect "status":"ok"
grep '^API_TOKEN=' ~/friday/.env | cut -d= -f2     # the pairing code (appears after the first start)
```

- The first start downloads the voice services (a few GB), so allow a minute or two.
- `"model"` in the health output should be the chosen `house-…` model, or the
  cloud/custom one.

Tell the user:
- Open **https://friday.local:8787** on any phone or computer on the home Wi-Fi.
  If that doesn't work, or on Windows, use `https://<this computer's IP>:8787`.
- The browser warns about the certificate the first time: choose *Advanced →
  Proceed*. The README's "Trust Friday's certificate" section removes the
  warning for good.
- Type the pairing code when asked. Then tap **Talk**, or type.
- Useful commands: `scripts/autostart.sh status|logs|restart`, and
  `scripts/update.sh` to update.

## 6. Optional: the user's own AI as Friday's brain

Friday can use any service that speaks the **OpenAI chat-completions API with
tool calling**. Friday controls the house by calling tools, so the endpoint must
return Friday's tool calls, not run its own. Friday tests this when switching
and refuses an endpoint that only chats.

Works:
- A model API or server: Hermes models through Nous Portal or OpenRouter,
  LM Studio (`http://<host>:1234/v1`), vLLM (`http://<host>:8000/v1`),
  llama.cpp's server (`http://<host>:8080/v1`), Ollama on a stronger PC
  (`http://<host>:11434/v1`), Groq, Together, and others.
- An agent gateway (such as OpenClaw, or Hermes Agent's API server), but only
  if its OpenAI-compatible endpoint passes the caller's tools through to the
  model. Check that gateway's docs. Friday's test tells you either way.

Doesn't work: ChatGPT, Claude.ai, Claude Code, Codex or Cursor
*subscriptions*. They have no API a program can call. An API key from OpenAI,
Anthropic or OpenRouter does work, and those are built into Settings already.

To switch, the user picks **Settings → Assistant model → Custom endpoint**
and fills in the address, model name and key. Or, with their explicit consent
(conversations go to that endpoint):

```bash
TOKEN=$(grep '^API_TOKEN=' ~/friday/.env | cut -d= -f2)
curl -sk -X PUT https://127.0.0.1:8787/api/assistant-model \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"provider":"custom","base_url":"http://192.168.0.20:8000/v1","model":"<model name>","api_key":null,"accept_cloud":true}'
```

A `400` explains what failed: unreachable, bad key, unknown model, or no tool
calls. Friday falls back to its local model for any turn where the endpoint
errors.

## Updating later

```bash
~/friday/scripts/update.sh
```
