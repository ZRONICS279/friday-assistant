// Devices: every light and switch the house knows, as tiles. A tile shows
// the light's colour and state and has its own power switch; tapping it
// opens the full controls — a brightness dial, warmth and colour — in a
// panel beside the grid on a wide screen, or a sheet from the bottom on a
// phone. State refreshes every few seconds, but never underneath a finger.
(() => {
  const grid = document.querySelector("#device-grid");
  const empty = document.querySelector("#devices-empty");
  const notice = document.querySelector("#devices-notice");
  const chips = document.querySelector("#backend-chips");
  const pairPanel = document.querySelector("#pair-panel");
  const roomBar = document.querySelector("#device-rooms");
  const summary = document.querySelector("#devices-summary");
  const allOff = document.querySelector("#devices-all-off");
  const panel = document.querySelector("#device-detail");
  const sheetScrim = document.querySelector("#device-scrim");
  const sheetQuery = window.matchMedia("(max-width: 1099px)");
  const wideQuery = window.matchMedia("(min-width: 1280px)");

  const tiles = new Map(); // device id -> { el, power, device, touchedAt }
  let devices = [];
  let room = localStorage.getItem("friday-room") || "";
  let selected = null; // device id shown in the panel
  let detail = null; // { id, controls, touchedAt }
  let timer = null;
  let loading = false;

  const RAINBOW =
    "linear-gradient(90deg,#ff3b3b 0%,#ffb020 16%,#f5f52a 33%,#3ddc84 50%,#2ec5ff 66%,#5b6cff 83%,#ff3bd4 100%)";
  const WARMTH = "linear-gradient(90deg,#ff9a3c 0%,#ffd39a 45%,#fff5e6 70%,#cfe6ff 100%)";

  const ICONS = {
    light: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2V16h5v-.1c0-.8.4-1.5 1-2A6 6 0 0 0 12 3z"/></svg>',
    switch: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v8M6.3 6.6a8 8 0 1 0 11.4 0"/></svg>',
  };

  function hslToHex(h, s, l) {
    s /= 100;
    l /= 100;
    const k = (n) => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return "#" + [f(0), f(8), f(4)].map((x) => Math.round(x * 255).toString(16).padStart(2, "0")).join("");
  }

  function hexToHue(hex) {
    if (!hex || !/^#[0-9a-f]{6}$/i.test(hex)) return null;
    const r = parseInt(hex.slice(1, 3), 16) / 255;
    const g = parseInt(hex.slice(3, 5), 16) / 255;
    const b = parseInt(hex.slice(5, 7), 16) / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    if (max === min) return null;
    const d = max - min;
    let h;
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    return Math.round(((h * 60) + 360) % 360);
  }

  function kelvinToHex(kelvin) {
    // A rough tint for the glow, warm to cool.
    const t = Math.max(0, Math.min(1, (kelvin - 2000) / 4500));
    const warm = [255, 170, 90];
    const cool = [205, 228, 255];
    const mix = warm.map((w, i) => Math.round(w + (cool[i] - w) * t));
    return "#" + mix.map((c) => c.toString(16).padStart(2, "0")).join("");
  }

  function glowFor(device) {
    const state = device.state || {};
    if (!state.power || device.online === false) return null;
    if (state.color) return state.color;
    if (state.color_temp_kelvin) return kelvinToHex(state.color_temp_kelvin);
    return "#ffe2b0";
  }

  function stateText(device) {
    const state = device.state || {};
    if (device.online === false) return "Offline";
    if (!state.power) return "Off";
    return state.brightness != null ? `${state.brightness}%` : "On";
  }

  const isOn = (device) => Boolean(device.state?.power) && device.online !== false;
  const inRoom = (device) => !room || (device.room || "Unassigned") === room;

  // ---- talking to the lights ---------------------------------------------
  function merge(fresh) {
    const index = devices.findIndex((d) => d.id === fresh.id);
    if (index >= 0) devices[index] = fresh;
  }

  async function send(device, patch) {
    const tile = tiles.get(device.id);
    if (tile) tile.touchedAt = Date.now();
    if (detail?.id === device.id) detail.touchedAt = Date.now();
    try {
      const result = await Friday.api("/api/devices/set", { method: "POST", body: { device: device.id, ...patch } });
      if (result.ok === false) throw new Error(result.error || "Device refused");
      for (const fresh of result.device ? [result.device] : result.devices || []) merge(fresh);
      if (result.failed?.length) Friday.toast(`Some devices failed: ${result.failed.join(", ")}`, "error");
    } catch (error) {
      Friday.toast(`${device.name}: ${error.message}`, "error");
      tile?.el.classList.add("shake");
      setTimeout(() => tile?.el.classList.remove("shake"), 500);
    }
    paintAll({ keepControls: true });
  }

  // Optimistic: the tile flips at once, the light catches up.
  function setPower(device, on) {
    device.state = { ...(device.state || {}), power: on };
    paintAll({ keepControls: true });
    return send(device, { power: on ? "on" : "off" });
  }

  // ---- tiles -------------------------------------------------------------
  function buildTile(device) {
    const el = Friday.el("article", { class: "dev", tabindex: "0", role: "button", dataset: { id: device.id } });
    const icon = Friday.el("span", { class: "dev-icon", html: ICONS[device.type] || ICONS.light });
    const powerHost = Friday.el("div");
    const tile = { el, device, touchedAt: 0 };
    tile.power = Friday.toggle(powerHost, {
      checked: isOn(device),
      label: `${device.name} power`,
      onChange: (on) => setPower(tile.device, on),
    });
    el.append(
      Friday.el("div", { class: "dev-top" }, icon, powerHost),
      Friday.el(
        "div",
        { class: "dev-text" },
        Friday.el("h3", { class: "dev-name" }, device.name),
        Friday.el("span", { class: "dev-sub" }, Friday.el("span", { class: "dev-room" }), Friday.el("span", { class: "dev-state" })),
      ),
    );
    const openIt = (event) => {
      if (event.target.closest(".switch")) return;
      select(tile.device.id, { open: true });
    };
    el.addEventListener("click", openIt);
    el.addEventListener("keydown", (event) => {
      if (event.target !== el || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      openIt(event);
    });
    return tile;
  }

  function paintTile(tile, device, { keepControls = false } = {}) {
    tile.device = device;
    const glow = glowFor(device);
    tile.el.style.setProperty("--glow", glow || "transparent");
    tile.el.classList.toggle("on", Boolean(glow));
    tile.el.classList.toggle("offline", device.online === false);
    tile.el.classList.toggle("selected", device.id === selected);
    tile.el.querySelector(".dev-name").textContent = device.name;
    tile.el.querySelector(".dev-room").textContent = room ? "" : `${device.room || "No room"} · `;
    tile.el.querySelector(".dev-state").textContent = stateText(device);
    if (!keepControls || Date.now() - tile.touchedAt > 2500) tile.power.set(isOn(device));
  }

  function renderRooms() {
    const counts = new Map();
    for (const device of devices) {
      const name = device.room || "Unassigned";
      counts.set(name, (counts.get(name) || 0) + 1);
    }
    if (room && !counts.has(room)) room = "";
    const names = [...counts.keys()].sort((a, b) => (a === "Unassigned") - (b === "Unassigned") || a.localeCompare(b));
    roomBar.replaceChildren();
    roomBar.hidden = names.length < 2;
    const chip = (label, value, count) => {
      const button = Friday.el(
        "button",
        { class: `room-chip${room === value ? " active" : ""}`, type: "button", "aria-pressed": String(room === value) },
        label,
        Friday.el("span", { class: "count" }, String(count)),
      );
      button.addEventListener("click", () => {
        room = value;
        localStorage.setItem("friday-room", room);
        render();
      });
      return button;
    };
    roomBar.append(chip("All", "", devices.length));
    for (const name of names) roomBar.append(chip(name, name, counts.get(name)));
  }

  function paintAll(options = {}) {
    for (const device of devices) {
      const tile = tiles.get(device.id);
      if (tile) paintTile(tile, device, options);
    }
    const shown = devices.filter(inRoom);
    const lit = shown.filter(isOn).length;
    summary.textContent = devices.length
      ? `${lit} of ${shown.length} on${room ? ` in ${room}` : ""}`
      : "Lights and switches around the house";
    // One button: "All off" while anything shown is lit, "All on" once
    // everything is dark.
    const reachable = shown.filter((d) => d.online !== false).length;
    allOff.dataset.mode = lit ? "off" : "on";
    allOff.querySelector("span").textContent = lit ? "All off" : "All on";
    allOff.title = lit ? `Turn off ${room ? `the ${room}` : "every light"}` : `Turn on ${room ? `the ${room}` : "every light"}`;
    allOff.disabled = !lit && !reachable;
    paintDetail(options);
    Friday.emit("devices", devices);
  }

  function render(data) {
    if (data) {
      devices = [...(data.devices || [])].sort((a, b) => {
        const ra = a.room || "￿";
        const rb = b.room || "￿";
        return ra.localeCompare(rb) || String(a.name).localeCompare(String(b.name));
      });
    }
    const seen = new Set(devices.map((d) => d.id));
    for (const [id, tile] of tiles) {
      if (!seen.has(id)) {
        tile.el.remove();
        tiles.delete(id);
      }
    }
    renderRooms();
    const fragment = document.createDocumentFragment();
    for (const device of devices.filter(inRoom)) {
      let tile = tiles.get(device.id);
      if (!tile) {
        tile = buildTile(device);
        tiles.set(device.id, tile);
      }
      fragment.append(tile.el);
    }
    grid.replaceChildren(fragment);
    empty.hidden = devices.length > 0;
    document.querySelector("#devices-layout").classList.toggle("has-devices", devices.length > 0);

    if (selected && !seen.has(selected)) closeDetail();
    // Wide screens always show someone in the panel.
    if (!sheetQuery.matches && !selected && devices.length) select(devices.filter(inRoom)[0]?.id || devices[0].id);
    paintAll({ keepControls: true });

    if (!data) return;
    const lines = [];
    for (const [name, status] of Object.entries(data.backends || {})) {
      if (status.enabled && status.problem) {
        lines.push(`${status.problem}, so ${name} devices can't be controlled. Friday restarts it by itself within a couple of minutes.`);
      }
    }
    if (data.setup_pending?.length) {
      const n = data.setup_pending.length;
      lines.push(
        `${n} new light${n === 1 ? "" : "s"} joined but ${n === 1 ? "has" : "have"} no name yet. Tell Friday "set up the new light", or rename ${n === 1 ? "it" : "them"}.`,
      );
    }
    if (data.missing?.length) {
      lines.push(`Paired before but not answering now: ${data.missing.map((m) => m.name || m.key || m).join(", ")}.`);
    }
    notice.hidden = lines.length === 0;
    notice.textContent = lines.join(" ");

    chips.replaceChildren();
    for (const [name, status] of Object.entries(data.backends || {})) {
      if (!status.enabled) continue;
      chips.append(
        Friday.el(
          "span",
          { class: `chip ${status.connected ? "ok" : "bad"}`, title: `${name}: ${status.connected ? "connected" : "not connected"}` },
          Friday.el("span", { class: "chip-dot" }),
          `${name} · ${status.devices}`,
        ),
      );
    }
  }

  // ---- the detail panel ----------------------------------------------------
  function select(id, { open = false } = {}) {
    if (!id) return;
    if (selected !== id || !detail) {
      selected = id;
      buildDetail(devices.find((d) => d.id === id));
    }
    if (open && sheetQuery.matches) {
      panel.hidden = false;
      sheetScrim.hidden = false;
      document.body.classList.add("sheet-open");
    }
    paintAll({ keepControls: true });
  }

  function closeDetail() {
    if (sheetQuery.matches) {
      panel.hidden = true;
      sheetScrim.hidden = true;
      document.body.classList.remove("sheet-open");
      selected = null;
      detail = null;
    }
  }

  function buildDetail(device) {
    if (!device) return;
    const caps = new Set(device.capabilities || []);
    const controls = {};
    detail = { id: device.id, controls, touchedAt: 0 };
    const current = () => devices.find((d) => d.id === device.id) || device;
    const touch = () => {
      detail.touchedAt = Date.now();
    };
    const throttled = Friday.throttle((patch) => send(current(), patch), 220);

    const powerHost = Friday.el("div");
    controls.power = Friday.toggle(powerHost, {
      checked: isOn(device),
      label: `${device.name} power`,
      onChange: (on) => setPower(current(), on),
    });
    const close = Friday.el("button", { class: "icon-btn detail-close", type: "button", "aria-label": "Close", html: "&#10005;" });
    close.addEventListener("click", closeDetail);
    const head = Friday.el(
      "header",
      { class: "detail-head" },
      Friday.el("span", { class: "dev-icon", html: ICONS[device.type] || ICONS.light }),
      Friday.el(
        "div",
        { class: "detail-title" },
        Friday.el("h2", { class: "detail-name" }),
        Friday.el("span", { class: "detail-meta" }),
      ),
      powerHost,
      close,
    );

    const body = Friday.el("div", { class: "detail-body" });
    if (caps.has("brightness")) {
      const host = Friday.el("div");
      controls.brightness = Friday.dial(host, {
        min: 1,
        max: 100,
        value: device.state?.brightness ?? 100,
        format: (v) => `${v}%`,
        caption: "Brightness",
        label: `${device.name} brightness`,
        onInput: (v) => {
          touch();
          throttled({ brightness: v });
        },
        onChange: (v) => send(current(), { brightness: v }),
      });
      body.append(host);
    } else {
      const big = Friday.el("button", { class: "big-power", type: "button", html: ICONS.switch });
      big.addEventListener("click", () => setPower(current(), !isOn(current())));
      controls.big = big;
      body.append(big);
    }
    if (caps.has("color_temp")) {
      const host = Friday.el("div");
      controls.warmth = Friday.slider(host, {
        min: 2200,
        max: 6500,
        step: 50,
        value: device.state?.color_temp_kelvin ?? 3000,
        track: WARMTH,
        format: (v) => `${v}K`,
        label: `${device.name} warmth`,
        onInput: (v) => {
          touch();
          throttled({ color_temp: v });
        },
        onChange: (v) => send(current(), { color_temp: v }),
      });
      body.append(Friday.el("div", { class: "control" }, Friday.el("span", { class: "control-label" }, "Warmth"), host));
    }
    if (caps.has("color")) {
      const host = Friday.el("div");
      controls.hue = Friday.slider(host, {
        min: 0,
        max: 359,
        value: hexToHue(device.state?.color) ?? 30,
        track: RAINBOW,
        label: `${device.name} colour`,
        onInput: (v) => {
          touch();
          throttled({ color: hslToHex(v, 100, 50) });
        },
        onChange: (v) => send(current(), { color: hslToHex(v, 100, 50) }),
      });
      host.classList.add("rainbow");
      body.append(Friday.el("div", { class: "control" }, Friday.el("span", { class: "control-label" }, "Colour"), host));
    }

    // Rename, move, identify, forget.
    const edit = Friday.el("button", { class: "ghost", type: "button" }, "Rename");
    const flash = Friday.el("button", { class: "ghost", type: "button" }, "Blink");
    const forget = Friday.el("button", { class: "ghost danger", type: "button" }, "Remove");
    const form = Friday.el(
      "form",
      { class: "device-edit", hidden: true },
      Friday.el("input", { type: "text", name: "name", maxlength: "60", placeholder: "Name", value: device.name }),
      Friday.el("input", { type: "text", name: "room", maxlength: "40", placeholder: "Room", value: device.room || "" }),
      Friday.el("button", { class: "primary", type: "submit" }, "Save"),
    );
    edit.addEventListener("click", () => {
      form.hidden = !form.hidden;
      if (!form.hidden) form.querySelector("[name=name]").focus();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const name = form.querySelector("[name=name]").value.trim();
      const newRoom = form.querySelector("[name=room]").value.trim();
      const target = current();
      const payload = { device: target.id, replace: true };
      if (name && name !== target.name) payload.name = name;
      if (newRoom && newRoom !== target.room) payload.room = newRoom;
      if (!payload.name && !payload.room) {
        form.hidden = true;
        return;
      }
      try {
        const result = await Friday.api("/api/devices/alias", { method: "PUT", body: payload });
        if (result.ok === false) throw new Error(result.error || "Could not rename");
        form.hidden = true;
        Friday.toast("Saved.");
        load(true);
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    });
    flash.addEventListener("click", async () => {
      try {
        const result = await Friday.api("/api/devices/flash", { method: "POST", body: { device: device.id, times: 3 } });
        if (result.ok === false) throw new Error(result.error || "Could not blink");
        Friday.toast(`${current().name} is blinking.`);
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    });
    Friday.armed(forget, async () => {
      try {
        const result = await Friday.api("/api/devices/remove", { method: "POST", body: { device: device.id } });
        if (result.ok === false) throw new Error(result.error || "Could not remove");
        Friday.toast(`${current().name} removed.`);
        closeDetail();
        selected = null;
        load(true);
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    }, { label: "Remove?" });

    panel.replaceChildren(
      Friday.el("span", { class: "sheet-grip", "aria-hidden": "true" }),
      head,
      body,
      Friday.el("footer", { class: "detail-foot" }, edit, flash, forget),
      form,
    );
  }

  function paintDetail({ keepControls = false } = {}) {
    if (!detail) return;
    const device = devices.find((d) => d.id === detail.id);
    if (!device) return;
    const state = device.state || {};
    const glow = glowFor(device);
    panel.style.setProperty("--glow", glow || "transparent");
    panel.classList.toggle("on", Boolean(glow));
    panel.querySelector(".detail-name").textContent = device.name;
    panel.querySelector(".detail-meta").textContent =
      `${device.room || "No room"} · ${device.backend}${device.online === false ? " · offline" : ""}`;
    const { controls } = detail;
    const fresh = !keepControls || Date.now() - detail.touchedAt > 2500;
    if (fresh) controls.power.set(isOn(device));
    controls.big?.classList.toggle("on", isOn(device));
    if (!fresh) return;
    if (controls.brightness && state.brightness != null) controls.brightness.set(state.brightness);
    if (controls.warmth && state.color_temp_kelvin != null) controls.warmth.set(state.color_temp_kelvin);
    if (controls.hue) {
      const hue = hexToHue(state.color);
      if (hue != null) controls.hue.set(hue);
    }
  }

  allOff.addEventListener("click", async () => {
    const on = allOff.dataset.mode === "on";
    const targets = devices.filter((d) => inRoom(d) && d.online !== false && isOn(d) !== on);
    allOff.disabled = true;
    await Promise.all(targets.map((d) => setPower(d, on)));
  });
  sheetScrim.addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden && sheetQuery.matches) closeDetail();
  });
  sheetQuery.addEventListener("change", () => {
    // Crossing into the phone layout: the panel becomes a sheet, closed.
    if (sheetQuery.matches) {
      panel.hidden = true;
      sheetScrim.hidden = true;
      document.body.classList.remove("sheet-open");
    } else {
      panel.hidden = false;
      sheetScrim.hidden = true;
      document.body.classList.remove("sheet-open");
      if (!selected && devices.length) select(devices[0].id);
    }
  });
  if (!sheetQuery.matches) panel.hidden = false;

  // Other pages (the quick controls beside the chat) switch lights too.
  Friday.devicePower = (id, on) => {
    const device = devices.find((d) => d.id === id);
    if (device) setPower(device, on);
  };

  async function load(force = false) {
    if (loading && !force) return;
    loading = true;
    try {
      const busy = detail && Object.values(detail.controls).some((c) => c.dragging);
      const data = await Friday.api("/api/devices");
      if (!busy) render(data);
    } catch (error) {
      if (force) Friday.toast(`Could not load devices: ${error.message}`, "error");
    } finally {
      loading = false;
    }
  }

  function start() {
    if (timer) return;
    load(true);
    timer = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 4000);
  }

  function stop() {
    clearInterval(timer);
    timer = null;
  }

  // Poll while the grid is on screen, or something else is showing live
  // switches: the chat's quick controls on a wide screen, or the home screen
  // that stands in for an empty chat at any width.
  const onDash = () => document.body.dataset.chat === "idle";
  const wanted = () =>
    Friday.view === "devices" || (Friday.view === "chat" && (wideQuery.matches || onDash()));
  const sync = () => (wanted() ? start() : stop());
  wideQuery.addEventListener("change", sync);
  Friday.on("chat-idle", sync);

  // ---- pairing -------------------------------------------------------------
  function showPair() {
    pairPanel.hidden = false;
    pairPanel.innerHTML = `
      <div class="pair-head"><h2>Add a device</h2><button class="icon-btn" type="button" data-close aria-label="Close">&#10005;</button></div>
      <div class="pair-options">
        <form class="pair-option" data-network="matter">
          <h3>Matter</h3>
          <p>Type the pairing code from the box or QR label. Wi‑Fi and Bluetooth devices both work.</p>
          <div class="inline-form">
            <input type="text" name="code" maxlength="120" placeholder="MT:… or 1234-567-8901" autocomplete="off" />
            <button class="primary" type="submit">Pair</button>
          </div>
        </form>
        <form class="pair-option" data-network="zigbee">
          <h3>Zigbee</h3>
          <p>Open the join window, then power the light on (or reset it: off and on six times).</p>
          <div class="inline-form"><button class="primary" type="submit">Open join window</button></div>
        </form>
      </div>
      <p class="pair-result" hidden></p>`;
    pairPanel.querySelector("[data-close]").addEventListener("click", () => (pairPanel.hidden = true));
    for (const form of pairPanel.querySelectorAll("form")) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const result = pairPanel.querySelector(".pair-result");
        const button = form.querySelector("button[type=submit]");
        const body = { network: form.dataset.network };
        const code = form.querySelector("[name=code]")?.value.trim();
        if (body.network === "matter" && !code) {
          Friday.toast("Enter the pairing code first.", "error");
          return;
        }
        if (code) body.code = code;
        button.disabled = true;
        result.hidden = false;
        result.textContent = body.network === "matter" ? "Pairing… this can take a minute." : "Opening the join window…";
        try {
          const data = await Friday.api("/api/devices/pair", { method: "POST", body });
          result.textContent = data.note || data.message || data.error || (data.ok === false ? "Pairing failed." : "Done.");
          if (data.device) result.textContent = `Paired ${data.device.name}. ${data.note || ""}`;
          load(true);
        } catch (error) {
          result.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });
    }
  }

  document.querySelector("#devices-pair").addEventListener("click", showPair);
  document.querySelector("#devices-refresh").addEventListener("click", () => load(true));
  Friday.on("view", (view) => {
    if (view !== "devices") closeDetail();
    sync();
  });
  sync();
})();
