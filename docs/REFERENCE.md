# Friday reference

The in-depth guide: how each part works, its settings, and its HTTP API.
For installing and everyday use, start with the [README](../README.md).

The `curl` examples below are shortened. Against a real install, add the
pairing code and use HTTPS: `curl -k -H "Authorization: Bearer $API_TOKEN"
https://friday.local:8787/...` (`-k` only while the certificate is
self-signed).

## What works now

- OpenAI-compatible local LLM endpoint (Ollama, vLLM, llama.cpp server, or another compatible server)
- OpenAI-compatible local speech-to-text endpoint
- OpenAI-compatible local text-to-speech endpoint for spoken web voice replies
- Configured Roku allowlist and SSDP discovery
- Roku status, installed apps, remote keys, text entry, app launch, and content deep links
- Named, bounded navigation routines for flows such as “open Netflix and choose Mom”
- SQLite conversation context and a local watch-history ledger
- Durable household subscriptions and provider-level deep-link calibration
- Watchmode/TMDB catalog search filtered to subscriptions and region
- Subscription-filtered recommendations by genre, year, ratings, and popularity
- Local taste memory for likes, dislikes, and temporary “not now” deferrals
- Live flight and hotel search through LetsFG: hotels need no setup; flights need a one-time free `letsfg auth` (see `config.example.toml` `[travel]`)
- Food, grocery, and household-item ordering by voice through DoorDash's own `dd-cli`: search, menus, carts, reorders, receipts, and placing orders after a read-back and a clear yes (see “Ordering from DoorDash” below)
- A one-way messages panel in the web client (envelope icon, unread badge) that Friday pushes links, sign-in pages, and step-by-step instructions to with `inbox_send`, instead of reading them aloud
- A same-origin web app (installable to a phone home screen) with chat and past chats, every light and switch with slide controls, a TV remote with a trackpad, per-person rules and preferences Friday follows, and the house settings (TVs, DoorDash, Wi-Fi) — see “The app” below

Roku cannot report what is drawn inside Netflix, Disney+, and similar apps. A routine can send the right buttons, but the backend can only verify that Roku accepted them—not that a particular profile tile or show was on screen. The API reports that distinction instead of manufacturing certainty.

## Setup

Roku OS 14.1 and later requires local control to be enabled:

`Settings → System → Advanced system settings → Control by mobile apps → Network access → Enabled`

Then set up the service:

```bash
cp config.example.toml config.toml
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Edit `config.toml` with the TV's reserved LAN address and your model endpoints. The app defaults to Ollama at `http://127.0.0.1:11434/v1`. Start it with:

```bash
house-agent
```

Open `https://friday.local:8787`. API documentation is at `https://friday.local:8787/docs`.

## Containers

