// Settings: the televisions and their addresses, DoorDash, the Wi‑Fi handed
// to new devices, and what the system is running on.
(() => {
  const tvList = document.querySelector("#tv-list");
  const tvEmpty = document.querySelector("#tv-list-empty");
  const tvAddForm = document.querySelector("#tv-add-form");
  const tvFound = document.querySelector("#tv-found");
  const ddStatus = document.querySelector("#dd-status");
  const ddLoginPanel = document.querySelector("#dd-login-panel");
  const ddCallbackForm = document.querySelector("#dd-callback-form");
  const wifiStatus = document.querySelector("#wifi-status");
  const wifiForm = document.querySelector("#wifi-form");
  const systemStatus = document.querySelector("#system-status");
  const voicesList = document.querySelector("#voices-list");
  const voicesEmpty = document.querySelector("#voices-empty");

  const PLATFORM_NAMES = {
    roku: "Roku", webos: "LG webOS", samsung: "Samsung", vizio: "Vizio",
    androidtv: "Android TV", firetv: "Fire TV", appletv: "Apple TV",
  };

  function kv(host, pairs) {
    host.replaceChildren();
    for (const [key, value, cls] of pairs) {
      if (value === undefined || value === null || value === "") continue;
      host.append(
        Friday.el("div", { class: "kv-row" }, Friday.el("span", { class: "kv-key" }, key), Friday.el("span", { class: `kv-value ${cls || ""}` }, value)),
      );
    }
  }

  // ---- televisions -------------------------------------------------------------
  async function loadTVs() {
    try {
      const data = await Friday.api("/api/tvs");
      tvList.replaceChildren();
      for (const tv of data.tvs || []) tvList.append(renderTV(tv));
      tvEmpty.hidden = (data.tvs || []).length > 0;
    } catch (error) {
      Friday.toast(`TVs: ${error.message}`, "error");
    }
  }

  function renderTV(tv) {
    const li = Friday.el("li", { class: "row tv-row" });
    const text = Friday.el(
      "div",
      { class: "row-text" },
      Friday.el("span", { class: "row-title" }, Friday.el("span", { class: `dot ${tv.reachable ? "ok" : "bad"}` }), tv.name),
      Friday.el("span", { class: "row-sub" }, `${PLATFORM_NAMES[tv.platform] || tv.platform} · ${tv.host}:${tv.port} · ${tv.reachable ? "answering" : "not answering"}${tv.from_env ? " · from .env" : ""}`),
    );
    const actions = Friday.el("div", { class: "row-actions" });
    const edit = Friday.el("button", { class: "ghost tiny", type: "button" }, "Edit");
    const remove = Friday.el("button", { class: "ghost tiny danger", type: "button" }, "Remove");
    const remoteBtn = Friday.el("button", { class: "ghost tiny", type: "button" }, "Remote");
    remoteBtn.addEventListener("click", () => {
      localStorage.setItem("friday-tv", tv.id);
      Friday.navigate("tv");
    });
    const form = Friday.el(
      "form",
      { class: "edit-form", hidden: true },
      Friday.el("label", {}, "Name", Friday.el("input", { type: "text", name: "name", maxlength: "80", value: tv.name, required: true })),
      Friday.el("label", {}, "Address", Friday.el("input", { type: "text", name: "host", maxlength: "253", value: tv.host, required: true })),
      Friday.el("label", {}, "Port", Friday.el("input", { type: "number", name: "port", min: "1", max: "65535", value: String(tv.port) })),
      Friday.el(
        "div",
        { class: "edit-actions" },
        Friday.el("button", { class: "ghost", type: "button", "data-cancel": true }, "Cancel"),
        Friday.el("button", { class: "primary", type: "submit" }, "Save"),
      ),
    );
    edit.addEventListener("click", () => (form.hidden = !form.hidden));
    form.querySelector("[data-cancel]").addEventListener("click", () => (form.hidden = true));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = {};
      const name = form.name.value.trim();
      const host = form.host.value.trim();
      const port = Number(form.port.value) || null;
      if (name && name !== tv.name) body.name = name;
      if ((host && host !== tv.host) || (port && port !== tv.port)) {
        body.host = host || tv.host;
        if (port) body.port = port;
      }
      if (!Object.keys(body).length) {
        form.hidden = true;
        return;
      }
      const button = form.querySelector("[type=submit]");
      button.disabled = true;
      try {
        await Friday.api(`/api/tvs/${encodeURIComponent(tv.id)}`, { method: "PUT", body });
        Friday.toast("Saved. It takes effect now and survives restarts.");
        Friday.emit("tvs-changed");
        loadTVs();
      } catch (error) {
        Friday.toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
    Friday.armed(remove, async () => {
      try {
        await Friday.api(`/api/tvs/${encodeURIComponent(tv.id)}`, { method: "DELETE" });
        Friday.toast(`${tv.name} removed.`);
        Friday.emit("tvs-changed");
        loadTVs();
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    }, { label: "Remove?" });
    actions.append(remoteBtn, edit, remove);
    li.append(text, actions, form);
    return li;
  }

  function slug(text) {
    return String(text).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "tv";
  }

  document.querySelector("#tv-add").addEventListener("click", () => {
    tvAddForm.hidden = !tvAddForm.hidden;
    if (!tvAddForm.hidden) tvAddForm.name.focus();
  });
  tvAddForm.querySelector("[data-cancel]").addEventListener("click", () => (tvAddForm.hidden = true));
  tvAddForm.name.addEventListener("input", () => {
    if (!tvAddForm.id.dataset.touched) tvAddForm.id.value = slug(tvAddForm.name.value);
  });
  tvAddForm.id.addEventListener("input", () => (tvAddForm.id.dataset.touched = "1"));
  tvAddForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      id: tvAddForm.id.value.trim() || slug(tvAddForm.name.value),
      name: tvAddForm.name.value.trim(),
      host: tvAddForm.host.value.trim(),
      platform: tvAddForm.platform.value,
    };
    const port = Number(tvAddForm.port.value);
    if (port) body.port = port;
    const button = tvAddForm.querySelector("[type=submit]");
    button.disabled = true;
    try {
      const data = await Friday.api("/api/tvs", { method: "POST", body });
      Friday.toast(`${data.device.name} added${data.device.reachable ? " and answering." : ". It is not answering yet."}`);
      tvAddForm.reset();
      delete tvAddForm.id.dataset.touched;
      tvAddForm.hidden = true;
      Friday.emit("tvs-changed");
      loadTVs();
    } catch (error) {
      Friday.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  document.querySelector("#tv-discover").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    tvFound.hidden = false;
    tvFound.replaceChildren(Friday.el("span", { class: "muted" }, "Scanning the network…"));
    try {
      const data = await Friday.api("/api/tvs/discover", { method: "POST" });
      tvFound.replaceChildren();
      const found = data.found || [];
      if (!found.length) tvFound.append(Friday.el("span", { class: "muted" }, "No TVs answered. They must be on and on this network."));
      for (const item of found) {
        const row = Friday.el(
          "div",
          { class: "found-row" },
          Friday.el("span", {}, `${item.name || "TV"} · ${PLATFORM_NAMES[item.platform] || item.platform || "?"} · ${item.host}`),
        );
        if (item.already_configured) {
          row.append(Friday.el("span", { class: "chip ok" }, "saved"));
        } else {
          const add = Friday.el("button", { class: "primary tiny", type: "button" }, "Add");
          add.addEventListener("click", () => {
            tvAddForm.hidden = false;
            tvAddForm.name.value = item.name || "";
            tvAddForm.host.value = item.host || "";
            tvAddForm.port.value = item.port || "";
            if (item.platform && [...tvAddForm.platform.options].some((o) => o.value === item.platform)) tvAddForm.platform.value = item.platform;
            tvAddForm.id.value = slug(item.name || item.host);
            tvAddForm.name.focus();
          });
          row.append(add);
        }
        tvFound.append(row);
      }
    } catch (error) {
      tvFound.replaceChildren(Friday.el("span", { class: "muted" }, error.message));
    } finally {
      button.disabled = false;
    }
  });

  // ---- DoorDash -------------------------------------------------------------
  function paintDoorDash(status) {
    const signed = status.signed_in;
    kv(ddStatus, [
      ["Ordering", status.enabled ? "enabled" : "switched off in config", status.enabled ? "ok" : "bad"],
      ["dd-cli", status.installed ? `installed${status.binary ? ` · ${status.binary}` : ""}` : "not installed", status.installed ? "ok" : "bad"],
      ["Token", status.token_configured ? "from DD_CLI_ACCESS_TOKEN" : "from the OS keychain"],
      ["Account", signed === true ? "signed in" : signed === false ? "not signed in" : "unknown — tap Check account", signed === true ? "ok" : signed === false ? "bad" : ""],
      ["Default address", status.account?.default_address ? [status.account.default_address.label, status.account.default_address.street || status.account.default_address.address].filter(Boolean).join(" · ") : undefined],
      ["Saved addresses", status.account ? String(status.account.addresses) : undefined],
      ["Problem", status.error, "bad"],
    ]);
    ddLoginPanel.hidden = !status.login_in_progress;
    if (status.login_in_progress && status.login_url) {
      ddLoginPanel.replaceChildren(
        Friday.el("p", {}, "A sign-in is waiting. Open the link, sign in, and (on a phone) paste the address of the final page below."),
        Friday.el("a", { class: "primary", href: status.login_url, target: "_blank", rel: "noopener" }, "Open DoorDash sign-in"),
      );
    }
  }

  async function loadDoorDash(probe = false) {
    try {
      paintDoorDash(await Friday.api(`/api/doordash/status?probe=${probe}`));
    } catch (error) {
      Friday.toast(`DoorDash: ${error.message}`, "error");
    }
  }

  document.querySelector("#dd-check").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    ddStatus.classList.add("busy");
    try {
      await loadDoorDash(true);
    } finally {
      ddStatus.classList.remove("busy");
      event.currentTarget.disabled = false;
    }
  });

  document.querySelector("#dd-login").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const conversation = typeof ensureConversationId === "function" ? ensureConversationId() : null;
      const data = await Friday.api("/api/doordash/login", { method: "POST", body: { conversation_id: conversation } });
      if (data.ok === false) throw new Error(data.error || "Could not start the sign-in");
      Friday.toast("Sign-in link ready. It is also in your chat messages.");
      await loadDoorDash(false);
      if (data.url) {
        ddLoginPanel.hidden = false;
        ddLoginPanel.replaceChildren(
          Friday.el("p", {}, "Open the link, sign in, and (on a phone) paste the address of the final page below."),
          Friday.el("a", { class: "primary", href: data.url, target: "_blank", rel: "noopener" }, "Open DoorDash sign-in"),
        );
      }
    } catch (error) {
      Friday.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  ddCallbackForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.querySelector("#dd-callback");
    const pasted = input.value.trim();
    if (!pasted) return;
    try {
      const conversation = typeof ensureConversationId === "function" ? ensureConversationId() : null;
      const data = await Friday.api("/api/doordash/callback", { method: "POST", body: { conversation_id: conversation, callback: pasted } });
      if (data.ok === false) throw new Error(data.error || "Sign-in failed");
      Friday.toast(data.text || "DoorDash is signed in.");
      input.value = "";
      loadDoorDash(true);
    } catch (error) {
      Friday.toast(error.message, "error");
    }
  });

  // ---- Web search in place of a missing key, per feature ----------------------
  const fallbackList = document.querySelector("#web-fallback-list");

  function paintFallback(features) {
    fallbackList.replaceChildren();
    for (const feature of features || []) {
      const status = Friday.el("span", { class: "row-sub" });
      const describe = (on) => {
        status.textContent = feature.has_key
          ? `Key set (${feature.key}). Web search only if it stops working.`
          : on
            ? `No ${feature.key}: Friday searches the web instead.`
            : `No ${feature.key}: Friday tells you it's missing.`;
      };
      const host = Friday.el("span", {});
      const sw = Friday.toggle(host, {
        checked: feature.web_search,
        label: `Search the web for ${feature.label.toLowerCase()} without a key`,
        onChange: async (on) => {
          describe(on);
          try {
            await Friday.api("/api/house/web-fallback", { method: "PUT", body: { feature: feature.id, enabled: on } });
          } catch (error) {
            sw.set(!on);
            describe(!on);
            Friday.toast(error.message, "error");
          }
        },
      });
      describe(feature.web_search);
      fallbackList.append(
        Friday.el(
          "li",
          { class: "row" },
          Friday.el(
            "div",
            { class: "row-text" },
            Friday.el("span", { class: "row-title" }, `${feature.label} · ${feature.what}`),
            status,
          ),
          host,
        ),
      );
    }
  }

  // ---- Wi‑Fi, system, voices --------------------------------------------------
  async function loadHouse() {
    try {
      const data = await Friday.api("/api/house");
      paintFallback(data.web_fallback?.features);
      kv(wifiStatus, [
        ["Network", data.wifi.ssid || "not set", data.wifi.ssid ? "" : "bad"],
        ["Password", data.wifi.password_saved ? "saved" : "not saved", data.wifi.password_saved ? "ok" : "bad"],
      ]);
      const smart = Object.entries(data.smart_home || {})
        .filter(([, s]) => s.enabled)
        .map(([name, s]) => `${name}: ${s.connected ? "connected" : "not connected"} (${s.devices})${s.managed_process ? ` · ${s.managed_process}` : ""}`);
      kv(systemStatus, [
        ["Model", data.llm.model],
        ["LLM endpoint", data.llm.endpoint],
        ["Voice", data.speech.tts_voice],
        ["Household profile", data.household],
        ["Smart home", smart.join(" · ") || "no backends enabled"],
        ["Calendars", data.calendar.subscriptions ? `${data.calendar.subscriptions} subscribed · feed at ${location.origin}${data.calendar.feed}` : `none subscribed · feed at ${location.origin}${data.calendar.feed}`],
        ["Config files", data.files.join(", ")],
      ]);
      paintDoorDash({ ...data.doordash, signed_in: null, login_in_progress: false });
    } catch (error) {
      Friday.toast(`Settings: ${error.message}`, "error");
    }
  }

  document.querySelector("#wifi-edit").addEventListener("click", () => {
    wifiForm.hidden = !wifiForm.hidden;
    if (!wifiForm.hidden) wifiForm.ssid.focus();
  });
  wifiForm.querySelector("[data-cancel]").addEventListener("click", () => (wifiForm.hidden = true));
  wifiForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = wifiForm.querySelector("[type=submit]");
    button.disabled = true;
    try {
      await Friday.api("/api/house/wifi", { method: "PUT", body: { ssid: wifiForm.ssid.value.trim(), password: wifiForm.password.value } });
      Friday.toast("Wi‑Fi saved for new devices.");
      wifiForm.reset();
      wifiForm.hidden = true;
      loadHouse();
    } catch (error) {
      Friday.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  async function loadVoices() {
    try {
      const data = await Friday.api("/api/voice/profiles");
      voicesList.replaceChildren();
      const profiles = data.profiles || [];
      for (const v of profiles) {
        const li = Friday.el(
          "li",
          { class: "row" },
          Friday.el(
            "div",
            { class: "row-text" },
            Friday.el("span", { class: "row-title" }, v.profile),
            Friday.el("span", { class: "row-sub" }, `${v.samples} sample${v.samples === 1 ? "" : "s"} · ${v.seconds}s${v.needs_reenrollment ? " · needs re-enrolling" : ""}`),
          ),
        );
        const forget = Friday.el("button", { class: "ghost tiny danger", type: "button" }, "Forget");
        Friday.armed(forget, async () => {
          try {
            await Friday.api(`/api/voice/profiles/${encodeURIComponent(v.profile)}`, { method: "DELETE" });
            li.remove();
            voicesEmpty.hidden = voicesList.children.length > 0;
          } catch (error) {
            Friday.toast(error.message, "error");
          }
        });
        li.append(Friday.el("div", { class: "row-actions" }, forget));
        voicesList.append(li);
      }
      voicesEmpty.hidden = profiles.length > 0;
      if (!data.enabled) voicesEmpty.textContent = "Voice recognition is off on this house.";
    } catch (error) {
      voicesEmpty.hidden = false;
      voicesEmpty.textContent = `Voices unavailable: ${error.message}`;
    }
  }

  // Settings was one long column of unrelated cards. Four groups, one on
  // screen at a time: which tab was open is remembered per browser.
  const tabs = document.querySelector("#settings-tabs");
  const panels = [...document.querySelectorAll(".settings-panel")];

  function showGroup(name) {
    if (!panels.some((panel) => panel.dataset.settings === name)) name = panels[0]?.dataset.settings;
    for (const panel of panels) panel.hidden = panel.dataset.settings !== name;
    for (const tab of tabs.querySelectorAll("[data-settings-tab]")) {
      tab.setAttribute("aria-selected", String(tab.dataset.settingsTab === name));
    }
    try {
      localStorage.setItem("friday-settings-tab", name);
    } catch {}
  }

  tabs.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-settings-tab]");
    if (tab) showGroup(tab.dataset.settingsTab);
  });
  showGroup(localStorage.getItem("friday-settings-tab") || panels[0]?.dataset.settings);

  function loadAll() {
    loadTVs();
    loadHouse();
    loadDoorDash(false);
    loadVoices();
  }
  document.querySelector("#settings-refresh").addEventListener("click", loadAll);
  Friday.on("view", (v) => {
    if (v === "settings") loadAll();
  });
  if (Friday.view === "settings") loadAll();
})();
