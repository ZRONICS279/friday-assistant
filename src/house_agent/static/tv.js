// The remote. A trackpad in the middle: slide a finger to move the
// highlight (each stretch of travel is one arrow press, so a long swipe
// walks several rows), tap to select. Volume is a strip you slide up or
// down. Apps and inputs are chips.
(() => {
  const view = document.querySelector('.view[data-view="tv"]');
  const picker = document.querySelector("#tv-picker");
  const title = document.querySelector("#tv-title");
  const remote = document.querySelector("#remote");
  const emptyNote = document.querySelector("#tv-empty");
  const statusDot = document.querySelector("#tv-status-dot");
  const statusText = document.querySelector("#tv-status-text");
  const trackpad = document.querySelector("#trackpad");
  const arrow = document.querySelector("#trackpad-arrow");
  const volume = document.querySelector("#volume");
  const knob = document.querySelector("#volume-knob");
  const appsHost = document.querySelector("#tv-apps");
  const inputsHost = document.querySelector("#tv-inputs");
  const typeForm = document.querySelector("#tv-type-form");
  const typeInput = document.querySelector("#tv-type");
  const segments = document.querySelector("#tv-segments");
  const recentsHost = document.querySelector("#tv-recents");
  const recentsList = document.querySelector("#tv-recents-list");
  const appFilter = document.querySelector("#tv-app-filter");
  const RECENT_MAX = 12;

  function showPanel(name) {
    for (const button of segments.querySelectorAll(".segment")) {
      button.setAttribute("aria-selected", String(button.dataset.panel === name));
    }
    for (const panel of remote.querySelectorAll(".panel")) panel.hidden = panel.dataset.panel !== name;
  }
  segments.addEventListener("click", (event) => {
    const button = event.target.closest(".segment");
    if (button && !button.hidden) showPanel(button.dataset.panel);
  });

  function tile(label, { sub, onClick } = {}) {
    const letters = String(label).replace(/[^A-Za-z0-9 ]/g, "").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
    const button = Friday.el(
      "button",
      { class: "tile", type: "button", title: label },
      Friday.el("span", { class: "tile-mark", dataset: { hue: String([...String(label)].reduce((a, c) => a + c.charCodeAt(0), 0) % 360) } }, letters),
      Friday.el("span", { class: "tile-label" }, label),
      sub ? Friday.el("span", { class: "tile-sub" }, sub) : null,
    );
    button.style.setProperty("--tile-hue", button.querySelector(".tile-mark").dataset.hue);
    button.addEventListener("click", async () => {
      button.classList.add("busy");
      try {
        await onClick?.();
      } finally {
        button.classList.remove("busy");
      }
    });
    return button;
  }

  // An app as a card: the TV's own artwork when it has some (Roku does),
  // the initials on a tinted square when it doesn't or the image fails.
  function appCard(app, { compact = false } = {}) {
    const hue = [...String(app.name)].reduce((a, c) => a + c.charCodeAt(0), 0) % 360;
    const letters = String(app.name).replace(/[^A-Za-z0-9 ]/g, "").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
    const art = Friday.el("span", { class: "app-art" }, Friday.el("span", { class: "app-letters" }, letters));
    art.style.setProperty("--tile-hue", String(hue));
    const url = iconUrl(app.id);
    if (url) {
      const img = Friday.el("img", { src: url, alt: "", loading: "lazy", decoding: "async" });
      img.addEventListener("load", () => art.classList.add("has-icon"));
      img.addEventListener("error", () => img.remove());
      art.append(img);
    }
    const button = Friday.el(
      "button",
      { class: `app-card${compact ? " compact" : ""}`, type: "button", title: app.name, dataset: { name: app.name.toLowerCase() } },
      art,
      Friday.el("span", { class: "app-name" }, app.name),
    );
    button.addEventListener("click", async () => {
      button.classList.add("busy");
      try {
        await api("/launch", { app: app.id || app.name });
        remember(app);
        Friday.toast(`Opening ${app.name}.`);
        setTimeout(refreshStatus, 1500);
      } catch (error) {
        Friday.toast(error.message, "error");
      } finally {
        button.classList.remove("busy");
      }
    });
    return button;
  }

  function iconUrl(appId) {
    const tv = tvs.find((t) => t.id === current);
    if (!appId || tv?.platform !== "roku") return null;
    const params = window.FridayAuth.withToken(new URLSearchParams());
    const query = params.toString();
    return `/api/roku/${encodeURIComponent(current)}/apps/${encodeURIComponent(appId)}/icon${query ? `?${query}` : ""}`;
  }

  // ---- recently used apps, newest first, kept per TV in this browser -------
  const recentKey = () => `friday-tv-recent:${current}`;

  function readRecents() {
    try {
      const list = JSON.parse(localStorage.getItem(recentKey()) || "[]");
      return Array.isArray(list) ? list.filter((a) => a && a.name) : [];
    } catch {
      return [];
    }
  }

  function remember(app) {
    if (!app?.name || String(app.id || "").startsWith("tvinput")) return;
    const list = readRecents().filter((a) => (app.id ? a.id !== app.id : a.name !== app.name));
    list.unshift({ id: app.id || null, name: app.name });
    try {
      localStorage.setItem(recentKey(), JSON.stringify(list.slice(0, RECENT_MAX)));
    } catch {}
    renderRecents();
  }

  function renderRecents() {
    const list = readRecents();
    recentsHost.hidden = list.length === 0;
    recentsList.replaceChildren(...list.map((app) => appCard(app, { compact: true })));
    recentsList.scrollLeft = 0;
  }

  // The app drawer, sorted the way people reach for things: the big
  // streaming services first, then news, sports, and the rest by kind.
  // Matched on the app's name, so a new install lands in the right place;
  // anything unrecognised goes to Extras. Order within a group follows the
  // pattern order, then the alphabet.
  const APP_GROUPS = [
    ["Streaming", [/^netflix/, /^youtube$/, /^disney(?! junior)/, /^hulu/, /^(hbo )?max\b|^hbo/, /^prime video|amazon/, /^apple tv/, /^paramount/, /^peacock/, /^starz|^showtime|^mgm|^amc\+|^britbox|^crunchyroll|^discovery\+|^youtube tv/]],
    // Live-TV services list "news" among everything else they carry.
    ["News", [/^(?!sling|fubo|vix|philo)(?:.*\bnews\b|daily wire|newsmax|cnn|fox news|msnbc|bbc|weather|local now)/]],
    ["Sports", [/^espn|^nfl|^nba|^mlb|^nhl|^b1g|^ncaa|march madness|^fubo|^(?!sling|philo).*\bsports?\b|^fox sports|^dazn|^peacock sports|^golf/]],
    ["Live TV & Channels", [/^sling|^philo|^directv|^frndly|^vix|^nbc|^cbs|^abc\b|^fox\b|^the cw|^pbs(?! kids)|^a&e|^travel channel|^hgtv|^food network|^history|^lifetime|^tlc|^bravo|^usa network/]],
    ["Free TV", [/^the roku channel$|^pluto|^tubi|^freetv|^freevee|^plex|^xumo|^crackle|^sling freestream/]],
    ["Kids & Family", [/\bkids?\b|family|^pbs kids|^nick|^cartoon|^disney junior/]],
    ["Music", [/^spotify|^pandora|^iheart|^amazon music|^apple music|^tidal|^siriusxm|^youtube music/]],
  ];

  function groupApps(apps) {
    const groups = new Map([...APP_GROUPS.map(([name]) => [name, []]), ["Extras", []]]);
    for (const app of apps) {
      const name = String(app.name).toLowerCase();
      let placed = false;
      for (const [group, patterns] of APP_GROUPS) {
        const rank = patterns.findIndex((pattern) => pattern.test(name));
        if (rank >= 0) {
          groups.get(group).push({ app, rank });
          placed = true;
          break;
        }
      }
      if (!placed) groups.get("Extras").push({ app, rank: 0 });
    }
    return [...groups]
      .filter(([, items]) => items.length)
      .map(([group, items]) => [group, items.sort((a, b) => a.rank - b.rank || a.app.name.localeCompare(b.app.name)).map((i) => i.app)]);
  }

  let tvs = [];
  let aliases = {};
  let current = localStorage.getItem("friday-tv") || null;
  let statusTimer = null;
  let powered = null;

  const api = (path, body) =>
    Friday.api(`/api/roku/${encodeURIComponent(current)}${path}`, body ? { method: "POST", body } : {});

  async function press(key, count = 1) {
    if (!current) return;
    try {
      await api("/key", { key, count });
    } catch (error) {
      Friday.toast(`${key.replace(/_/g, " ")}: ${error.message}`, "error");
    }
  }

  function flash(direction) {
    arrow.dataset.dir = direction;
    arrow.classList.remove("show");
    void arrow.offsetWidth;
    arrow.classList.add("show");
  }

  // ---- trackpad --------------------------------------------------------------
  const STEP = 56; // px of travel per arrow press
  let pad = null;
  trackpad.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    trackpad.setPointerCapture(event.pointerId);
    pad = { x: event.clientX, y: event.clientY, ox: event.clientX, oy: event.clientY, moved: false, at: Date.now() };
    trackpad.classList.add("touch");
  });
  trackpad.addEventListener("pointermove", (event) => {
    if (!pad) return;
    const dx = event.clientX - pad.ox;
    const dy = event.clientY - pad.oy;
    if (Math.abs(dx) >= STEP || Math.abs(dy) >= STEP) {
      pad.moved = true;
      let key;
      if (Math.abs(dx) > Math.abs(dy)) {
        key = dx > 0 ? "right" : "left";
        pad.ox += Math.sign(dx) * STEP;
        pad.oy = event.clientY;
      } else {
        key = dy > 0 ? "down" : "up";
        pad.oy += Math.sign(dy) * STEP;
        pad.ox = event.clientX;
      }
      flash(key);
      press(key);
    } else if (Math.abs(event.clientX - pad.x) > 10 || Math.abs(event.clientY - pad.y) > 10) {
      pad.moved = true;
    }
  });
  const padEnd = (event) => {
    if (!pad) return;
    trackpad.classList.remove("touch");
    const quick = Date.now() - pad.at < 350;
    const still = Math.abs(event.clientX - pad.x) < 10 && Math.abs(event.clientY - pad.y) < 10;
    if (!pad.moved && (quick || still)) {
      flash("select");
      press("select");
    }
    pad = null;
  };
  trackpad.addEventListener("pointerup", padEnd);
  trackpad.addEventListener("pointercancel", () => {
    pad = null;
    trackpad.classList.remove("touch");
  });
  trackpad.addEventListener("keydown", (event) => {
    const key = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right", Enter: "select", " ": "select" }[event.key];
    if (!key) return;
    event.preventDefault();
    flash(key);
    press(key);
  });

  // ---- volume strip --------------------------------------------------------
  // The TV only knows up and down, so the strip is relative: every 22px of
  // travel is one press, and the knob springs back when let go.
  const VOL_STEP = 22;
  let vol = null;
  volume.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    event.preventDefault();
    volume.setPointerCapture(event.pointerId);
    vol = { y: event.clientY, origin: event.clientY };
    volume.classList.add("touch");
  });
  volume.addEventListener("pointermove", (event) => {
    if (!vol) return;
    const travel = event.clientY - vol.origin;
    knob.style.transform = `translateY(${Math.max(-60, Math.min(60, travel))}px)`;
    const dy = event.clientY - vol.y;
    if (Math.abs(dy) >= VOL_STEP) {
      const steps = Math.trunc(dy / VOL_STEP);
      vol.y += steps * VOL_STEP;
      press(steps < 0 ? "volume_up" : "volume_down", Math.min(5, Math.abs(steps)));
      volume.classList.toggle("up", steps < 0);
      volume.classList.toggle("down", steps > 0);
    }
  });
  const volEnd = () => {
    vol = null;
    knob.style.transform = "";
    volume.classList.remove("touch", "up", "down");
  };
  volume.addEventListener("pointerup", volEnd);
  volume.addEventListener("pointercancel", volEnd);

  // ---- buttons -----------------------------------------------------------------
  for (const button of remote.querySelectorAll("button[data-key]")) {
    button.addEventListener("click", () => {
      let key = button.dataset.key;
      if (button.classList.contains("key-power")) {
        // Power flips whatever the TV last told us; unknown means "off".
        key = powered === false ? "power_on" : "power_off";
      }
      button.classList.add("pressed");
      setTimeout(() => button.classList.remove("pressed"), 160);
      press(key);
    });
  }

  typeForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = typeInput.value;
    if (!text.trim() || !current) return;
    try {
      await api("/text", { text });
      typeInput.value = "";
      Friday.toast("Typed on the TV.");
    } catch (error) {
      Friday.toast(error.message, "error");
    }
  });

  // ---- status, apps, inputs ------------------------------------------------
  async function refreshStatus() {
    if (!current) return;
    try {
      const status = await api("/status");
      const mode = String(status.power_mode || "").toLowerCase();
      powered = mode ? mode === "poweron" || mode === "on" : null;
      const app = status.active_app?.name;
      if (powered !== false && status.active_app && readRecents()[0]?.name !== app) remember(status.active_app);
      statusDot.className = `tv-status-dot ${powered === false ? "off" : "on"}`;
      statusText.textContent = powered === false ? "Standby" : app ? `On · ${app}` : "On";
    } catch (error) {
      powered = null;
      statusDot.className = "tv-status-dot bad";
      statusText.textContent = "Not answering";
    }
  }

  async function loadApps() {
    appsHost.replaceChildren(Friday.el("span", { class: "muted" }, "Loading…"));
    try {
      const data = await api("/apps");
      appsHost.replaceChildren();
      // Roku lists its HDMI ports as apps too; those live under Inputs.
      const apps = (data.apps || []).filter((a) => !String(a.id || "").startsWith("tvinput"));
      for (const [group, members] of groupApps(apps)) {
        appsHost.append(
          Friday.el(
            "section",
            { class: "app-group" },
            Friday.el("h3", { class: "app-group-head" }, group, Friday.el("span", { class: "count" }, String(members.length))),
            Friday.el("div", { class: "app-grid" }, members.map((app) => appCard(app))),
          ),
        );
      }
      filterApps();
      if (!apps.length) appsHost.replaceChildren(Friday.el("span", { class: "muted" }, "This TV does not list its apps."));
    } catch (error) {
      appsHost.replaceChildren(Friday.el("span", { class: "muted" }, error.message));
    }
  }

  function filterApps() {
    const term = appFilter.value.trim().toLowerCase();
    for (const card of appsHost.querySelectorAll(".app-card")) card.hidden = Boolean(term) && !card.dataset.name.includes(term);
    for (const group of appsHost.querySelectorAll(".app-group")) group.hidden = !group.querySelector(".app-card:not([hidden])");
  }
  appFilter.addEventListener("input", filterApps);

  function loadInputs() {
    inputsHost.replaceChildren();
    const items = [
      ...Object.entries(aliases).map(([name, key]) => [name.replace(/\b\w/g, (c) => c.toUpperCase()), name, key.replace(/^Input/, "")]),
      ["HDMI 1", "hdmi1", "HDMI1"],
      ["HDMI 2", "hdmi2", "HDMI2"],
      ["HDMI 3", "hdmi3", "HDMI3"],
      ["HDMI 4", "hdmi4", "HDMI4"],
      ["Live TV", "live tv", "Tuner"],
      ["AV", "av", "AV1"],
    ];
    const seen = new Set();
    for (const [label, target, sub] of items) {
      const key = target.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      inputsHost.append(
        tile(label, {
          sub,
          onClick: async () => {
            try {
              await api("/input", { input: target });
              Friday.toast(`Switched to ${label}.`);
            } catch (error) {
              Friday.toast(error.message, "error");
            }
          },
        }),
      );
    }
  }

  function select(id) {
    current = id;
    localStorage.setItem("friday-tv", id);
    const tv = tvs.find((t) => t.id === id);
    title.textContent = tv ? tv.name : "TV";
    for (const chip of picker.children) chip.classList.toggle("active", chip.dataset.id === id);
    const caps = new Set(tv?.capabilities || []);
    const available = { apps: caps.has("apps"), inputs: caps.has("inputs"), keyboard: caps.has("text_input") };
    for (const button of segments.querySelectorAll(".segment")) button.hidden = !available[button.dataset.panel];
    const first = Object.keys(available).find((name) => available[name]);
    segments.hidden = !first;
    showPanel(first || "none");
    appFilter.value = "";
    renderRecents();
    refreshStatus();
    if (caps.has("apps")) loadApps();
    if (caps.has("inputs")) loadInputs();
  }

  async function load() {
    try {
      const data = await Friday.api("/api/tvs");
      tvs = data.tvs || [];
      aliases = data.input_aliases || {};
    } catch (error) {
      Friday.toast(`Could not load TVs: ${error.message}`, "error");
      tvs = [];
    }
    picker.replaceChildren();
    picker.hidden = tvs.length < 2;
    for (const tv of tvs) {
      const chip = Friday.el("button", { class: "chip action", type: "button", dataset: { id: tv.id } }, tv.name);
      chip.addEventListener("click", () => select(tv.id));
      picker.append(chip);
    }
    remote.hidden = tvs.length === 0;
    emptyNote.hidden = tvs.length > 0;
    if (!tvs.length) {
      title.textContent = "TV";
      return;
    }
    select(tvs.some((t) => t.id === current) ? current : tvs[0].id);
  }

  function start() {
    load();
    clearInterval(statusTimer);
    statusTimer = setInterval(() => {
      if (document.visibilityState === "visible") refreshStatus();
    }, 8000);
  }
  function stop() {
    clearInterval(statusTimer);
    statusTimer = null;
  }

  Friday.on("view", (v) => (v === "tv" ? start() : stop()));
  Friday.on("tvs-changed", () => {
    if (Friday.view === "tv") load();
  });
  if (Friday.view === "tv") start();
  void view;
})();