`house-agent` is meant to be the only command this house needs. Four of its
pieces are not Python and run in containers — `mosquitto` and `Zigbee2MQTT`
for the Zigbee coordinator, `kokoro` and `whisper` for speech — and a fifth,
`matter-server`, is a separate program house-agent already starts itself
(see [Smart lights and devices](#smart-lights-and-devices-matter-and-zigbee)).
With `manage = true` under `[docker]`, house-agent starts the containers too,
and stops them again when you quit with Ctrl-C:

```toml
[docker]
manage = true
compose_files = ["docker-compose.zigbee.yml", "docker-compose.tts.yml"]
```

Both files go to one `docker compose` invocation on purpose. They share a
project — Compose names it after this folder — so passing only one of them
makes Compose treat the other's containers as orphans.

**It costs about a fifth of a second.** `docker compose up -d` returns as soon
as the containers are created; it does not wait for the services inside them
to answer, and it does not need to, because every backend here already has its
own reconnect loop. Measured on this machine, start-up to
`Application startup complete`:

| | time |
|---|---|
| `manage = false` (baseline) | 0.45 s |
| `manage = true`, containers already running | 0.66 s |
| `manage = true`, containers stopped — starts all four | 1.22 s |

The services finish coming up behind house-agent: mosquitto and Zigbee2MQTT
are talking within about three seconds, whisper's CUDA runtime within about
eleven. The Zigbee backend logs `connected to MQTT` in the same second
house-agent finishes booting.

**house-agent stops only what it started.** Containers already running when it
boots are adopted and left running when it exits, so a `docker compose up` you
are debugging with does not die under you. That is the rule `matter_process.py`
already follows for a controller it did not spawn. `GET /api/health` reports
which is which under `containers`.

For the same reason the compose files use `restart: on-failure` rather than
`restart: unless-stopped`: a crashed container still comes back on its own, but
a machine that reboots does not bring these up behind house-agent's back — which
would mean house-agent adopted them every time and never stopped anything.

If Docker is missing or its daemon is down, house-agent says so once and starts
anyway; the containers just have to be started some other way. Set
`manage = false` to go back to running them by hand. Podman works with
`command = ["podman", "compose"]`.

## Settings Friday can change for you

Some settings change when the house does, and Friday can update them herself — only when asked, never on her own to get past an error:

- A TV's saved address after a new router or Wi-Fi: “the TV's IP changed, it's 192.168.0.177 now”. With no address given she searches the network for the TV and applies the one match. `house_config_set` with `tv_address`.
- A TV's name: `tv_name`.
- The home Wi-Fi name and password given to brand-new Matter devices during pairing: `wifi`. The password is never spoken back and is dropped from the saved conversation trace.

Changes are written to `config.toml` and `.env` (whichever defines the value; `ROKU_IP_ADDRESS` in `.env` overrides the TV list in `config.toml`), comments intact, and take effect at once without a restart. `house_config_show` reads back what is saved.

## Local spoken responses

The web client's hold-to-talk path can speak the assistant's answer through a local
Kokoro service. Typed chat stays silent. The default `bf_isabella` voice is a calm,
polished British female synthetic voice selected for a futuristic-assistant feel; it
is not a clone of or impersonation of the performer who played FRIDAY.

Start the CPU image, which leaves the GPUs available for the LLM:

```bash
docker compose -f docker-compose.zigbee.yml -f docker-compose.tts.yml up -d
```

With `manage = true` under `[docker]` in config.toml — see [Containers](#containers)
— house-agent runs that itself at start-up and stops it again when you quit, so
you never need the command above.

If that machine has Docker but not the Compose plugin, use the equivalent command:

```bash
docker run -d \
  --name house-agent-kokoro \
  --restart unless-stopped \
  -p 127.0.0.1:8880:8880 \
  -e KOKORO_API_KEY=local \
  -e KOKORO_VOICE=bf_isabella \
  -e KOKORO_SPEED=0.96 \
  -e KOKORO_LANG_CODE=b \
  -e KOKORO_DISABLE_USAGE_COUNTS=1 \
  -v kokoro-data:/var/lib/kokoro \
  hwdsl2/kokoro-server
```

The first start downloads the voice model. Once the container is ready, test it
directly:

```bash
curl -sS http://127.0.0.1:8880/v1/audio/speech \
  -H 'authorization: Bearer local' \
  -H 'content-type: application/json' \
  -d '{"model":"tts-1","voice":"bf_isabella","input":"Good evening. Systems are ready.","response_format":"mp3","speed":0.96}' \
  --output voice-test.mp3
```

Then start `house-agent` normally and use the `CONVO` button in the web app. The flow
is microphone → local Whisper endpoint → local LLM → local Kokoro endpoint → browser
audio. You can change `TTS_VOICE` or `TTS_SPEED` in `.env`; `bf_alice`, `bf_emma`,
and `bf_lily` are useful alternate British female voices, and `af_bella`, `af_heart`,
and `af_sarah` American ones (the server picks British or American pronunciation
from the voice's first letter, so no other setting changes).

## Quick Roku CLI

You do not need the LLM or speech server running to test the TV. After installing the project, find the TV's IP on the Roku under `Settings → Network → About`, then run:

```bash
roku-cli --host 192.168.0.150 info
roku-cli --host 192.168.0.150 apps
roku-cli --host 192.168.0.150 key Home
roku-cli --host 192.168.0.150 launch Netflix
roku-cli --host 192.168.0.150 input xbox
roku-cli --host 192.168.0.150 input switch
roku-cli --host 192.168.0.150 input tv
roku-cli --host 192.168.0.150 interactive
```

Inside interactive mode:

```text
roku> home
roku> down
roku> key right 2
roku> select
roku> launch "Disney Plus"
roku> apps
roku> status
roku> quit
```

To type a search, first use the remote keys to focus an actual text box, then run `type Loki`. Roku OS 12 and later removed the old universal ECP search command, so the CLI does not pretend it can search without a focused field.

If `config.toml` contains exactly one Roku, `roku-cli status` is enough. With multiple TVs, use `roku-cli --device living-room status`.

### Roku-side setup

1. Put the computer and Roku on the same home LAN. Guest Wi-Fi and client isolation commonly block local control.
2. Find the Roku IP under `Settings → Network → About`. Reserve that address in the router so it does not change.
3. On Roku OS 14.1 or later, open `Settings → System → Advanced system settings → Control by mobile apps → Network access` and select `Enabled`. `Limited` returns HTTP 403 for remote keys.
4. For a Roku TV that should wake over the network, enable `Settings → System → Power → Fast TV Start`. This uses more standby power.
5. Developer mode is **not** required for normal info, app launch, or remote-key commands.

Start with `info`, then `key Home`, then `apps`. If those work, the backend has everything it needs for basic control. App profile selection still needs a calibrated routine because Roku cannot expose streaming-app screen contents.

Environment variables override the common model settings:

```text
HOUSE_AGENT_CONFIG
HOUSE_AGENT_HOST
HOUSE_AGENT_PORT
LLM_BASE_URL
LLM_MODEL
LLM_API_KEY
STT_BASE_URL
STT_MODEL
STT_API_KEY
TTS_BASE_URL
TTS_MODEL
TTS_VOICE
TTS_API_KEY
TTS_RESPONSE_FORMAT
TTS_SPEED
ROKU_IP_ADDRESS
ROKU_DEVICE_ID
ROKU_DEVICE_NAME
ROKU_PORT
ROKU_INPUT_XBOX
ROKU_INPUT_SWITCH
ROKU_INPUT_TV
ROKU_INPUT_LIVE_TV
HOUSEHOLD_PROFILE
CATALOG_REGION
WATCHMODE_API_KEY
TMDB_READ_ACCESS_TOKEN
TMDB_API_TOKEN
```

`ROKU_IP_ADDRESS` is the TV/streaming player's own address from `Settings → Network → About`. Do not use the gateway address; that points to the router. The project automatically reads these values from a local `.env` next to `config.toml`.

## Catalog and direct playback

TMDB is preferred for searches, discovery, metadata, ratings, genres, release dates,
and subscription availability. Watchmode is reserved for resolving a selected title
into a provider URL and candidate Roku content ID because TMDB's provider endpoint does
not return full streaming deep links.

Add at least one catalog credential to `.env`:

```env
CATALOG_REGION=US
WATCHMODE_API_KEY=your_key
TMDB_READ_ACCESS_TOKEN=your_read_access_token
```

The catalog and playback pipeline is:

```text
Discovery/recommendation → TMDB filters + JustWatch availability
Selected title → Watchmode provider URL → candidate content ID
               → trusted provider resolver → Roku deep link
```

Omit `season` and `episode` to let the provider resume a series from its own watch
history. Include both to request one exact episode:

```bash
curl -X POST http://127.0.0.1:8787/api/content/play \
  -H 'content-type: application/json' \
  -d '{
    "device_id":"living-room",
    "title":"Loki",
    "provider":"Disney+",
    "season":2,
    "episode":3
  }'
```

Exact episode playback uses Watchmode's paid episode-level source links and is disabled
by default. After upgrading the catalog plan, set
`EXACT_EPISODE_PLAYBACK_ENABLED=true` in `.env`. If a provider still does not return a
usable episode link, the request reports the limitation and does not silently resume a
different episode.

## Recommendations and taste memory

Recommendation discovery only returns titles available through enabled household
subscriptions. It uses TMDB for genre, release year, user rating, and popularity
filtering, with availability data supplied by JustWatch through TMDB. Critic-score
filtering falls back to Watchmode because TMDB does not expose a comparable critic
score. Complete streaming-profile history is not available, so results exclude the
local watch ledger and locally learned preferences instead.

```bash
curl -X POST http://127.0.0.1:8787/api/recommendations \
  -H 'content-type: application/json' \
  -d '{
    "media_type":"movie",
    "genres":["Horror"],
    "year_min":1990,
    "year_max":1999,
    "user_rating_min":7,
    "limit":5
  }'

curl -X PUT http://127.0.0.1:8787/api/taste \
  -H 'content-type: application/json' \
  -d '{"title":"The Thing","media_type":"movie","status":"liked"}'
```

Preference meanings are deliberately distinct:

- `liked`: accepted, explicitly praised, or requested for playback
- `disliked`: a bad choice or something the user does not want recommended
- `deferred`: not wanted right now; excluded for 30 days and then eligible again

Set `HOUSEHOLD_PROFILE` in `.env` to choose the default taste profile. Later, separate
front-end or voice profiles can use the same APIs without mixing household preferences.

Provider URL formats are calibrated once, not once per title. To test Disney+ using a catalog result:

```bash
# 1. Search and inspect subscription availability.
curl 'http://127.0.0.1:8787/api/catalog/search?q=Loki'

# 2. Build and cache a candidate without touching the TV.
curl -X POST http://127.0.0.1:8787/api/content/plan \
  -H 'content-type: application/json' \
  -d '{"title":"Loki","provider":"Disney+","media_type":"series"}'

# 3. Test the unverified provider pattern once. This controls the TV.
curl -X POST http://127.0.0.1:8787/api/content/play \
  -H 'content-type: application/json' \
  -d '{
    "device_id":"living-room",
    "title":"Loki",
    "provider":"Disney+",
    "media_type":"series",
    "allow_unverified":true
  }'

# 4. Only after confirming the expected title opened, trust this provider pattern.
curl -X PUT http://127.0.0.1:8787/api/provider-resolvers/disney_plus \
  -H 'content-type: application/json' \
  -d '{"verified":true,"notes":"Loki deep link opened correctly"}'
```

After step 4, newly resolved Disney+ titles using that URL format do not require individual verification. Repeat the one-time calibration for Netflix, Max, Prime Video, and Hulu. If a provider changes its URL/deep-link format, mark its resolver unverified and recalibrate it.

The LLM can search, plan, and play verified links. It cannot set `allow_unverified`; that flag is intentionally limited to the explicit HTTP calibration workflow.

Every API call needs the pairing code (see "Pairing a screen"). Keep Friday on your home network; do not expose this control API directly to the internet.

## Who is talking (voice profiles)

Speaker identification runs locally on the CPU with a 26 MB WeSpeaker model
through `sherpa-onnx`; nothing leaves the machine and no audio is kept.

```
mkdir -p data/models
curl -L -o data/models/wespeaker_en_voxceleb_resnet34_LM.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx
```

How it behaves:

- Every chat (NEW CHAT, or a fresh conversation-mode session) locks onto one
  speaker from its first two to three seconds of speech. Short follow-ups
  (“yes”, “mhmm”) are never re-identified.
- A recognized voice applies its profile to taste memory, recommendations, and
  feedback automatically; the chip in the masthead shows who the chat is
  locked to.
- An unrecognized voice is asked once, after its request is handled, for a
  name — with the option to stay anonymous. A name enrolls the voiceprint from
  the audio already heard (`voice_enroll`); declining keeps the chat anonymous
  and records no personal taste feedback (`voice_stay_anonymous`).
- Only the 256-number voiceprint is stored per profile (`voiceprints` table);
  the audio behind it is discarded as soon as it is computed.

### First-time setup

While nobody is enrolled, the first chat is an introduction: the assistant
greets you, asks your name (learning your voice from what you already said),
then asks whether you want to set up preferences now or later. If now, it asks
— one question at a time — what you like and dislike, a little about you,
whether others live with you (their profiles are created by name; their voices
are learned the first time they talk), and household-wide preferences everyone
shares. Everything is saved as **profile notes** — plain sentences in your own
words — which recommendations read alongside learned title reactions. "Later"
means it learns as it goes — from reactions to titles and from anything people
say about themselves — and never brings setup up again on its own. Anyone can
say "let's set up my profile" or "edit my preferences" at any time; notes can
be added and outdated ones removed. Progress is kept in the `settings` table
(`setup_status`), notes in `profile_notes`; `/api/health` shows the status.

Manage voiceprints with `GET /api/voice/profiles` and
`DELETE /api/voice/profiles/{name}`. Scores are logged per chat
(`voice: conversation … -> identified as Alex (score 0.81, …)`); tune
`threshold` in `[voice]` if two household voices are being confused (raise
it) or a known voice is missed (lower it). Voiceprints are compared after
subtracting an average voice (a generic one shipped with the app, blended with
the household's own once two or more people are enrolled), which cancels what
every recording shares — the same phone, room, and speaker — and leaves the
voice itself. Set `enabled = false` or delete the model file to turn it off;
everything else keeps working.

## Smart lights and devices (Matter and Zigbee)

Lights, plugs, and switches are controlled by voice through one hub with two
backends, so devices from either ecosystem work the same way ("turn on the
kitchen light", "make it warmer", "dim to 20").

**Matter** (most new WiFi bulbs and plugs, including anything with the Matter
logo) is spoken through the open-source `matter-server` controller — the same
one Home Assistant uses. It runs as its own small service because its native
CHIP wheels need Python 3.12:

```bash
# once:
uv tool install --python 3.12 "python-matter-server[server]"
sudo mkdir -p /data && sudo chown $USER /data   # the CHIP build expects it
```

That is the whole setup: house-agent starts `matter-server` itself (through
`scripts/matter-server.sh`) when it starts, restarts it if it crashes, streams
its log into house-agent's own (logger `matter-server`), and stops it when
house-agent exits. It costs about 90 MB of memory and a few seconds of CPU a
day. To run the controller separately instead — on another machine, or as the
systemd user unit in `scripts/matter-server.service` — set `manage = false`
under `[matter]`; if the unit is running, house-agent notices the lock and
simply connects to it.

house-agent connects to `ws://127.0.0.1:5580/ws` (see `[matter]` in
config.toml), reconnecting quietly in the background whenever the controller
is up. Pairing a device without a Bluetooth adapter on the server: set the
device up in its vendor app first (AiDot/Linkind, etc.) so it joins the home
WiFi. The device's Matter pairing window only opens for ~15 minutes after
power-on, so cut its power completely at the wall switch or plug (not from
an app), wait 10 seconds, turn it back on, then immediately tell the
assistant to pair it and read out the pairing code. With a Bluetooth adapter
on the server (set `bluetooth = true` — and `bluetooth_adapter` if it is not
hci0 — plus HOUSE_WIFI_SSID/HOUSE_WIFI_PASSWORD in .env), brand-new devices
pair straight from the box code: house-agent starts matter-server with
`--bluetooth-adapter` and hands it the WiFi. "Remove the bedroom lamp" unpairs
a device (it forgets this controller and returns to pairing mode) and drops its
name and room. The Matter fabric lives in `~/.matter_server` — back it up;
losing it means re-pairing everything.

**Protecting the pairing list.** matter-server keeps its paired devices in
`~/.matter_server/<fabric-id>.json` with one `.backup` that every save
overwrites, and if that file is missing or empty at start-up it silently
starts with no devices — every light looks unpaired even though the devices
themselves still trust the controller. `scripts/matter-server.sh` now runs
`scripts/matter-storage-guard.py` around the server: it snapshots the pairing
list into `~/.matter_server/snapshots/` whenever it has devices (never when it
is empty), and if the server would start with an empty list it restores the
newest snapshot first. The unit also snapshots at shutdown. The wrapper takes
a lock on the storage folder, so starting a second copy by hand while the
service runs fails immediately instead of both writing the same file. If a
device does go missing, the assistant says so by name (`device_list` reports
it under `missing`) rather than claiming nothing is paired; a snapshot with
that device can be restored by stopping the service, running
`scripts/matter-storage-guard.py restore-if-empty ~/.matter_server`, and
starting it again.

**Zigbee** needs a USB coordinator dongle (e.g. Sonoff ZBDongle-E), an MQTT
broker (`mosquitto`), and [Zigbee2MQTT](https://www.zigbee2mqtt.io/). Both of
those run in containers from `docker-compose.zigbee.yml`, which house-agent
starts and stops with itself — see [Containers](#containers). Set
`enabled = true` under `[zigbee]` in config.toml and devices Zigbee2MQTT knows
appear automatically; "pair a zigbee device" opens the join window. No
Bluetooth is ever involved.

If the dongle is unplugged, or re-enumerates, Zigbee2MQTT exits and does not
come back by itself — every Zigbee light then looks dead while house-agent
keeps running. `GET /api/health` shows it as `zigbee: connected: false`, and
`docker compose logs zigbee2mqtt` ends in `Adapter disconnected, stopping`.
Restarting house-agent brings it back up.

After pairing, the assistant asks what to call the device and which room it is
in; those names live in the `smart_devices` table and survive restarts.

```text
GET  /api/devices          merged device list with live state
POST /api/devices/set      {"device": "kitchen light", "power": "on", "brightness": 40, "color_temp": 2700}
POST /api/devices/pair     {"network": "matter", "code": "1234-567-8901"}
PUT  /api/devices/alias    {"device": "matter-5", "name": "Desk lamp", "room": "office"}
```

## Reminders, plans, light schedules, lists, and recipes

Friday keeps its own clock. Anything set out loud is stored in SQLite and run
by a scheduler inside the server, and whatever it needs to say is pushed to
every open browser tab over `GET /api/events` (Server-Sent Events) and spoken
there. Nothing here needs an account or a cloud service.

Things you can say:

- "Remind me to call grandma tomorrow morning" — a one-off spoken reminder
  (`reminder_add`). "Every weekday at 7, remind me to take my vitamins" repeats.
- "Let me know if it'll rain before my 2pm meeting" — a scheduled *check*:
  at the chosen time Friday answers the question with its own tools (weather,
  directions, web) and reads out the answer.
- "Gradually brighten the master bedroom starting at 7:30" or "from 10pm slowly
  fade the lights until they're out at 11" — `light_schedule_add`, a daily
  fade of brightness (and optionally color temperature) over any number of
  minutes. "Slowly dim the kitchen over ten minutes" does the same starting now.
- "What's scheduled?" / "Cancel the bedroom sunrise" — `automation_list` and
  `automation_delete`.
- "Remind me I need eggs", "we're out of milk", "what's on my shopping list?",
  "got the eggs" — the shopping list (`list_add`, `list_show`, `list_remove`).
  Other lists ("add 'fix the fence' to my to-do list") work the same way.
- "Find me a fluffy pancake recipe", "what's a good high-protein breakfast?",
  "something quick for the family tonight" — `recipe_search` pulls the
  structured recipe data (schema.org JSON-LD) that recipe sites embed, so
  Friday gets ingredients, times, servings, rating, calories and protein
  instead of a blog post. "Read me the steps" is `recipe_get`; "save that one"
  keeps it; "put the ingredients on the shopping list" does exactly that.

- "I've got Ana's party at the park at 4 tomorrow" — Friday remembers plans
  (`calendar_add`). Today's and tomorrow's plans ride along in every turn, so
  "I'm heading out" gets a weather check for right now, and talking about
  the party gets one for four o'clock (`weather_at`, hour-by-hour) — Friday
  warns about rain, storms, heat, cold, or wind on its own and stays quiet
  when it's fine. An hour before each plan it checks again and speaks up
  only if there's something to prepare for. "What's on tomorrow?" and
  "when's the concert?" are `calendar_events`.
- Meetings and events already on a Google, iCloud, or Outlook calendar are
  read in too, recurring ones included, and get the same heads-up an hour
  before (skipped for video calls). See "Connecting calendars" below.

### Connecting calendars

Both directions are free and need no sign-up or OAuth: everything is a
private iCalendar URL.

**Friday → your phone** (plans said out loud appear in your calendar app):

- *iPhone/iPad:* Settings → Calendar → Accounts → Add Account → Other →
  **Add Subscribed Calendar** → `https://friday.local:8787/calendar.ics`.
  Friday's certificate is self-signed, so iOS will warn; tap Continue, or
  run `scripts/make-cert.sh` with [mkcert](https://github.com/FiloSottile/mkcert)
  installed and put its root CA on each phone once (the script prints the
  file) — then every device trusts Friday with no warnings, browser included. Works on home
  Wi-Fi (or over Tailscale/VPN). Refresh cadence is under Settings →
  Calendar → Accounts → Fetch New Data.
- *Android:* the Google Calendar app cannot subscribe to a LAN URL itself
  (Google's "From URL" is fetched by Google's servers, which cannot see
  your house). Install the free **ICSx⁵** app, add the same URL, and the
  events show in Google Calendar as a local calendar.
- *Mac:* Calendar → File → New Calendar Subscription.

Ask Friday "how do I get your calendar on my phone?" — it asks whether you use Apple
Calendar or Google Calendar, then walks through the matching steps out loud. Each
plan in the feed carries a 30-minute alert, so subscribed phones ring for it.

**Your calendars → Friday** (so it knows about the 4 pm meeting):

1. Get the private ICS link: Google Calendar on the web → Settings → pick
   the calendar → *Integrate calendar* → **Secret address in iCal format**;
   iCloud → Calendar → share icon → *Public Calendar* → copy the link;
   Outlook → Settings → Calendar → *Shared calendars* → Publish → ICS.
2. Put it in `.env`: `CALENDAR_ICS_URLS=https://…/basic.ics` (comma-separate
   several). The link is the secret — keep it out of `config.toml` and git.
3. Restart Friday. It syncs every 15 minutes (`[calendar] refresh_minutes`),
   `POST /api/calendar/refresh` forces one, and `GET /api/calendar/sources`
   shows what it read. Subscribed events are read-only from Friday's side.

Behavior worth knowing:

- Times are local to the server. A repeating job that came due while the
  server was off (more than 45 minutes ago) is skipped and rescheduled rather
  than run late; one-off reminders still fire late rather than be lost.
- Starting a new fade on a light cancels one already running on it, and a
  fade that ends at 0% turns the light off.
- A browser that was closed when a reminder fired replays the backlog when it
  reopens (the last 50 announcements are kept in memory).
- Reminders play through the browser's speaker when sound is on or
  conversation mode is active; with sound off they only appear in the
  transcript.

Endpoints: `GET /api/automations`, `POST /api/automations/{id}/run`,
`PUT /api/automations/{id}/enabled?enabled=false`, `DELETE /api/automations/{id}`,
`GET /api/lists`, `POST /api/lists/{name}` (`{"items": [...]}`),
`DELETE /api/lists/{name}?item=eggs`, `GET /api/recipes?q=chicken`, `GET /api/calendar?days=7`, `POST /api/calendar`
(`{"title": ..., "when": "YYYY-MM-DD HH:MM", "location": ...}`), `DELETE /api/calendar/{id}`, and
`POST /api/announce` (`{"text": "..."}`) to push a line to every screen —
useful for testing the channel, or for other programs on the LAN.

## Pairing a screen

Every API call needs the house's pairing code, so nobody on the Wi-Fi can drive the TV or order food just by finding the address. The first time house-agent starts it mints one, prints it (“Pairing code for the web app: …”), and saves it to `.env` as `API_TOKEN`; set `API_TOKEN` yourself to choose it. Each browser asks for the code once and keeps it. `/api/health` and the app shell stay open; the calendar feed's own address is its secret.

## Fitting the model's window

Friday has ninety-odd tools, and their schemas plus the full guidance run past a 16k-token context before any history. Ollama answers an over-long prompt by keeping its tail and dropping the front — the system prompt and most tools — which is exactly the turn where Friday forgets who it is. So each turn is routed: a core set (lists, messages, web) is always there, and a domain (TV, lights, DoorDash, calendar, weather, travel, recipes, profile, house settings) rides along only when the request, the last few turns, or something in progress points at it; its guidance paragraphs come and go with it. `[llm] tool_budget_tokens` caps the schemas per turn and `route_tools = false` sends everything. History is then trimmed to what is left of `context_tokens`, which the agent checks against Ollama at startup. `scripts/house-qwen.Modelfile` builds the house model with a 32k window (`ollama create house-qwen -f scripts/house-qwen.Modelfile`).

## Ordering from DoorDash

Friday can search DoorDash, read menus, build a cart, reorder a past order, price it, and place it — through DoorDash's own command-line tool, `dd-cli` (a beta, by invitation: [doordash-oss/doordash-cli](https://github.com/doordash-oss/doordash-cli)). Nothing runs against DoorDash except that binary; Friday shells out to it with `--json-output` and trims the answer to what a spoken reply needs.

Setup, once, on the house computer:

```bash
# 1. Download the linux-amd64 release and verify it against the published checksum
curl -LO https://github.com/doordash-oss/doordash-cli/releases/download/v0.2.4/dd-cli-v0.2.4-linux-amd64.tar.gz
curl -LO https://github.com/doordash-oss/doordash-cli/releases/download/v0.2.4/dd-cli-v0.2.4-linux-amd64.tar.gz.sha256
sha256sum -c dd-cli-v0.2.4-linux-amd64.tar.gz.sha256
tar xzf dd-cli-v0.2.4-linux-amd64.tar.gz
# 2. Install (lands in ~/.local/bin/dd-cli); Friday finds it there even when it is not on PATH
bash dd-cli-v0.2.4-linux-amd64/install.sh
# 3. Sign in — opens a browser, keeps the token in the OS keychain
dd-cli login
```

Or let Friday do the sign-in. The first DoorDash request while signed out (or "sign in to DoorDash") runs `dd-cli login` and puts DoorDash's sign-in link in the messages panel (the envelope icon). `dd-cli` gives each link five minutes; when one lapses unused Friday mints a fresh attempt and swaps the new link into the same card, for half an hour, after which asking again makes a new one. On the house computer, sign in and she announces when it is done. On a phone the final page will not load, because DoorDash redirects to `localhost:4180` on the house computer; copy that address from the phone's address bar and paste it into the chat, and Friday hands the code to `dd-cli` from there.

Each chat has its own messages board: NEW CHAT starts an empty one.

A machine with no keychain (a headless box, a container) takes the token from `dd-cli export-token` run anywhere you can sign in, pasted into `.env` as `DD_CLI_ACCESS_TOKEN`. Restart house-agent after installing so it notices the binary; until it is signed in every DoorDash tool answers `needs_setup` and Friday offers to sign in.

What you can ask for:

- “A cheeseburger from McDonald's”, “two pad thai from Wanida”, “add fries” — `doordash_order`, the whole thing in one call: it finds the place near home, picks the nearest open branch of a chain, finds each dish on the menu (the plain “Cheeseburger” over the Double), adds it to the cart there, and prices it, so Friday reads back the total and card straight away. A dish already in that cart is not added twice.
- “Find thai near here”, “is there a good ramen place?”, “search DoorDash for Zeeks” — `doordash_search`, from the account's default address (or a saved label, only when it is said in that request: “near work”).
- “What's on the menu?”, “do they have pad thai?”, “how much is the pepperoni?” — `doordash_menu` and `doordash_item_details`.
- “Add two pad thai and a Thai iced tea” — `doordash_cart_add`; when an item needs a choice (size, spice level) Friday asks it and adds again.

Placing needs nothing copied: once a cart is priced, “yes” is `doordash_order_place` with `confirmed` and a tip of “suggested”, “none”, or an amount in words; the house supplies the cart id itself, and a wrong id from the model is replaced by this chat's cart. Each chat keeps a small working memory of its DoorDash context — the stores found, the store in play, the cart and its price — held in memory only and shown to the model with every turn, so “now from McDonald's” lands on the McDonald's it already saw instead of a fresh search. It is never written to the database.
- “Order milk, eggs, and bread”, “get my shopping list delivered” — `doordash_grocery_list`, which builds a grocery cart at a nearby store and reports anything it could not find.
- “What did I order last time?”, “what's my usual?”, “how much did I spend on DoorDash last month?” — `doordash_order_history` (with `usual_places`, the stores ordered from most) and `doordash_receipt`.
- “Reorder my usual from Zeeks” — `doordash_reorder` builds the cart from the last order there and prices it in one step.
- “My favorite pizza place is Zeeks; my usual is a large pepperoni” — `doordash_favorite` remembers the name and the usual order in the household's own words, and the house line reminds Friday of them.
- “Where's my order?” — `doordash_order_status`; after placing, Friday watches the order and announces when the store confirms it, when the Dasher is nearby, and when it arrives.
- Addresses, cards on file, and store deals — `doordash_account` and `doordash_promo`.

Placing an order is deliberately slow: Friday always previews first (`doordash_order_preview`), reads back the items, the total, the delivery time, the card on file, and the Dasher tip, and calls `doordash_order_place` only on a clear yes. The tool itself refuses without a fresh preview, without `confirmed`, and without an agreed tip on a delivery. Age-restricted carts cannot be placed by an assistant; Friday hands over a checkout link instead.

DoorDash's CLI terms make you responsible for every order the agent places, and forbid keeping what the CLI returns. So this house stores none of it: tool results are redacted from the saved conversation trace, and the only DoorDash-related memory is the favorites you dictate. See `LICENSE.txt` in the dd-cli tarball before turning this on.

## Calibrating a profile routine

Streaming-app menus change, so UI macros live in SQLite rather than in the LLM prompt. Watch the TV and create a routine whose waits and keys match the actual screen:

```bash
curl -X PUT http://127.0.0.1:8787/api/routines/netflix-mom \
  -H 'content-type: application/json' \
  -d '{
    "name": "netflix-mom",
    "device_id": "living-room",
    "description": "Launch Netflix and choose the Mom profile",
    "actions": [
      {"type": "launch", "app": "Netflix"},
      {"type": "wait", "milliseconds": 4000},
      {"type": "key", "key": "Right"},
      {"type": "key", "key": "Select"}
    ]
  }'
```

Valid routine actions are `key`, `text`, `wait`, and `launch`. A routine is limited to 40 actions and each wait to five seconds. Longer loading flows should use several bounded waits so the trace remains explicit.

## The app

The page at `/` is the whole house in one place. On a computer the sections
sit in a rail on the left; on a phone they are a tab bar along the bottom,
and “Add to Home Screen” gives it an icon and a full-screen window.

- **Chat** — type a message and SEND, or start CONVO for a hands-free spoken
  back-and-forth (it always talks back). **History** opens
  every past chat on this house; tap one to pick it back up, swipe it left
  (or use the bin) to delete it, messages and all.
- **Devices** — every Matter and Zigbee light and switch, grouped by room,
  with live state. Power is a switch; brightness, warmth (candle to daylight)
  and colour (a slide through the rainbow) are sliders. The card glows the
  colour the light is showing. Rename, move to a room, blink to identify,
  remove, and pair new devices from here.
- **TV** — a remote for each saved television. The trackpad in the middle
  is slid to move (each stretch of travel is one arrow press, so a long swipe
  walks several rows) and tapped to select. Volume is a strip slid up or
  down. Below the remote, Apps / Inputs / Keyboard switch between a grid of
  app tiles, input tiles (HDMI, tuner, and any names from `[roku.inputs]`),
  and a field that types into the TV's search.
- **Home** — a summary strip (next event, active automations, open list
  items) that jumps to each section; the week's calendar grouped by day;
  automations split into active and paused (toggle, run now, delete); and
  the lists, one at a time with a tab per list.
- **People** — everyone the house knows, plus “Everyone” for the household.
  Each person has three kinds of note, written in plain words and editable in
  place: **Rules for Friday** (standing instructions followed on every turn,
  before anything else in the prompt), **How to talk to me** (reply style),
  and **Likes & tastes** (what shapes recommendations). Rules and style notes
  ride into every turn; tastes stay with recommendations.
- **Settings** — the televisions (name, address, port; add one by hand or
  from a network scan; remove), DoorDash (installed, signed in, whose account
  by its default address, start or finish a sign-in), the Wi‑Fi handed to new
  devices, voices Friday recognises, and what the system is running on.

Adding a second TV while `ROKU_IP_ADDRESS` is set in `.env` moves that TV's
entry into `config.toml` and comments the override out, so both load from the
same file from then on. Every change on the page takes effect immediately and
survives a restart.

```text
GET    /api/conversations                 past chats, newest first
GET    /api/conversations/{id}            one chat's messages
DELETE /api/conversations/{id}            forget a chat
GET    /api/profiles                      everyone, with voice and note counts
GET    /api/profiles/{profile}/notes      rules, style notes, tastes
POST   /api/profiles/{profile}/notes      {"note": "...", "kind": "rule|style|taste"}
PUT    /api/notes/{id}                    {"note": "..."}
DELETE /api/notes/{id}
GET    /api/tvs                           saved TVs with reachability
POST   /api/tvs                           {"id": "den", "name": "Den TV", "host": "192.168.0.70", "platform": "roku"}
PUT    /api/tvs/{id}                      {"name": "...", "host": "...", "port": 8060}
DELETE /api/tvs/{id}
POST   /api/tvs/discover
GET    /api/doordash/status?probe=true    probe runs dd-cli to check the sign-in
POST   /api/doordash/login                {"conversation_id": "..."}
GET    /api/house                         Wi-Fi, model, backends, config files
PUT    /api/house/wifi                    {"ssid": "...", "password": "..."}
```

## Useful endpoints

```text
POST /api/chat
POST /api/voice
POST /api/speech
GET  /api/roku/devices
POST /api/roku/discover
GET  /api/roku/{device_id}/status
GET  /api/roku/{device_id}/apps
POST /api/roku/{device_id}/key
POST /api/roku/{device_id}/text
POST /api/roku/{device_id}/launch
GET  /api/routines
PUT  /api/routines/{name}
POST /api/routines/{name}/run
POST /api/watch-history
GET  /api/watch-history/{profile}
GET  /api/subscriptions
PUT  /api/subscriptions
GET  /api/catalog/search
GET  /api/content-links
POST /api/content-links
POST /api/content/plan
POST /api/content/play
GET  /api/provider-resolvers
PUT  /api/provider-resolvers/{provider}
```

## “Find me something new in the Top 10”

That request needs both current catalog rankings and complete profile watch history. The catalog adapter can filter availability to enabled subscriptions. The local watch ledger can exclude known watched titles, but an empty ledger means unknown rather than unwatched. Netflix viewing activity can be imported separately; other providers may require conversational updates or account exports.

## Tests

```bash
pytest
```

The Roku tests use a mocked ECP transport and do not send commands to a real TV.

## Reference behavior

Roku ECP is a LAN REST API on port `8060`, discovered through SSDP. As of Roku OS 14.1, remote commands require “Control by mobile apps” to be enabled. Home Assistant documents the same local-control setting, supported keys, app discovery, and provider-dependent deep links:

- [Roku External Control Protocol](https://developer.roku.com/dev/docs/external-control-api)
- [Home Assistant Roku integration](https://www.home-assistant.io/integrations/roku)

## Surviving restarts and power cuts

Schedules, reminders, and lists live in `data/house-agent.db`, so nothing is
lost when house-agent stops. A fade that was under way is written to the
database as it starts and picked up again at the next start from where the
clock says it should be (or set straight to its final level if it has already
ended). A light schedule that came due while the machine was off still runs
for up to six hours afterwards — a fade joins in progress, a plain on/off is
applied — while a spoken reminder that is more than 45 minutes late is marked
missed rather than announced at the wrong time. To have house-agent itself
come back after a reboot or a power cut, install `scripts/house-agent.service`
(instructions at the top of the file).
