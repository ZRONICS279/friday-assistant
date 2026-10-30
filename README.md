# Friday

**A private home assistant that runs on your own computer.**

Friday is a voice and chat assistant for your home. It controls your TV,
lights and plugs, keeps your calendar, lists and reminders, and answers
questions. The AI runs locally, so your conversations, your voice and
everything Friday learns about your household stay in your house. There is
no account, no subscription and no cloud service in between.

> Say *"Friday, put The Bear on"* and it opens Hulu on the living-room TV,
> picks your profile and starts the episode.

- 🔒 **Private by design.** The AI model, speech recognition, voice ID and
  all your data run and live on your own machine.
- 🧑‍🤝‍🧑 **Knows who's talking.** Recognizes each person's voice, and
  follows their own rules, tastes and way of being spoken to.
- 📺 **Controls real TVs.** Roku, LG, Samsung, Vizio, Android/Google TV,
  Fire TV and Apple TV. Plays shows by name, switches inputs, and works as a
  remote.
- 💡 **Lights and plugs.** Matter and Zigbee devices by room: "dim the
  bedroom to 20", "all lights off", "fade the kitchen in at 7am".
- 🗓️ **Runs the day.** Reminders, routines, shopping lists, recipes, weather,
  news and directions. Plans you tell Friday show up in your phone's calendar,
  and it reads your Google, iCloud or Outlook calendars too.
- 🍔 **Orders food.** DoorDash search, carts and checkout, but only after it
  reads back the total and hears a clear yes.
- 📱 **One app for every screen.** A fast web app for phones and computers.
  Add it to your home screen; nothing to install from an app store.

---

## Contents

