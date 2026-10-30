// People: everyone the house knows. Each person has rules Friday follows on
// every turn, a reply style, and tastes for recommendations — all editable
// here in plain words. "Everyone" is the household: what applies to all.
(() => {
  const list = document.querySelector("#people-list");
  const panel = document.querySelector("#person");
  const nameEl = document.querySelector("#person-name");
  const eyebrow = document.querySelector("#person-eyebrow");
  const voiceEl = document.querySelector("#person-voice");
  const addForm = document.querySelector("#person-add-form");
  const addName = document.querySelector("#person-add-name");
  let household = "default";
  let selected = null;

  const KIND_LABEL = { rule: "rule", style: "style note", taste: "preference" };

  async function load() {
    try {
      const data = await Friday.api("/api/profiles");
      household = data.household;
      render(data.profiles || [], data.voice_recognition);
    } catch (error) {
      Friday.toast(`People: ${error.message}`, "error");
    }
  }

  function initials(name) {
    return name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0].toUpperCase())
      .join("");
  }

  function render(profiles, voiceOn) {
    list.replaceChildren();
    for (const person of profiles) {
      const isHome = person.household;
      const label = isHome ? "Everyone" : person.profile;
      const counts = [
        person.rules ? `${person.rules} rule${person.rules === 1 ? "" : "s"}` : "",
        person.style ? `${person.style} style` : "",
        person.taste ? `${person.taste} taste${person.taste === 1 ? "" : "s"}` : "",
      ].filter(Boolean);
      const li = Friday.el(
        "li",
        { class: `person-card ${isHome ? "household" : ""}`, dataset: { profile: person.profile } },
        Friday.el("span", { class: "avatar" }, isHome ? "★" : initials(person.profile)),
        Friday.el(
          "div",
          { class: "person-text" },
          Friday.el("span", { class: "person-title" }, label),
          Friday.el(
            "span",
            { class: "person-sub" },
            isHome
              ? "Applies to the whole household"
              : [
                  person.voice ? `voice enrolled${person.voice.needs_reenrollment ? " (needs a refresh)" : ""}` : voiceOn ? "no voice yet" : "",
                  ...counts,
                ]
                  .filter(Boolean)
                  .join(" · ") || "Nothing written down yet",
          ),
        ),
        Friday.el("span", { class: "chev", "aria-hidden": "true" }, "›"),
      );
      li.addEventListener("click", () => open(person));
      list.append(li);
    }
  }

  async function open(person) {
    selected = person;
    const isHome = person.household;
    nameEl.textContent = isHome ? "Everyone" : person.profile;
    eyebrow.textContent = isHome ? "Household · applies to everyone" : "Person";
    voiceEl.replaceChildren();
    if (!isHome && person.voice) {
      const v = person.voice;
      const forget = Friday.el("button", { class: "ghost tiny danger", type: "button" }, "Forget voice");
      Friday.armed(forget, async () => {
        try {
          await Friday.api(`/api/voice/profiles/${encodeURIComponent(person.profile)}`, { method: "DELETE" });
          Friday.toast("Voice forgotten.");
          voiceEl.replaceChildren(Friday.el("span", { class: "muted" }, "No voice enrolled"));
          load();
        } catch (error) {
          Friday.toast(error.message, "error");
        }
      });
      voiceEl.append(
        Friday.el("span", { class: "chip ok" }, Friday.el("span", { class: "chip-dot" }), `voice · ${v.samples} sample${v.samples === 1 ? "" : "s"} · ${v.seconds}s`),
        forget,
      );
    } else if (!isHome) {
      voiceEl.append(Friday.el("span", { class: "muted" }, "No voice enrolled — say hello in a chat"));
    }
    list.hidden = true;
    panel.hidden = false;
    document.body.classList.add("person-open");
    await loadNotes();
  }

  function back() {
    selected = null;
    panel.hidden = true;
    list.hidden = false;
    document.body.classList.remove("person-open");
    load();
  }

  async function loadNotes() {
    if (!selected) return;
    for (const kind of ["rule", "style", "taste"]) document.querySelector(`#notes-${kind}`).replaceChildren();
    try {
      const data = await Friday.api(`/api/profiles/${encodeURIComponent(selected.profile)}/notes`);
      for (const note of data.notes || []) {
        const host = document.querySelector(`#notes-${note.kind}`) || document.querySelector("#notes-taste");
        host.append(renderNote(note));
      }
    } catch (error) {
      Friday.toast(error.message, "error");
    }
    for (const kind of ["rule", "style", "taste"]) {
      const host = document.querySelector(`#notes-${kind}`);
      if (!host.children.length) host.append(Friday.el("li", { class: "note empty-note" }, `No ${KIND_LABEL[kind]}s yet.`));
    }
  }

  function renderNote(note) {
    const li = Friday.el("li", { class: "note", dataset: { id: note.id } });
    const text = Friday.el("p", { class: "note-text" }, note.note);
    const editor = Friday.el("textarea", { class: "note-editor", maxlength: "500", rows: "2", hidden: true }, note.note);
    const edit = Friday.el("button", { class: "icon-btn tiny", type: "button", "aria-label": "Edit", html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l10-10-4-4L4 16zM13 7l4 4"/></svg>' });
    const del = Friday.el("button", { class: "icon-btn tiny danger", type: "button", "aria-label": "Delete", html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>' });
    const save = Friday.el("button", { class: "primary tiny", type: "button", hidden: true }, "Save");
    const cancel = Friday.el("button", { class: "ghost tiny", type: "button", hidden: true }, "Cancel");
    const setEditing = (on) => {
      text.hidden = on;
      editor.hidden = !on;
      save.hidden = !on;
      cancel.hidden = !on;
      edit.hidden = on;
      del.hidden = on;
      if (on) {
        editor.value = text.textContent;
        editor.focus();
      }
    };
    edit.addEventListener("click", () => setEditing(true));
    text.addEventListener("dblclick", () => setEditing(true));
    cancel.addEventListener("click", () => setEditing(false));
    save.addEventListener("click", async () => {
      const value = editor.value.trim();
      if (!value) return;
      try {
        const data = await Friday.api(`/api/notes/${note.id}`, { method: "PUT", body: { note: value } });
        text.textContent = data.note.note;
        setEditing(false);
        Friday.toast("Saved.");
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    });
    editor.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        save.click();
      }
      if (event.key === "Escape") setEditing(false);
    });
    Friday.armed(del, async () => {
      try {
        await Friday.api(`/api/notes/${note.id}`, { method: "DELETE" });
        li.classList.add("gone");
        setTimeout(() => li.remove(), 200);
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    }, { label: "Delete?" });
    li.append(text, editor, Friday.el("div", { class: "note-actions" }, edit, del, save, cancel));
    return li;
  }

  for (const form of panel.querySelectorAll("form[data-kind]")) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!selected) return;
      const input = form.querySelector("input");
      const value = input.value.trim();
      if (!value) return;
      try {
        const data = await Friday.api(`/api/profiles/${encodeURIComponent(selected.profile)}/notes`, {
          method: "POST",
          body: { note: value, kind: form.dataset.kind },
        });
        const host = document.querySelector(`#notes-${form.dataset.kind}`);
        host.querySelector(".empty-note")?.remove();
        host.append(renderNote(data.note));
        input.value = "";
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    });
  }

  addForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const name = addName.value.trim();
    if (!name) return;
    addName.value = "";
    open({ profile: name, household: name === household, voice: null, rules: 0, style: 0, taste: 0 });
    Friday.toast(`Add a rule or preference and ${name} is saved.`);
  });

  document.querySelector("#person-back").addEventListener("click", back);
  Friday.on("view", (v) => {
    if (v === "people" && !selected) load();
  });
  if (Friday.view === "people") load();
})();
