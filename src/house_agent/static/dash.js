// The phone's home screen: a greeting and the weather, then the four things
// worth a glance before anything is typed — what is coming up, what the
// house does on its own, the lights with their own switches, and the day's
// headlines. It stands in for the transcript while the chat is empty, so
// nobody lands on a blank conversation.
(() => {
  const dash = document.querySelector("#dash");
  if (!dash) return;

  const wide = window.matchMedia("(min-width: 861px)");
  const pick = (id) => document.querySelector(`#${id}`);
  const greeting = pick("dash-greeting");
  const sub = pick("dash-sub");
  const clock = pick("dash-clock");
  const wx = pick("dash-wx");
  const wxIcon = pick("dash-wx-icon");
  const wxTemp = pick("dash-wx-temp");
  const wxLine = pick("dash-wx-line");
  const calendarRows = pick("dash-calendar-rows");
  const calendarEmpty = pick("dash-calendar-empty");
  const automationRows = pick("dash-automation-rows");
  const automationsEmpty = pick("dash-automations-empty");
  const deviceRows = pick("dash-device-rows");
  const devicesEmpty = pick("dash-devices-empty");
  const newsRows = pick("dash-news-rows");
  const newsEmpty = pick("dash-news-empty");
  const newsSub = pick("dash-news-sub");

  // The dashboard is on screen with the chat tab open and nothing said yet.
  // Everything it loads checks this first, so no other page spends a request
  // on it.
  const showing = () => Friday.view === "chat" && document.body.dataset.chat === "idle";

  // ---- greeting and weather --------------------------------------------------
  const ROUNDS = { weather: 0, calendar: 0, automations: 0, news: 0 };

  function due(key, minutes) {
    if (Date.now() - ROUNDS[key] < minutes * 60 * 1000) return false;
    ROUNDS[key] = Date.now();
    return true;
  }

  // A failed round is worth retrying sooner than a good one.
  const retry = (key, minutes) => {
    ROUNDS[key] = Date.now() - (minutes - 2) * 60 * 1000;
  };

  function tick() {
    const now = new Date();
    const hour = now.getHours();
    const part = hour < 5 ? "Good evening" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
    // Friday knows who it is talking to once a voice has been recognised.
    const who = document.querySelector("#chat-user")?.hidden
      ? ""
      : document.querySelector("#chat-user-name")?.textContent?.trim() || "";
    greeting.textContent = who ? `${part}, ${who}` : part;
    clock.textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  // The line under the greeting is the house in one sentence. It only says
  // anything once the devices have been polled at least once.
  function summarise(devices) {
    const switchable = devices.filter((device) => device.type !== "unknown");
    if (!switchable.length) {
      sub.textContent = "Ask Friday anything, or tap the mic.";
      return;
    }
    const lit = switchable.filter((device) => device.state?.power && device.online !== false).length;
    sub.textContent = lit
      ? `${lit} of ${switchable.length} light${switchable.length === 1 ? "" : "s"} on`
      : "Everything is off";
  }

  async function loadWeather() {
    if (!due("weather", 10)) return;
    try {
      const data = await Friday.api("/api/weather");
      if (!data.known || data.error || data.temperature == null) {
        wx.classList.remove("has-weather");
        wxLine.textContent = data.known ? "" : "Tell Friday where you live";
        if (data.error) retry("weather", 10);
        return;
      }
      wxIcon.innerHTML = Friday.skyIcon(data.code, data.daylight);
      wxTemp.textContent = `${data.temperature}°`;
      const condition = data.condition ? data.condition[0].toUpperCase() + data.condition.slice(1) : "";
      // "Springfield, Illinois, US" is a whole line on a phone; the town is
      // the part anyone reads.
      const place = String(data.place || "").split(",")[0].trim();
      wxLine.textContent = [condition, place].filter(Boolean).join(" · ");
      wx.classList.add("has-weather");
      wx.classList.toggle("alert", Boolean(data.alerts?.length));
    } catch {
      retry("weather", 10);
    }
  }

  // ---- what is coming up -----------------------------------------------------
  function dayOf(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const today = new Date();
    const tomorrow = new Date(today);
    tomorrow.setDate(today.getDate() + 1);
    if (date.toDateString() === today.toDateString()) return "Today";
    if (date.toDateString() === tomorrow.toDateString()) return "Tomorrow";
    return date.toLocaleDateString([], { weekday: "short" });
  }

  function clockOf(event) {
    if (/all day/i.test(event.when || "")) return "all day";
    const date = new Date(event.starts_at);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  // How many rows each tile shows before it scrolls. Rows are not all the
  // same height — a device name can wrap to two lines — so the cap is
  // measured from the real rows rather than guessed in CSS, and a sliver of
  // the next one is left showing so there is something to grab at.
  const VISIBLE = {
    "dash-calendar-rows": [2, 4],
    "dash-automation-rows": [2, 4],
    "dash-device-rows": [4, 6],
  };

  function cap(list) {
    const visible = VISIBLE[list.id][wide.matches ? 1 : 0];
    const rows = [...list.children];
    if (rows.length <= visible) {
      list.style.maxHeight = "";
      return;
    }
    const gap = parseFloat(getComputedStyle(list).rowGap) || 0;
    const height = (row) => row.getBoundingClientRect().height;
    const shown = rows.slice(0, visible).reduce((total, row) => total + height(row), 0);
    // Off screen everything measures zero; leave the cap for when it is up.
    if (!shown) return;
    list.style.maxHeight = `${shown + gap * visible + Math.min(16, height(rows[visible]) / 3)}px`;
  }

  const capAll = () => Object.keys(VISIBLE).forEach((id) => cap(pick(id)));

  function row(title, meta) {
    return Friday.el(
      "li",
      { class: "dash-row" },
      Friday.el("span", { class: "dash-row-title" }, title),
      Friday.el("span", { class: "dash-row-meta" }, meta),
    );
  }

  async function loadCalendar() {
    if (!due("calendar", 5)) return;
    try {
      const data = await Friday.api("/api/calendar?days=7");
      const events = (data.events || []).filter((event) => !event.past).slice(0, KEPT.plans);
      calendarRows.replaceChildren(
        ...events.map((event) => row(event.title, [dayOf(event.starts_at), clockOf(event)].filter(Boolean).join(" · "))),
      );
      calendarEmpty.hidden = events.length > 0;
      cap(calendarRows);
    } catch {
      retry("calendar", 5);
    }
  }

  // ---- what the house does on its own ----------------------------------------
  function nextRun(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return `${dayOf(iso)} · ${date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }

  async function loadAutomations() {
    if (!due("automations", 5)) return;
    try {
      const data = await Friday.api("/api/automations");
      const active = (data.automations || []).filter((item) => item.enabled);
      automationRows.replaceChildren(
        ...active
          .slice(0, KEPT.automations)
          .map((item) => row(item.name || item.description || "Automation", item.next_run_at ? nextRun(item.next_run_at) : "")),
      );
      automationsEmpty.hidden = active.length > 0;
      cap(automationRows);
    } catch {
      retry("automations", 5);
    }
  }

  // ---- lights and switches ---------------------------------------------------
  // The switches are live: each row keeps its own toggle, and a poll that
  // lands mid-tap must not yank it back, so a row just touched is left alone.
  const switches = new Map(); // device id -> { li, power, state, touchedAt }
  // Each tile shows a couple of rows and scrolls for the rest, so a busy
  // house is all reachable without the home screen turning into a list.
  const KEPT = { plans: 8, automations: 8, devices: 12 };

  function paintDevices(devices) {
    // Only what can actually be switched: a row with a dead toggle is worse
    // than no row.
    const shown = devices.filter((device) => device.type !== "unknown").slice(0, KEPT.devices);
    const seen = new Set();
    for (const device of shown) {
      let entry = switches.get(device.id);
      if (!entry) {
        const host = Friday.el("div");
        const state = Friday.el("span", { class: "dash-device-state" });
        const li = Friday.el(
          "li",
          { class: "dash-device" },
          Friday.el("span", { class: "dash-device-dot" }),
          Friday.el(
            "span",
            { class: "dash-device-text" },
            Friday.el("span", { class: "dash-device-name" }),
            state,
          ),
          host,
        );
        const power = Friday.toggle(host, {
          label: `${device.name} power`,
          onChange: (on) => {
            entry.touchedAt = Date.now();
            Friday.devicePower?.(device.id, on);
          },
        });
        entry = { li, power, state, touchedAt: 0 };
        switches.set(device.id, entry);
      }
      const on = Boolean(device.state?.power) && device.online !== false;
      entry.li.classList.toggle("on", on);
      entry.li.style.setProperty("--glow", device.state?.color || "#ffe2b0");
      entry.li.querySelector(".dash-device-name").textContent = device.name;
      entry.state.textContent = device.online === false
        ? "Offline"
        : on
          ? device.state?.brightness != null ? `On · ${device.state.brightness}%` : "On"
          : "Off";
      if (Date.now() - entry.touchedAt > 2500) entry.power.set(on);
      deviceRows.append(entry.li);
      seen.add(device.id);
    }
    for (const [id, entry] of switches) {
      if (!seen.has(id)) {
        entry.li.remove();
        switches.delete(id);
      }
    }
    devicesEmpty.hidden = shown.length > 0;
    cap(deviceRows);
    summarise(devices);
  }

  // ---- headlines -------------------------------------------------------------
  function story(item) {
    const when = item.published_at ? Friday.timeAgo(item.published_at) : "";
    const link = Friday.el(
      "a",
      { class: "dash-story", href: item.url || "#", target: "_blank", rel: "noopener noreferrer" },
      Friday.el("span", { class: "dash-story-title" }, item.title),
      Friday.el("span", { class: "dash-story-meta" }, [item.source, when].filter(Boolean).join(" · ")),
    );
    if (!item.url) link.removeAttribute("href");
    return Friday.el("li", { class: `dash-news-item ${item.kind === "traffic" ? "traffic" : ""}` }, link);
  }

  async function loadNews() {
    if (!due("news", 10)) return;
    try {
      const data = await Friday.api("/api/news");
      if (data.enabled === false) {
        document.querySelector("#dash-news").hidden = true;
        return;
      }
      // Traffic first: it is the part of a briefing that expires.
      const stories = [...(data.stories || [])].sort(
        (a, b) => (b.kind === "traffic") - (a.kind === "traffic"),
      );
      const top = stories.slice(0, 4);
      newsRows.replaceChildren(...top.map(story));
      newsEmpty.hidden = top.length > 0;
      if (data.error && !top.length) newsEmpty.textContent = data.error;
      newsSub.textContent = top.some((item) => item.kind === "traffic") ? "news & traffic" : "";
      if (!top.length) retry("news", 10);
    } catch {
      retry("news", 10);
    }
  }

  // ---- when to do any of this ------------------------------------------------
  function refresh() {
    if (!showing()) return;
    tick();
    loadWeather();
    loadCalendar();
    loadAutomations();
    loadNews();
    // Rows measure zero while the home screen is off screen, so whatever was
    // rendered then is capped the first time it is actually up.
    capAll();
  }

  // Plans and automations are also edited in the Planner, and by Friday
  // itself mid-chat. Coming back to the home screen, or hearing that
  // something changed, re-reads them instead of waiting out the cache.
  function reread() {
    ROUNDS.calendar = 0;
    ROUNDS.automations = 0;
    refresh();
  }

  Friday.on("devices", paintDevices);
  Friday.on("view", reread);
  Friday.on("chat-idle", reread);
  Friday.on("planner", reread);
  // Who Friday is talking to changes the greeting.
  Friday.on("conversation", tick);
  // A resize across the breakpoint changes how many rows fit.
  wide.addEventListener("change", () => {
    capAll();
    refresh();
  });
  setInterval(refresh, 30 * 1000);
  tick();
  refresh();
})();
