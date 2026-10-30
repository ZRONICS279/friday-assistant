// Settings → Friday's voice. Fifty-odd voices named Heart, Fenrir and Puck
// are not something anyone can search: they are grouped by accent in a
// dropdown, with the everyday English ones first. Kokoro runs on this
// computer and takes about a second a line, so there are no stored samples
// to keep in sync — tapping a voice synthesizes the line in it right then.
(() => {
  const open = document.querySelector("#voice-open");
  if (!open) return;
  const openName = document.querySelector("#voice-open-name");
  const openWhere = document.querySelector("#voice-open-where");
  const menu = document.querySelector("#voice-menu");
  const groups = document.querySelector("#voice-groups");
  const search = document.querySelector("#voice-search");
  const empty = document.querySelector("#voice-empty");
  const currentLabel = document.querySelector("#voice-current");
  const note = document.querySelector("#voice-note");

  // The house speaks English and Spanish, so those come first; the rest are
  // there for anyone who wants them.
  const ORDER = ["American", "British", "Spanish"];

  let voices = [];
  let picked = null;
  let spanish = null;
  let sample = "";
  let spanishSample = "";
  let audio = null;
  let playing = null;
  let loaded = false;

  function stop() {
    if (audio) {
      audio.pause();
      audio.src = "";
      audio = null;
    }
    playing = null;
  }

  const byId = (id) => voices.find((voice) => voice.id === id);
  // A Spanish voice reading an English line comes out as nonsense, so each
  // auditions in its own language.
  const lineFor = (voice) => (voice.language === "Spanish" ? spanishSample : sample);
  const describe = (voice) => [voice.accent, voice.gender, voice.description].filter(Boolean).join(" · ");

  async function audition(voice) {
    stop();
    playing = voice.id;
    paintMenu();
    try {
      const response = await fetch("/api/speech", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: lineFor(voice), voice: voice.id }),
      });
      if (!response.ok) throw new Error(`Could not speak that voice (${response.status})`);
      const url = URL.createObjectURL(await response.blob());
      audio = new Audio(url);
      audio.addEventListener("ended", () => {
        URL.revokeObjectURL(url);
        playing = null;
        paintMenu();
      });
      await audio.play();
    } catch (error) {
      playing = null;
      paintMenu();
      Friday.toast(error.message, "error");
    }
  }

  async function keep(voice) {
    // Spanish replies have a voice of their own; picking one sets that slot
    // rather than replacing the everyday voice.
    const body = voice.language === "Spanish" ? { spanish_voice: voice.id } : { voice: voice.id };
    try {
      const data = await Friday.api("/api/tts/voice", { method: "PUT", body });
      picked = data.voice;
      spanish = data.spanish_voice;
      paintCurrent();
      paintMenu();
      Friday.toast(`Friday now speaks as ${voice.name}.`);
    } catch (error) {
      Friday.toast(error.message, "error");
    }
  }

  function row(voice) {
    const inUse = voice.id === picked || voice.id === spanish;
    const isSpanishSlot = voice.id === spanish && voice.id !== picked;
    const play = Friday.el(
      "button",
      { class: "voice-play", type: "button", "aria-label": `Hear ${voice.name}` },
      Friday.el("span", { class: "voice-wave", html: "<i></i><i></i><i></i><i></i>" }),
    );
    play.addEventListener("click", (event) => {
      event.stopPropagation();
      audition(voice);
    });
    const action = inUse
      ? Friday.el("span", { class: "voice-badge" }, isSpanishSlot ? "Spanish" : "In use")
      : Friday.el("button", { class: "ghost tiny voice-keep", type: "button" }, "Use");
    if (!inUse) {
      action.addEventListener("click", (event) => {
        event.stopPropagation();
        keep(voice);
      });
    }
    const item = Friday.el(
      "div",
      {
        class: `model-row voice-row${inUse ? " picked" : ""}${playing === voice.id ? " playing" : ""}`,
        role: "option",
        "aria-selected": String(inUse),
      },
      play,
      Friday.el(
        "span",
        { class: "model-row-text" },
        Friday.el("strong", {}, voice.name),
        Friday.el("small", {}, describe(voice)),
      ),
      action,
    );
    // Anywhere else on the row plays it: the whole row is the audition.
    item.addEventListener("click", () => audition(voice));
    return item;
  }

  function paintMenu() {
    const query = search.value.trim().toLowerCase();
    const shown = voices.filter(
      (voice) => !query || `${voice.name} ${voice.accent} ${voice.language} ${voice.description}`.toLowerCase().includes(query),
    );
    const byAccent = new Map();
    for (const voice of shown) {
      const key = voice.accent || voice.language;
      if (!byAccent.has(key)) byAccent.set(key, []);
      byAccent.get(key).push(voice);
    }
    const accents = [...byAccent.keys()].sort((a, b) => {
      const rank = (name) => (ORDER.includes(name) ? ORDER.indexOf(name) : ORDER.length);
      return rank(a) - rank(b) || a.localeCompare(b);
    });
    groups.replaceChildren(
      ...accents.map((accent) =>
        Friday.el(
          "section",
          { class: "model-group" },
          Friday.el(
            "header",
            { class: "model-group-head" },
            Friday.el("h4", {}, accent),
            accent === "Spanish" ? Friday.el("span", { class: "model-group-pill" }, "for Spanish") : null,
          ),
          ...byAccent.get(accent).map(row),
        ),
      ),
    );
    empty.hidden = shown.length > 0;
    if (!shown.length && voices.length) empty.textContent = "No voice matches that.";
  }

  function paintCurrent() {
    const now = byId(picked);
    const other = byId(spanish);
    openName.textContent = now ? now.name : picked || "Pick a voice";
    openWhere.textContent = now ? describe(now) : "";
    currentLabel.textContent = other ? `${other.name} for Spanish` : "";
  }

  function setOpen(isOpen) {
    menu.hidden = !isOpen;
    open.setAttribute("aria-expanded", String(isOpen));
    if (isOpen) {
      search.value = "";
      paintMenu();
      search.focus();
    } else {
      stop();
    }
  }

  open.addEventListener("click", () => setOpen(menu.hidden));
  search.addEventListener("input", paintMenu);
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target) && !open.contains(event.target)) setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) setOpen(false);
  });

  async function load() {
    if (loaded) return;
    loaded = true;
    try {
      const data = await Friday.api("/api/tts/voices");
      voices = data.voices || [];
      picked = data.voice;
      spanish = data.spanish_voice;
      sample = data.sample || "";
      spanishSample = data.spanish_sample || sample;
      note.textContent = data.error || `${voices.length} voices, all generated on this computer.`;
      paintCurrent();
    } catch (error) {
      loaded = false;
      note.textContent = error.message;
    }
  }

  Friday.on("view", (view) => {
    if (view === "settings") load();
    else {
      stop();
      setOpen(false);
    }
  });
  if (Friday.view === "settings") load();
})();
