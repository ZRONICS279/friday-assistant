// At a glance: the column beside the chat on a wide screen. The date and
// time, what's next on the calendar, and every light with its own switch,
// so the everyday things need neither a sentence nor a page change.
(() => {
  const dateEl = document.querySelector("#glance-date");
  const timeEl = document.querySelector("#glance-time");
  const next = document.querySelector("#glance-next");
  const nextTitle = document.querySelector("#glance-next-title");
  const nextWhen = document.querySelector("#glance-next-when");
  const list = document.querySelector("#glance-lights");
  const count = document.querySelector("#glance-count");
  const empty = document.querySelector("#glance-empty");
  const wide = window.matchMedia("(min-width: 1280px)");
  const wx = document.querySelector("#wx");
  const wxIcon = document.querySelector("#wx-icon");
  const wxTemp = document.querySelector("#wx-temp");
  const wxLine = document.querySelector("#wx-line");
  let weatherAt = 0;

  async function loadWeather() {
    if (Date.now() - weatherAt < 10 * 60 * 1000) return;
    weatherAt = Date.now();
    try {
      const data = await Friday.api("/api/weather");
      // No location yet, or the lookup failed: the card is just the clock.
      if (!data.known || data.error || data.temperature == null) {
        wx.hidden = true;
        wxLine.hidden = true;
        if (data.error) weatherAt = Date.now() - 8 * 60 * 1000; // retry in two minutes
        return;
      }
      const deg = "°";
      wxIcon.innerHTML = Friday.skyIcon(data.code, data.daylight);
      wxTemp.textContent = `${data.temperature}${deg}`;
      const today = data.today || {};
      const range = today.high != null && today.low != null ? `H ${today.high}${deg} · L ${today.low}${deg}` : "";
      const condition = data.condition ? data.condition[0].toUpperCase() + data.condition.slice(1) : "";
      wxLine.textContent = [condition, range, data.place].filter(Boolean).join(" · ");
      wx.title = data.alerts?.length ? data.alerts.map((a) => a.event || a.headline || a).join("\n") : "";
      wx.classList.toggle("alert", Boolean(data.alerts?.length));
      wx.hidden = false;
      wxLine.hidden = false;
    } catch {
      weatherAt = Date.now() - 8 * 60 * 1000;
    }
  }
  const rows = new Map(); // device id -> { li, power, sub }
  let calendarAt = 0;

  function tick() {
    const now = new Date();
    dateEl.textContent = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
    timeEl.textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function when(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const today = new Date();
    const tomorrow = new Date(today);
    tomorrow.setDate(today.getDate() + 1);
    const day =
      date.toDateString() === today.toDateString()
        ? "Today"
        : date.toDateString() === tomorrow.toDateString()
          ? "Tomorrow"
          : date.toLocaleDateString([], { weekday: "long" });
    return `${day} · ${date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }

  async function loadCalendar() {
    if (Date.now() - calendarAt < 5 * 60 * 1000) return;
    calendarAt = Date.now();
    try {
      const data = await Friday.api("/api/calendar?days=7");
      const event = (data.events || []).find((e) => !e.past);
      next.hidden = !event;
      if (event) {
        nextTitle.textContent = event.title;
        nextWhen.textContent = when(event.starts_at);
      }
    } catch {
      next.hidden = true;
    }
  }

  function paint(devices) {
    const lights = devices.filter((d) => d.type !== "unknown");
    const seen = new Set();
    for (const device of lights) {
      let row = rows.get(device.id);
      if (!row) {
        const powerHost = Friday.el("div");
        const sub = Friday.el("span", { class: "glance-sub" });
        const li = Friday.el(
          "li",
          { class: "glance-light" },
          Friday.el("span", { class: "glance-dot" }),
          Friday.el("span", { class: "glance-text" }, Friday.el("span", { class: "glance-name" }), sub),
          powerHost,
        );
        const power = Friday.toggle(powerHost, {
          label: `${device.name} power`,
          onChange: (on) => Friday.devicePower?.(device.id, on),
        });
        row = { li, power, sub };
        rows.set(device.id, row);
      }
      const on = Boolean(device.state?.power) && device.online !== false;
      row.li.classList.toggle("on", on);
      row.li.style.setProperty("--glow", device.state?.color || "#ffe2b0");
      row.li.querySelector(".glance-name").textContent = device.name;
      row.sub.textContent = device.online === false
        ? "Offline"
        : `${device.room || "No room"} · ${on ? (device.state?.brightness != null ? `${device.state.brightness}%` : "On") : "Off"}`;
      row.power.set(on);
      list.append(row.li);
      seen.add(device.id);
    }
    for (const [id, row] of rows) {
      if (!seen.has(id)) {
        row.li.remove();
        rows.delete(id);
      }
    }
    const lit = lights.filter((d) => d.state?.power && d.online !== false).length;
    count.textContent = lights.length ? `${lit} on` : "";
    empty.hidden = lights.length > 0;
  }

  function refresh() {
    if (Friday.view !== "chat" || !wide.matches) return;
    tick();
    loadCalendar();
    loadWeather();
  }

  Friday.on("devices", paint);
  Friday.on("view", refresh);
  wide.addEventListener("change", refresh);
  setInterval(refresh, 20 * 1000);
  tick();
  refresh();
})();