- [What you need](#what-you-need)
- [Install](#install)
- [Open Friday and talk to it](#open-friday-and-talk-to-it)
- [Running Friday (automatically or by hand)](#running-friday)
- [Local AI models](#local-ai-models)
- [Add your TV](#add-your-tv)
- [Add lights and plugs](#add-lights-and-plugs)
- [Friday's calendar on your phone](#fridays-calendar-on-your-phone)
- [Getting API keys (optional features)](#getting-api-keys-optional-features)
- [Updating](#updating)
- [Privacy: what stays home and what doesn't](#privacy-what-stays-home-and-what-doesnt)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What you need

A computer that stays on at home. Everyone else uses Friday from their phone
or browser.

| | Linux | macOS | Windows |
|---|---|---|---|
| **Supported** | Ubuntu 22.04+ / Debian 12+ (best) | macOS 13+, Apple Silicon recommended | Windows 11 22H2+ (runs in WSL) |
| **Local AI speed** | Fast with an NVIDIA GPU | Fast on M1/M2/M3/M4 with 16 GB+ | Fast with an NVIDIA GPU |
| **Voice** | ✓ | ✓ (voice recognition on the CPU) | ✓ |
| **TVs** | ✓ | ✓ | ✓ |
| **Matter & Zigbee lights** | ✓ | ✗ for now | ✗ for now |
| **friday.local address** | ✓ | ✓ | Use the PC's IP address |

**For the local AI:**

| Your hardware | Model the installer picks | How it feels |
|---|---|---|
| NVIDIA GPU with 12 GB+ (RTX 3060 12GB, 4070, 3080, 3090, 4090…) | qwen3:14b | Quick, smart replies |
| NVIDIA GPU with 8 GB, or a Mac with 16 GB | qwen3:8b | Quick, a little less clever |
| Mac with 24 GB+ | qwen3:14b | Quick, smart replies |
| No GPU | qwen3:8b | Slow (tens of seconds). Consider a [cloud model](#cloud-ai-models-optional) |

Also plan for **16 GB RAM** (32 GB is better) and **25–50 GB of free disk**.
A wired network connection for the Friday computer helps.

---

## Install

### The one-line install (recommended)

**Linux or macOS:** open a Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/knightlie-code/friday/main/scripts/get.sh | bash
```

**Windows 11:** right-click Start → **Terminal (Admin)**, and paste:

```powershell
irm https://raw.githubusercontent.com/knightlie-code/friday/main/scripts/get.ps1 | iex
```

The first time, Windows installs WSL and Ubuntu and asks you to pick a
Linux username and password. It may need a restart. Afterwards, run the same
line again to carry on.

The installer downloads Friday into a `friday` folder in your home folder,
then **asks before each step that changes your computer**:

| Step | What happens |
|---|---|
| Basics | Installs Python 3.11+, git and mkcert if missing (with apt, or Homebrew on a Mac) |
| Docker | Installs Docker if missing: Docker's official script on Linux, Docker Desktop on a Mac |
| GPU | Installs the NVIDIA Container Toolkit so voice recognition can use the GPU (skipped without an NVIDIA card) |
| Friday | Installs Friday's Python packages into its own folder (`friday/.venv`), and nothing system-wide |
| Settings | Creates `config.toml` and `.env`, and never overwrites existing ones |
| Voice ID | Downloads the 26 MB voice-recognition model |
| **AI model** | Lists the models Friday offers (qwen3:14b, qwen3:8b, qwen3.6:35b-a3b, qwen3.8:27b, gpt-oss:20b, qwen3:32b, or none), marks the one that fits your hardware, then installs [Ollama](https://ollama.com) and downloads your pick (5–22 GB). Choose none to use a cloud model, or download one from Settings later |
| Address | Names the computer `friday` so phones can find `friday.local`, and makes the HTTPS certificate |
| Lights | Linux only, optional: sets up Matter |
| Autostart | Asks whether Friday should [start by itself](#running-friday) |

It takes 10 to 30 minutes, mostly downloads. You can run it again at any
time: it skips what's done. To accept every recommended answer without
questions, put `-s -- --yes` after `bash`.

The voice services (speech-to-text and text-to-speech, a few GB) download
automatically the first time Friday starts.

### Or let an AI agent install it for you

If you use an AI agent that can run commands on your computer (Claude Code,
Codex, Cursor, Hermes Agent, OpenClaw and similar), paste this to it:

> Install Friday on this computer by following
> https://raw.githubusercontent.com/knightlie-code/friday/main/skills/install-friday/SKILL.md

The agent checks your hardware and asks you everything up front: which
model, autostart, and any keys you have. Then it runs the installer, adds
your keys and checks that Friday is working. It asks before anything that
changes your system. Agents that support skills can also install
[`skills/install-friday`](skills/install-friday/SKILL.md) as a skill.

<details>
<summary><b>Prefer to look before you run it? Install step by step instead</b></summary>

Piping a script into your shell means trusting it, so here's the manual way.
The scripts are short and commented, so feel free to read them first.

```bash
git clone https://github.com/knightlie-code/friday.git ~/friday
cd ~/friday
less scripts/install.sh      # read it
scripts/install.sh           # asks before each step
```

On Windows, install WSL first (`wsl --install -d Ubuntu` in an admin
terminal), add `networkingMode=mirrored` under `[wsl2]` in
`%USERPROFILE%\.wslconfig`, run `wsl --shutdown`, then run the commands above
inside Ubuntu.
</details>

### First start

If you chose automatic start, Friday is already running. Otherwise:

```bash
cd ~/friday
.venv/bin/house-agent
```

In the log (`scripts/autostart.sh logs` for the automatic copy), look for:

```
Pairing code for the web app: 7fKq2mXa9LpT (saved to .env as API_TOKEN)
Friday is at https://friday.local:8787
```

Keep the pairing code handy. Every phone or browser asks for it once.

---

## Open Friday and talk to it

On any phone, tablet or computer on your home Wi-Fi, open:

### **https://friday.local:8787**

1. **Certificate warning:** the first visit shows a "not private" warning,
   because the certificate was made on your own computer rather than bought.
   Choose *Advanced → Proceed*. To remove the warning for good, see
   [Trust Friday's certificate](#trust-fridays-certificate-no-more-warnings).
2. **Enter the pairing code** from the log (it's also in `.env` as `API_TOKEN`).
3. **Say hello.** Tap the round **Talk** button and speak, or type in the
   chat. On the first chat Friday introduces itself, learns your name and
   voice, and offers to set up your preferences.
4. **Put it on your home screen:**
   - *iPhone/iPad:* Safari → Share → **Add to Home Screen**
   - *Android:* Chrome → ⋮ menu → **Add to Home screen** / **Install app**

   It then opens full-screen like a normal app.

**If `friday.local` doesn't open**, use the computer's IP address instead,
e.g. `https://192.168.0.123:8787`. Some Android phones and routers don't
support `.local` names, and on Windows the IP address is the way in. The
installer prints the address at the end. To look it up again:

- **Linux:** `hostname -I`
- **macOS:** `ipconfig getifaddr en0`
- **Windows:** `ipconfig` in PowerShell (the "IPv4 Address" line)

Reserve that address in your router so it never changes.

**Things to try:**

- "Turn off all the lights" · "Make the bedroom warmer" · "Dim the kitchen to 30"
- "Put on Stranger Things" · "Resume my show" · "Switch to the Xbox"
- "Remind me to take the bins out every Tuesday at 7pm"
- "Slowly brighten the bedroom starting at 6:45 on weekdays"
- "Add eggs and milk to the shopping list" · "What's on my list?"
- "What's the weather this weekend?" · "How long to drive to the airport?"
- "What's on tomorrow?" · "I have a dentist appointment Friday at 3"
- "Find me a quick high-protein dinner recipe"
- "Order my usual from Chipotle" (with DoorDash set up)

### Trust Friday's certificate (no more warnings)

The installer sets up `mkcert`, so Friday's certificate is signed by a private root made on your computer.
Install that root on each device once, and every browser trusts Friday:

```bash
echo "$(mkcert -CAROOT)/rootCA.pem"    # the file to send to your phones
```

On a Mac, `open "$(mkcert -CAROOT)"` shows it in Finder. On Windows, run
`explorer.exe "$(mkcert -CAROOT)"` in Ubuntu to open the folder.

- **iPhone/iPad:** AirDrop or email the file to yourself and open it.
  Then go to *Settings → General → VPN & Device Management* and install it,
  then *Settings → General → About → Certificate Trust Settings* and turn it on.
- **Android:** copy the file over, then *Settings → Security → Encryption &
  credentials → Install a certificate → CA certificate*.
- **Mac:** double-click it, then in Keychain Access set it to *Always Trust*.
- **Windows:** double-click it, *Install Certificate → Local Machine →
  Trusted Root Certification Authorities*.

If the installer couldn't install mkcert, install it yourself, then delete
`data/certs/cert.pem` and run `scripts/make-cert.sh`, then restart Friday.

---

## Running Friday

You can run Friday in one of two ways. Switch between them at any time.

**Automatically (recommended):** Friday starts whenever the computer starts,
and restarts itself if it ever crashes. It keeps running in the background
with no window.

```bash
cd ~/friday
scripts/autostart.sh on        # turn on (also starts it now)
scripts/autostart.sh off       # turn off (also stops it)
scripts/autostart.sh status    # is it on, and running?
scripts/autostart.sh restart   # after changing config.toml or .env
scripts/autostart.sh logs      # watch what it's doing
```

- **Linux:** a systemd user service. It starts at boot, even before anyone logs in.
- **macOS:** a launch agent. It starts when you log in, so turn on automatic
  login (*System Settings → Users & Groups*) for power-on start. Also set Docker
  Desktop to *Start when you sign in*.
- **Windows:** a scheduled task starts Ubuntu when you sign in, and Friday
  starts inside it. Answer yes to the autostart question in *both* the Windows
  and Ubuntu parts of the installer. To stop it, run
  `Unregister-ScheduledTask -TaskName Friday` in PowerShell.

**By hand:** Friday runs only while you have it open in a terminal:

```bash
cd ~/friday
.venv/bin/house-agent          # Ctrl-C to stop
```

On Windows, open **Ubuntu** from the Start menu first.

Either way, Friday starts and stops its own voice (and Zigbee) containers.

Your data lives in `~/friday/data`: the database, voiceprints, Zigbee
network and certificates. Back it up along with `config.toml` and `.env` (and
`~/.matter_server` if you use Matter).

---

## Local AI models

Friday's AI runs in **[Ollama](https://ollama.com)**, a free app that
downloads and runs open models on your own computer. The installer asks
which model you want; you can add or switch models later, **right in the
app**.

### The models Friday offers

| Model | Download | Needs | Good for |
|---|---|---|---|
| **qwen3:14b** | 9.3 GB | 12 GB+ GPU or 24 GB+ Mac | The default. Best balance of speed and smarts |
| **qwen3:8b** | 5.2 GB | 8 GB+ GPU or 16 GB+ Mac | Smaller computers. Quicker, a little less reliable |
| **qwen3.6:35b-a3b** | 22 GB | 24 GB+ GPU or 32 GB+ Mac | Newer. A "mixture of experts" model: it only uses 3B of its 35B at a time, so it's quick for its size and copes with some of it running on the CPU |
| **qwen3.8:27b** | 17 GB | 20 GB+ GPU or 32 GB+ Mac | Newest and sharpest, but a bit slow |
| **gpt-oss:20b** | 13 GB | 16 GB+ GPU or 32 GB+ Mac | OpenAI's open model, good with tools |
| **qwen3:32b** | 20 GB | 24 GB+ GPU or 48 GB+ Mac | Sharp but slow |

### Add or switch models in the app

Open **Settings → Assistant model** and tap the model picker:

- **On this computer** lists the models you already have. Pick one and
  press **Switch**.
- **Download to this computer** lists the offered models you don't have
  yet. Pick one and press **Download**. Friday shows the progress, sets the
  model up, and then you press **Switch**.

Ollama itself must be installed on the Friday computer. If you chose "none"
in the installer, install it first:

- **Linux / Windows (inside Ubuntu):** `curl -fsSL https://ollama.com/install.sh | sh`
- **macOS:** `brew install ollama && brew services start ollama`, or the app from [ollama.com/download](https://ollama.com/download)

### By hand

Each model gets a Friday build (named `house-…`) that gives it a 32k-token
memory. Ollama's default is too small for Friday's instructions and tools.

```bash
cd ~/friday
ollama pull qwen3.6:35b-a3b
printf 'FROM qwen3.6:35b-a3b\nPARAMETER num_ctx 32768\n' > data/house-qwen3.6.Modelfile
ollama create house-qwen3.6 -f data/house-qwen3.6.Modelfile
```

Then pick it in **Settings → Assistant model**. Any other Ollama model works
the same way, as long as it supports **tool calling**. A model without it
can chat but can't control anything.

**Running Ollama on another computer:** if a stronger PC on your network
runs Ollama, point Friday at it in `.env` with
`LLM_BASE_URL=http://that-pc:11434/v1`. On that PC, set
`OLLAMA_HOST=0.0.0.0` so it accepts connections from the network.

---

## Add your TV

**Roku** (TVs and sticks) works with no pairing. First, on the TV, turn on
*Settings → System → Advanced system settings → Control by mobile apps →
Network access → **Enabled***.

Then, in Friday, either:
- say **"find my TV"**, or
- open **Settings → House → Scan network**, or
- tap **Add** and type the TV's IP address (from the TV's *Settings →
  Network → About*).

**Other brands** are paired the same way. The TV shows a prompt or PIN the
first time:

| Brand | What to do on the TV |
|---|---|
| LG (webOS) | Accept the prompt. For power-on, enable *Quick Start+* |
| Samsung (2016+) | Accept the prompt. For power-on, enable *Network Standby* |
| Vizio SmartCast | Read the PIN on screen back to Friday |
| Android TV / Google TV (Sony, Hisense, TCL, Philips, Shield) | Read back the 6-digit code |
| Amazon Fire TV | First turn on *Settings → My Fire TV → Developer options → ADB debugging* |
| Apple TV (the box) | Read back the PIN |

**Tip:** reserve each TV's IP address in your router so it never moves. If it
does move, tell Friday "the TV's IP changed" and it will find it again.

**Playing shows by name** ("put on The Office") needs a free TMDB key, see
[below](#movies-and-shows-tmdb--watchmode).

---

## Add lights and plugs

> **Linux only for now.** Matter and Zigbee need direct access to Bluetooth,
> USB and the home network, which macOS and WSL containers can't provide yet.
> On a Mac or Windows PC, TVs, voice and everything else still work.

### Matter (most new Wi-Fi bulbs and plugs)

Say yes to Matter in the installer, or run it again later. Then either:

- **With Bluetooth on the Friday computer:** set `bluetooth = true` under
  `[matter]` in `config.toml`, and your Wi-Fi name and password in `.env`
  (`HOUSE_WIFI_SSID` / `HOUSE_WIFI_PASSWORD`). Then tell Friday "pair a new
  device" and read it the code from the box or QR label.
- **Without Bluetooth:** set the device up in its own app first so it joins
  your Wi-Fi. Then switch its power off at the wall for 10 seconds, turn it
  back on, and within 15 minutes tell Friday to pair it and read out its
  Matter code.

After pairing, Friday asks what to call the device and which room it's in.

### Zigbee (IKEA, Philips Hue bulbs, Aqara and more)

You need a USB Zigbee coordinator. The
[SONOFF ZBDongle-E](https://sonoff.tech/product/gateway-and-sensors/sonoff-zigbee-3-0-usb-dongle-plus-e/)
or Dongle Lite is about $20–30.

1. Plug it in and find its name:
   ```bash
   ls /dev/serial/by-id/
   ```
2. In `.env`, add the dongle and your time zone:
   ```env
   ZIGBEE_DONGLE=/dev/serial/by-id/usb-SONOFF_..._if00-port0
   TZ=America/New_York
   ```
3. In `config.toml`, set `enabled = true` under `[zigbee]`, and add the Zigbee file under `[docker]`:
   ```toml
   compose_files = ["docker-compose.zigbee.yml", "docker-compose.tts.yml"]
   ```
   (Use `docker-compose.tts-cpu.yml` there instead if the installer picked CPU speech.)
4. For a ZBDongle-P or another TI-based stick, change `adapter: ember` to
   `adapter: zstack` in `data/zigbee2mqtt/configuration.yaml`.
5. Restart Friday and say **"pair a Zigbee device"**, then put the bulb in
   pairing mode (usually: turn it on and off 5 times).

Back up the `data/zigbee2mqtt` folder. It holds your Zigbee network's keys.

---

## Friday's calendar on your phone

Tell Friday about a plan ("I've got the dentist Thursday at 3", "put Ana's
party on Saturday at 4") and it appears **in your phone's own calendar app**,
with a 30-minute alert. Changes and cancellations show up there too. Subscribe
once on each phone:

- **iPhone / iPad:** *Settings → Calendar* (iOS 18+: *Settings → Apps →
  Calendar*) *→ Calendar Accounts → Add Account → Other → **Add Subscribed
  Calendar***, and enter
  `https://friday.local:8787/calendar.ics`. If friday.local doesn't work on
  your network, use the IP address.
- **Mac:** Calendar → *File → New Calendar Subscription…*, same address. Set
  *Auto-refresh* to every 5 minutes.
- **Android:** Google Calendar can't subscribe to an address inside your home
  (Google's servers fetch it, and they can't see your network). Install the free
  **[ICSx⁵](https://icsx5.bitfire.at/)** app, add the same address, and
  Friday's events appear in Google Calendar as their own calendar.

Or just ask Friday: *"How do I get your calendar on my phone?"* It walks you
through it.

Good to know:
- The phone refreshes on its own schedule, usually every 15 minutes to an
  hour. On iPhone, set it under *Settings → Calendar → Calendar Accounts → Fetch
  New Data*.
- It only refreshes while the phone can reach Friday: on your home Wi-Fi, or
  away from home through a VPN such as Tailscale.
- Trust Friday's certificate first ([how](#trust-fridays-certificate-no-more-warnings)),
  otherwise iPhones may refuse the subscription.
- Friday's calendar is read-only on the phone: add or change plans by telling
  Friday. Your own Google/iCloud/Outlook calendars can also be read *into*
  Friday; see [Getting API keys](#getting-api-keys-optional-features).

---

## Getting API keys (optional features)

Friday works without any keys. Each key below switches on one extra feature.
Most are free. Put keys in the **`.env`** file in the Friday folder, then
restart Friday. (`.env` is private: it's never uploaded, and git ignores it.)

| Feature | Key | Cost |
|---|---|---|
| Play movies/shows by name, recommendations | `TMDB_READ_ACCESS_TOKEN` | Free |
| Open a title directly in the right streaming app | `WATCHMODE_API_KEY` | Free tier |
| Live traffic on driving times | `TOMTOM_API_KEY` | Free tier |
| Backup route planner | `OPENROUTESERVICE_API_KEY` | Free |
| Flight search (hotels need nothing) | `letsfg auth` | Free |
| Your existing calendars | `CALENDAR_ICS_URLS` | Free |
| Food and grocery orders | DoorDash's `dd-cli` | Free (you pay for orders) |
| Cloud AI instead of the local model | OpenAI / Anthropic / OpenRouter key | Pay per use |

Weather, forecasts, severe-weather alerts, directions, news, web search,
recipes and your calendar on your phone need **no key**.

**Don't want to sign up for anything?** You don't have to. When a feature's
key is missing, Friday looks the answer up on the web instead ("what's
streaming on Netflix", "find me a flight to Denver", "what should we watch
tonight") and tells you it did. Web results can be less accurate or out of
date than the real services, and without the movie keys Friday can open the
right streaming app but not jump straight to the title. It's decided **per feature**: in
**Settings → Assistant → Features without a key**, each feature that needs a
key (movies & shows, flight search) has its own switch. Leave it on and
Friday searches the web whenever that feature has no key. Turn it off and
Friday tells you which key is missing instead. Features whose key you've set
always use the real service.

The same switches cover a service that's simply *having a bad day*. When a
keyed service times out, rate limits Friday or goes down mid-question, Friday
searches the web for the same answer rather than handing the question back,
and says where the answer came from. A page that refuses to open (a paywall
or a bot wall) means the next search result gets tried, not that the question
is dropped. Turn a feature's switch off and Friday reports the outage
instead.

The same habit runs through everything else, with or without a key. When a
search comes back empty, Friday widens it before saying so — fewer words, the
plainer name, a whole menu section instead of one item, a longer window, the
next store on the list — and when the exact thing you asked for isn't there,
it says so and tells you what is, in the same breath. It won't hand a question
back that one of its own tools could answer, and it won't ask you to narrow
something it could narrow itself.

<details>
<summary><b>Movies and shows: TMDB + Watchmode</b></summary>

<a name="movies-and-shows-tmdb--watchmode"></a>

**TMDB** (The Movie Database) powers search, "find me a 90s horror movie",
and which of your streaming services has a title.

1. Create a free account at [themoviedb.org/signup](https://www.themoviedb.org/signup) and confirm your email.
2. Go to **Settings → API** ([themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)) and request an API key. Choose *Developer*, then *Personal use*, and fill in the short form.
3. Copy the long **API Read Access Token** (not the short "API Key").
4. In `.env`:
   ```env
   TMDB_READ_ACCESS_TOKEN=eyJhbGciOi...
   ```

**Watchmode** turns a title into the exact link that opens it in Netflix,
Disney+, Max, Prime Video or Hulu on your TV.

1. Sign up at [api.watchmode.com](https://api.watchmode.com/) (free tier).
2. Copy your API key from the dashboard.
3. In `.env`:
   ```env
   WATCHMODE_API_KEY=your_key
   ```

By default Friday looks across all the major streaming services. To limit
it to the ones you pay for, set the list once:

```bash
curl -k -X PUT https://friday.local:8787/api/subscriptions \
  -H "Authorization: Bearer $(grep ^API_TOKEN= .env | cut -d= -f2)" \
  -H 'content-type: application/json' \
  -d '{"providers": ["Netflix", "Hulu", "Disney+"]}'
```
</details>

<details>
<summary><b>Live traffic: TomTom</b></summary>

Directions work without a key. This adds real-time traffic to driving times.

1. Sign up at [developer.tomtom.com](https://developer.tomtom.com/) (free, no card).
2. Open your **Dashboard → Keys** and copy the default key.
3. In `.env`: `TOMTOM_API_KEY=your_key`
</details>

<details>
<summary><b>Backup route planner: OpenRouteService</b></summary>

Optional. Directions use the free OpenStreetMap route servers; this adds a
backup with an availability guarantee.

1. Sign up at [openrouteservice.org/dev/#/signup](https://openrouteservice.org/dev/#/signup).
2. Request a free **Standard** token in the dashboard.
3. In `.env`: `OPENROUTESERVICE_API_KEY=your_token`
</details>

<details>
<summary><b>Flights: LetsFG</b></summary>

Hotel search works with nothing to set up. Flight search needs a free token:

```bash
.venv/bin/pip install letsfg
.venv/bin/letsfg auth
```

It walks you through a zero-amount card check (nothing is charged, and
searches are free) and saves the token where Friday finds it.
</details>

<details>
<summary><b>Your calendars: Google, iCloud, Outlook</b></summary>

Friday reads your existing calendars (read-only) so it knows about your
2pm meeting. Each calendar has a private link:

- **Google:** [calendar.google.com](https://calendar.google.com) → ⚙ Settings → click the calendar on the left → *Integrate calendar* → copy **Secret address in iCal format**.
- **iCloud:** Calendar app → share icon next to the calendar → turn on *Public Calendar* → copy the link.
- **Outlook:** Settings → Calendar → *Shared calendars* → *Publish a calendar* → copy the **ICS** link.

In `.env` (separate several with commas):

```env
CALENDAR_ICS_URLS=https://calendar.google.com/calendar/ical/.../basic.ics
```

Keep these links private. Anyone with one can read that calendar.

To have Friday **add** events to your phone's calendar, see
[Friday's calendar on your phone](#fridays-calendar-on-your-phone). That part
needs no link or key.
</details>

<details>
<summary><b>DoorDash: food, groceries and household items</b></summary>

Friday orders through **dd-cli**, DoorDash's own command-line tool
([github.com/doordash-oss/doordash-cli](https://github.com/doordash-oss/doordash-cli)).
It's currently a beta. Check its page for current availability and access
requirements.

1. Download and install the Linux release (check the page for the newest version):
   ```bash
   V=v0.2.4
   curl -LO https://github.com/doordash-oss/doordash-cli/releases/download/$V/dd-cli-$V-linux-amd64.tar.gz
   curl -LO https://github.com/doordash-oss/doordash-cli/releases/download/$V/dd-cli-$V-linux-amd64.tar.gz.sha256
   sha256sum -c dd-cli-$V-linux-amd64.tar.gz.sha256
   tar xzf dd-cli-$V-linux-amd64.tar.gz
   bash dd-cli-$V-linux-amd64/install.sh
   ```
2. Sign in to your DoorDash account:
   ```bash
   ~/.local/bin/dd-cli login
   ```
   Or skip this and just ask Friday to "sign in to DoorDash". It sends the
   sign-in link to the app's Messages panel.
3. Restart Friday.

Friday always reads back the items, total, card and tip, and places the
order only after a clear "yes". DoorDash's terms make you responsible for
orders placed this way, so read `LICENSE.txt` inside the dd-cli download
before turning it on.
</details>

<details>
<summary><b>Cloud AI models: OpenAI, Anthropic, OpenRouter (optional)</b></summary>

<a name="cloud-ai-models-optional"></a>

Friday uses the private local model by default. If your computer has no
suitable graphics card, or you want a smarter model, you can switch to a
cloud one in **Settings → Assistant model**.

> ⚠️ **With a cloud model, Friday is no longer private.** Everything you say
> or type to Friday is sent to that company, and may be stored under their
> data policy. That includes names, rooms, devices, your calendar and your
> lists. Recorded audio still stays on your computer; only the text is sent.
> Switch back to *This computer* at any time.

These are **API keys**, which are billed separately from ChatGPT Plus or
Claude Pro subscriptions (those can't be used here). Add a few dollars of
credit first:

- **OpenAI:** [platform.openai.com](https://platform.openai.com) → *Settings → Billing* (add credit) → [*API keys*](https://platform.openai.com/api-keys) → **Create new secret key**.
- **Anthropic (Claude):** [console.anthropic.com](https://console.anthropic.com) → *Billing* (add credit) → [*API Keys*](https://console.anthropic.com/settings/keys) → **Create Key**.
- **OpenRouter** (hundreds of models with one key): [openrouter.ai](https://openrouter.ai) → *Credits* → [*Keys*](https://openrouter.ai/keys) → **Create Key**.

Paste the key into **Settings → Assistant model**, pick a model, tick the
privacy box, and press **Test & switch**. If a cloud request fails, Friday
falls back to the local model for that answer.
</details>

<details>
<summary><b>Your own AI service or agent as Friday's brain</b></summary>

Pick **Settings → Assistant model → Custom endpoint** to use any service that
speaks the **OpenAI chat-completions API with tool calling**. Fill in its
address, the model name, and a key if it needs one. For example:

- Hermes and other models through Nous Portal, OpenRouter, Groq or Together
- LM Studio (`http://that-pc:1234/v1`), vLLM (`http://that-pc:8000/v1`) or
  llama.cpp's server (`http://that-pc:8080/v1`) on another computer
- An agent gateway (such as OpenClaw or Hermes Agent's API server), if its
  OpenAI-compatible endpoint passes Friday's tools through to the model

Friday works by calling tools (turn on this light, play that show), so when
you switch it checks that the endpoint really returns tool calls. It refuses
one that can only chat.

**What doesn't work:** ChatGPT, Claude.ai, Claude Code, Codex and Cursor
*subscriptions*. They have no API another program can call. Their companies'
API keys (above) do work.

The same privacy rule applies: if the endpoint isn't on your own network,
your conversations go to whoever runs it.
</details>

---

## Updating

```bash
cd ~/friday
scripts/update.sh
```

On Windows, run it in Ubuntu, or from PowerShell:
`wsl -d Ubuntu -- ~/friday/scripts/update.sh`.

This downloads the newest version and installs anything new. It rebuilds
`house-qwen` if its settings changed, and restarts Friday if it runs
automatically. Your `config.toml`, `.env` and `data/` folder are never
touched. Running the one-line install again also updates.

When `config.example.toml` gains new options, the updater tells you. Compare
it with yours using `diff config.toml config.example.toml`.

To be told about new versions, click **Watch → Custom → Releases** at the
top of this page on GitHub.

---

## Privacy: what stays home and what doesn't

**Stays on your computer, always:**
- the AI model and everything it thinks (with the default local model)
- microphone audio, speech-to-text, the spoken voice, and voice recognition
  (voiceprints are stored as numbers, and recordings are discarded)
- conversations, people, rules, preferences, lists, reminders, routines, and
  device names and history
- control of your TVs and lights, which happens directly over your home
  network with no cloud account

**Goes online, only when you use that feature:**

| When you… | Friday contacts | What's sent |
|---|---|---|
| ask about the weather | Open-Meteo, weather.gov (US alerts) | your home's approximate location |
| ask for directions | OpenStreetMap (Nominatim, OSRM), TomTom if keyed | the places you asked about |
| ask Friday to look something up | DuckDuckGo (Yahoo as a fallback) | the search words |
| ask something whose usual source is down or unkeyed | the same search engines | the search words |
| look for a recipe | TheMealDB and recipe websites | the recipe search |
| search for a movie or show | TMDB, Watchmode | the title |
| open the home screen's news | BBC, NPR (or your own feeds) | nothing personal |
| search flights or hotels | LetsFG | the trip details |
| order food | DoorDash (through dd-cli) | the order, as with the DoorDash app |
| choose a cloud AI model | OpenAI / Anthropic / OpenRouter | **every conversation** (off by default, with a warning first) |

No telemetry, analytics, crash reports or "phone home" of any kind. There is
no Friday account and no Friday server. The code is here for you to check.

---

## Troubleshooting

**`friday.local` won't open.** Check that the phone is on the same Wi-Fi (not
a guest network), and on Linux that `avahi-daemon` is running
(`systemctl status avahi-daemon`). Otherwise use the IP address (see
[Open Friday](#open-friday-and-talk-to-it)).

**Windows: phones can't reach Friday at all.** Mirrored networking needs
Windows 11 22H2 or newer. Check that `%USERPROFILE%\.wslconfig` contains
`networkingMode=mirrored` under `[wsl2]`, then run `wsl --shutdown` and start
Friday again.

**The microphone button does nothing.** Browsers only allow the microphone
over HTTPS. Use `https://`, not `http://`, and accept or trust the certificate.

**"Couldn't reach the TV".** Check that the TV is on the same network, and on
Roku that *Control by mobile apps → Network access* is **Enabled** (not
*Limited*). If the TV got a new IP address, tell Friday "find my TV".

**Lights stopped responding.** Ask Friday "what's wrong with the lights?", or
check `https://friday.local:8787/api/health`. For Zigbee, `docker compose
logs zigbee2mqtt` shows whether the dongle was found. Friday restarts
Zigbee2MQTT by itself if it stops.

**Friday is slow to answer.** The local model needs an NVIDIA GPU (or Apple
Silicon Mac) with enough memory; check with `nvidia-smi` or `ollama ps`. Try
a [smaller model](#local-ai-models), or a cloud model.

**Forgot the pairing code.** It's `API_TOKEN` in the `.env` file.

**Something else?** [Open an issue](https://github.com/knightlie-code/friday/issues)
with what you did, what happened, and the relevant part of the log
(`scripts/autostart.sh logs`, or the terminal output). Remove any keys or addresses first.

The [full reference](docs/REFERENCE.md) covers every setting, how each part
works, and the HTTP API. Friday's API documentation is at
`https://friday.local:8787/docs`.

---

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests use fake TVs and devices and never send commands to real hardware.
See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

Friday is **free for personal and other noncommercial use** under the
[PolyForm Noncommercial License 1.0.0](LICENSE). Use it at home, change it,
and share your changes on the same terms.

**Commercial use needs a paid license.** That includes selling it, building
it into a product or device, offering it as a service, or installing it for
clients. See [COMMERCIAL.md](COMMERCIAL.md).

Friday is source-available, not "open source" in the OSI sense, because
commercial use isn't free.

Friday talks to other people's software and services (Ollama, Qwen,
Whisper, Kokoro, Zigbee2MQTT, python-matter-server, dd-cli and others). Each
has its own license and terms, which you accept when you install or use it.
"Friday" is not affiliated with Roku, LG, Samsung, DoorDash, OpenAI,
Anthropic or any other company named here.
