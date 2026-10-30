// Settings → Assistant model: which AI answers. One dropdown holds the lot —
// the models already on this computer at the top, because that is the point
// of the house running its own, and the paid cloud ones under them behind a
// warning. Everything else is a search away.
(() => {
  const open = document.querySelector("#model-open");
  const openName = document.querySelector("#model-open-name");
  const openWhere = document.querySelector("#model-open-where");
  const menu = document.querySelector("#model-menu");
  const groups = document.querySelector("#model-groups");
  const menuEmpty = document.querySelector("#model-menu-empty");
  const search = document.querySelector("#model-search");
  const current = document.querySelector("#model-current");
  const warning = document.querySelector("#model-warning");
  const warningText = document.querySelector("#model-warning-text");
  const form = document.querySelector("#model-form");
  const keyRow = document.querySelector("#model-key-row");
  const keyInput = document.querySelector("#model-key");
  const keyForget = document.querySelector("#model-key-forget");
  const consentRow = document.querySelector("#model-consent-row");
  const consent = document.querySelector("#model-consent");
  const consentText = document.querySelector("#model-consent-text");
  const result = document.querySelector("#model-result");
  const urlRow = document.querySelector("#model-url-row");
  const urlInput = document.querySelector("#model-url");
  const nameRow = document.querySelector("#model-name-row");
  const nameInput = document.querySelector("#model-name-input");
  const save = document.querySelector("#model-save");

  const KEY_HINT = {
    openai: "sk-… from platform.openai.com",
    anthropic: "sk-ant-… from console.anthropic.com",
    openrouter: "sk-or-… from openrouter.ai/keys",
    custom: "Only if the endpoint needs one",
  };
  // How many of each kind the dropdown shows before the search box takes
  // over. Five is a shortlist; anything longer is a list to read.
  const SHORTLIST = 5;

  // What makes a local model a good one to run a house on: the house's own
  // tuned build first, then the families that behave with tools at a size
  // that fits one consumer card. Everything installed is still reachable
  // through the search box — this only decides what surfaces without it.
  const LOCAL_RANK = [
    { test: /^house-/i, note: "tuned for this house" },
    { test: /qwen.*\b(14b)\b/i, note: "recommended · fits a 16GB card" },
    { test: /gpt-?oss.*\b20b\b/i, note: "good with tools" },
    { test: /qwen.*\b(27b|30b|32b)\b/i, note: "sharper, needs ~24GB" },
    { test: /qwen.*a3b/i, note: "mixture-of-experts, fast" },
    { test: /qwen/i, note: "" },
    { test: /llama|mistral|gemma|phi|deepseek/i, note: "" },
  ];

  function rankLocal(name) {
    const index = LOCAL_RANK.findIndex((rule) => rule.test.test(name));
    return index === -1 ? LOCAL_RANK.length : index;
  }

  // The models Friday offers to download (the same list the installer
  // shows), from /api/assistant-model. Each installs as its own house-* build.
  const bare = (name) => String(name).replace(/:latest$/, "");
  const offered = (name) =>
    (state?.local_catalog || []).find((m) => m.name === bare(name) || m.base === bare(name));
  const gb = (bytes) => (bytes / 1e9).toFixed(1);

  function noteFor(name) {
    const entry = offered(name);
    if (entry && bare(name) === entry.name) return `${entry.base} · ${entry.note}`;
    const note = LOCAL_RANK[rankLocal(name)]?.note || "";
    // A raw model is given Friday's 32k window (as a house- build) on switching.
    if (!/^house-/i.test(name)) return [note, "set up for Friday when picked"].filter(Boolean).join(" · ");
    return note;
  }

  // "qwen3.8:27b" reads as a filename; "Qwen 3.8 27B" reads as a model. The
  // steps are ordered: a3b is uppercased before the family name is split off
  // its version, or "a3b" would become "a 3 b".
  function pretty(name) {
    const known = CLOUD_LABELS.get(String(name));
    if (known) return known;
    return String(name)
      .replace(/:latest$/, "")
      .replace(/[:_/]/g, " ")
      .replace(/-/g, " ")
      .replace(/\b(a\d+b)\b/gi, (m) => m.toUpperCase())
      .replace(/\bgpt\s?oss\b/i, "GPT-OSS")
      .replace(/([a-z])(\d)/g, "$1 $2")
      .replace(/\b(\d+(?:\.\d+)?)\s?([bm])\b/g, (_, size, unit) => `${size}${unit.toUpperCase()}`)
      .replace(/\b[a-z]/g, (ch) => ch.toUpperCase())
      .replace(/\s+/g, " ")
      .trim();
  }

  // The cloud shortlist is fixed: these are the models worth putting in front
  // of someone, not whatever a provider's /models happens to return first.
  const CLOUD_PICKS = [
    { provider: "anthropic", model: "claude-opus-5", label: "Claude Opus 5" },
    { provider: "anthropic", model: "claude-sonnet-5", label: "Claude Sonnet 5" },
    { provider: "openai", model: "gpt-5.6", label: "GPT-5.6" },
    { provider: "openai", model: "gpt-5.5", label: "GPT-5.5" },
    { provider: "openrouter", model: "openai/gpt-5.6", label: "GPT-5.6 (OpenRouter)" },
  ];
  // Any other OpenAI-compatible API: an agent gateway, Hermes on Nous
  // Portal, LM Studio or vLLM on another computer. Always offered.
  const CUSTOM_ENTRY = {
    provider: "custom",
    model: "",
    label: "Custom endpoint",
    note: "any OpenAI-compatible API or agent (Hermes, LM Studio, vLLM…)",
    tag: "custom",
  };
  // So the closed button shows "Claude Opus 5" rather than a prettified id.
  const CLOUD_LABELS = new Map(CLOUD_PICKS.map((pick) => [pick.model, pick.label]));

  let state = null;
  let chosen = { provider: "local", model: "" };
  let localModels = [];
  const cloudModels = new Map(); // provider -> [model names]

  const spec = (id) => state?.providers.find((p) => p.id === id);

  function privacyText(id) {
    if (id === "custom") {
      return (
        "Everything you say or type to Friday is sent to the endpoint address below, along with the names of people " +
        "in the house, rooms, devices, calendar and lists. If it runs on your own network with a local model, that " +
        "stays private; if it's a hosted service, it's stored under that company's data policy."
      );
    }
    const name = spec(id)?.label || id;
    const where =
      id === "openrouter"
        ? "OpenRouter and to whichever company runs the model you pick (for example OpenAI, Anthropic, Google or Meta's hosts)"
        : `${name}'s servers`;
    return (
      `Everything you say or type to Friday is sent to ${where} to be answered, and may be saved there under ` +
      `their data policy. That includes the names of people in the house, your rooms and devices, calendar, lists, ` +
      `reminders, what's on the TV, food orders, and anything Friday looks up for you. Recorded audio stays on this ` +
      `computer; only the text leaves. Switch back to a model on this computer at any time to be fully private again.`
    );
  }

  // ---- the dropdown ---------------------------------------------------------
  function row(entry) {
    const picked = entry.provider === chosen.provider && entry.model === chosen.model;
    const button = Friday.el(
      "button",
      {
        class: `model-row${picked ? " picked" : ""}`,
        type: "button",
        role: "option",
        "aria-selected": String(picked),
      },
      Friday.el(
        "span",
        { class: "model-row-text" },
        Friday.el("strong", {}, entry.label),
        entry.note ? Friday.el("small", {}, entry.note) : null,
      ),
      Friday.el(
        "span",
        { class: `model-tag ${entry.download ? "download" : entry.provider === "local" ? "local" : "cloud"}` },
        entry.tag,
      ),
    );
    button.addEventListener("click", () => {
      choose(entry.provider, entry.model, entry.download);
      setOpen(false);
    });
    return button;
  }

  function group(title, entries, { toggle = false, hint = "" } = {}) {
    if (!entries.length) return null;
    return Friday.el(
      "section",
      { class: "model-group" },
      Friday.el(
        "header",
        { class: "model-group-head" },
        Friday.el("h4", {}, title),
        toggle ? Friday.el("span", { class: "model-group-pill" }, "private") : null,
      ),
      hint ? Friday.el("p", { class: "model-group-hint" }, hint) : null,
      ...entries.map(row),
    );
  }

  // Offered models whose house build isn't installed yet.
  function downloadEntries() {
    const installed = new Set(localModels.map(bare));
    return (state?.local_catalog || [])
      .filter((m) => !installed.has(m.name))
      .map((m) => ({
        provider: "local",
        model: m.name,
        label: pretty(m.base),
        note: `${m.size_gb} GB · ${m.note} · needs a ${m.needs}`,
        tag: "download",
        download: m,
      }));
  }

  function localEntries() {
    return [...localModels]
      .sort((a, b) => rankLocal(a) - rankLocal(b) || a.localeCompare(b))
      .map((name) => ({
        provider: "local",
        model: name,
        label: pretty(name),
        note: noteFor(name),
        tag: "local",
      }));
  }

  function cloudEntries() {
    const seen = new Set();
    const picks = CLOUD_PICKS.map((pick) => {
      seen.add(`${pick.provider}/${pick.model}`);
      return {
        provider: pick.provider,
        model: pick.model,
        label: pick.label,
        note: spec(pick.provider)?.has_key ? "key saved" : "needs an API key",
        tag: spec(pick.provider)?.label || pick.provider,
      };
    });
    const rest = [];
    for (const [provider, names] of cloudModels) {
      for (const name of names) {
        if (seen.has(`${provider}/${name}`)) continue;
        rest.push({
          provider,
          model: name,
          label: name,
          note: "",
          tag: spec(provider)?.label || provider,
        });
      }
    }
    return { picks, rest };
  }

  function paintMenu() {
    const query = search.value.trim().toLowerCase();
    const matches = (entry) =>
      !query || `${entry.label} ${entry.model}`.toLowerCase().includes(query);
    const local = localEntries().filter(matches);
    const downloads = downloadEntries().filter(matches);
    const { picks, rest } = cloudEntries();
    const cloud = [...picks, ...rest].filter(matches);
    const custom = matches(CUSTOM_ENTRY) ? [CUSTOM_ENTRY] : [];
    // Without a search this is a shortlist; with one it is everything.
    const shownLocal = query ? local : local.slice(0, SHORTLIST);
    const shownCloud = query ? cloud : cloud.slice(0, SHORTLIST);

    groups.replaceChildren(
      ...[
        group("On this computer", shownLocal, {
          toggle: true,
          hint: local.length
            ? "Free, and nothing leaves the house."
            : "",
        }),
        group("Download to this computer", downloads, {
          hint: "Free and private once downloaded. Pick one, then press Download.",
        }),
        group("Cloud providers", shownCloud, {
          hint: "Faster and sharper, but every word you say is sent to them, and each needs an API key.",
        }),
        group("Your own", custom, {
          hint: "Another AI service or agent you run or subscribe to. It must support tool calling.",
        }),
      ].filter(Boolean),
    );
    const total = shownLocal.length + downloads.length + shownCloud.length + custom.length;
    menuEmpty.hidden = total > 0;
    if (!query && !local.length) {
      groups.prepend(
        Friday.el(
          "p",
          { class: "model-group-hint model-none" },
          "No models are installed on this computer yet. Pick one under " +
            "“Download to this computer” (needs Ollama running on the house computer).",
        ),
      );
    }
  }

  function setOpen(isOpen) {
    menu.hidden = !isOpen;
    open.setAttribute("aria-expanded", String(isOpen));
    if (isOpen) {
      search.value = "";
      paintMenu();
      search.focus();
    }
  }

  open.addEventListener("click", () => setOpen(menu.hidden));
  search.addEventListener("input", paintMenu);
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target) && event.target !== open && !open.contains(event.target)) {
      setOpen(false);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) setOpen(false);
  });

  // ---- what is chosen -------------------------------------------------------
  function choose(provider, model, download = null) {
    chosen = { provider, model, download };
    const cloud = provider !== "local";
    const providerSpec = spec(provider);
    const isCustom = provider === "custom";
    urlRow.hidden = !isCustom;
    nameRow.hidden = !isCustom;
    if (isCustom) {
      urlInput.value = providerSpec?.base_url || "";
      nameInput.value = model || providerSpec?.model || "";
    }
    openName.textContent = isCustom
      ? "Custom endpoint"
      : download
        ? pretty(download.base)
        : pretty(model) || "Pick a model";
    openWhere.textContent = cloud
      ? `${providerSpec?.label || provider} · cloud`
      : download
        ? `Download · ${download.size_gb} GB · private`
        : "On this computer · private";
    open.classList.toggle("cloud", cloud);
    warning.hidden = !cloud;
    warningText.textContent = cloud ? privacyText(provider) : "";
    keyRow.hidden = !cloud;
    keyInput.value = "";
    keyInput.placeholder = providerSpec?.has_key ? "Saved. Paste a new key to replace it." : KEY_HINT[provider] || "";
    keyForget.hidden = !providerSpec?.has_key;
    consentRow.hidden = !cloud;
    consent.checked = false;
    consentText.textContent = cloud
      ? provider === "custom"
        ? "I understand my conversations will be sent to this endpoint."
        : `I understand my conversations will be sent to and stored by ${provider === "openrouter" ? "OpenRouter and the model's provider" : providerSpec?.label}.`
      : "";
    result.textContent = "";
    const live = provider === state?.provider && bare(model) === bare(state?.model);
    save.textContent = live ? "In use" : download ? `Download (${download.size_gb} GB)` : cloud ? "Test & switch" : "Switch";
    save.disabled = live || downloading();
  }

  // ---- downloading a model --------------------------------------------------
  let pollTimer = null;
  const downloading = () => ["starting", "downloading", "building"].includes(state?.download?.status);

  function paintDownload(d) {
    if (!d || !d.status) return;
    const name = pretty(d.base);
    if (d.status === "downloading" && d.total) {
      const pct = Math.floor((d.completed / d.total) * 100);
      result.textContent = `Downloading ${name}: ${pct}% (${gb(d.completed)} of ${gb(d.total)} GB)`;
    } else if (d.status === "starting" || d.status === "downloading") {
      result.textContent = `Downloading ${name}…`;
    } else if (d.status === "building") {
      result.textContent = `Setting up ${name} for Friday…`;
    } else if (d.status === "failed") {
      result.textContent = `Downloading ${name} failed: ${d.error || "unknown error"}`;
    } else if (d.status === "done") {
      result.textContent = `${name} is downloaded. Press Switch to use it.`;
    }
  }

  async function pollDownload() {
    clearTimeout(pollTimer);
    try {
      const d = await Friday.api("/api/assistant-model/download");
      state.download = d;
      paintDownload(d);
      if (downloading()) {
        pollTimer = setTimeout(pollDownload, 1500);
        return;
      }
      save.disabled = false;
      if (d.status === "done") {
        await loadList("local");
        choose("local", d.name);
        paintDownload(d);
        Friday.toast(`${pretty(d.base)} is ready.`);
      }
    } catch {
      pollTimer = setTimeout(pollDownload, 5000);
    }
  }

  async function startDownload(entry) {
    save.disabled = true;
    result.textContent = `Starting the download of ${pretty(entry.base)}…`;
    try {
      state.download = await Friday.api("/api/assistant-model/download", {
        method: "POST",
        body: { model: entry.name },
      });
      pollDownload();
    } catch (error) {
      result.textContent = error.message;
      save.disabled = false;
    }
  }

  function paintCurrent() {
    const where = state.cloud ? `${state.label} (cloud)` : "this computer";
    current.textContent = `Now: ${state.model} on ${where}`;
    current.classList.toggle("cloud", state.cloud);
    if (state.fell_back) {
      result.textContent = `The last cloud request failed, so the local model answered: ${state.fell_back}`;
    }
  }

  async function loadList(provider) {
    try {
      const data = await Friday.api(`/api/assistant-model/models?provider=${provider}`);
      const names = data.models || [];
      if (provider === "local") localModels = names;
      else cloudModels.set(provider, names);
    } catch {
      if (provider === "local") localModels = [];
    }
    if (!menu.hidden) paintMenu();
  }

  async function load() {
    try {
      state = await Friday.api("/api/assistant-model");
      paintCurrent();
      choose(state.provider, state.model);
      if (downloading()) pollDownload();
      // The local list is the one that matters here, so it is not made to
      // wait behind three cloud calls that may not even have a key.
      await loadList("local");
      for (const provider of state.providers) {
        if (provider.id !== "local" && provider.has_key) loadList(provider.id);
      }
    } catch (error) {
      current.textContent = error.message;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (chosen.provider === "custom") {
      chosen.model = nameInput.value.trim();
      if (!urlInput.value.trim()) {
        result.textContent = "Give the endpoint's address first.";
        urlInput.focus();
        return;
      }
    }
    if (!chosen.model) {
      result.textContent = chosen.provider === "custom" ? "Give the model name." : "Pick a model first.";
      return;
    }
    if (chosen.download) {
      startDownload(chosen.download);
      return;
    }
    const cloud = chosen.provider !== "local";
    if (cloud && !consent.checked) {
      result.textContent = "Tick the box to confirm you understand what gets sent.";
      consent.focus();
      return;
    }
    save.disabled = true;
    result.textContent = "Testing…";
    try {
      const data = await Friday.api("/api/assistant-model", {
        method: "PUT",
        body: {
          provider: chosen.provider,
          model: chosen.model,
          api_key: keyInput.value.trim() || null,
          accept_cloud: consent.checked,
          base_url: chosen.provider === "custom" ? urlInput.value.trim() : null,
        },
      });
      state = data;
      paintCurrent();
      choose(state.provider, state.model);
      result.textContent = "Switched. Friday is answering with this model now.";
      Friday.toast(`Friday now uses ${pretty(data.model)}.`);
      Friday.emit("assistant-model", data);
    } catch (error) {
      result.textContent = error.message;
      save.disabled = false;
    }
  });

  Friday.armed(keyForget, async () => {
    try {
      state = await Friday.api(`/api/assistant-model/key?provider=${chosen.provider}`, { method: "DELETE" });
      paintCurrent();
      choose(state.provider, state.model);
      Friday.toast("Key removed.");
      Friday.emit("assistant-model", state);
    } catch (error) {
      Friday.toast(error.message, "error");
    }
  }, { label: "Remove?" });

  Friday.on("view", (view) => {
    if (view === "settings") load();
  });
  if (Friday.view === "settings") load();
})();
