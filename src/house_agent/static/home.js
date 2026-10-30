// Home: what is coming up (by day), what the house does on its own
// (active and paused), and the running lists (one at a time). A strip of
// tiles at the top sums it up and jumps to the section.
(() => {
  const days = document.querySelector("#calendar-days");
  const calendarEmpty = document.querySelector("#calendar-empty");
  const activeGroup = document.querySelector("#automations-active");
  const pausedGroup = document.querySelector("#automations-paused");
  const automationsEmpty = document.querySelector("#automations-empty");
  const automationsSub = document.querySelector("#automations-sub");
  const listTabs = document.querySelector("#list-tabs");
  const lists = document.querySelector("#lists-body");
  const listsEmpty = document.querySelector("#lists-empty");
  const stat = (id) => document.querySelector(`#${id}`);
  // The phone's home screen shows the same plans and automations, so a
  // change made here has to reach it — otherwise a deleted reminder sits
  // on the home screen until its cache expires.
  const changed = () => Friday.emit("planner");
  let currentList = localStorage.getItem("friday-list") || null;

  for (const tileButton of document.querySelectorAll("#home-stats .stat")) {
    tileButton.addEventListener("click", () => {
      document.querySelector(`#${tileButton.dataset.jump}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  // ---- calendar --------------------------------------------------------------
  function dayLabel(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "Sometime";
    const today = new Date();
    const tomorrow = new Date(today);
    tomorrow.setDate(today.getDate() + 1);
    if (date.toDateString() === today.toDateString()) return "Today";
    if (date.toDateString() === tomorrow.toDateString()) return "Tomorrow";
    return date.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
  }

  function clock(event) {
    if (/all day/i.test(event.when || "")) return "all day";
    const date = new Date(event.starts_at);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  async function loadCalendar() {
    try {
      const data = await Friday.api("/api/calendar?days=7");
      const events = (data.events || []).filter((e) => !e.past);
      days.replaceChildren();
      const groups = new Map();
      for (const event of events) {
        const label = dayLabel(event.starts_at);
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push(event);
      }
      for (const [label, items] of groups) {
        const block = Friday.el("div", { class: "day" }, Friday.el("h3", { class: "group-head" }, label));
        const ul = Friday.el("ul", { class: "rows" });
        for (const event of items) ul.append(renderEvent(event));
        block.append(ul);
        days.append(block);
      }
      calendarEmpty.hidden = events.length > 0;
      const next = events[0];
      stat("stat-next").textContent = next ? next.title : "Nothing";
      stat("stat-next-when").textContent = next ? `${dayLabel(next.starts_at)} · ${clock(next)}` : "this week";
    } catch (error) {
      days.replaceChildren();
      calendarEmpty.hidden = false;
      calendarEmpty.textContent = `Calendar unavailable: ${error.message}`;
    }
  }

  function renderEvent(event) {
    const external = typeof event.id === "string" && !/^\d+$/.test(event.id);
    const li = Friday.el(
      "li",
      { class: "row event" },
      Friday.el("span", { class: "event-time" }, clock(event)),
      Friday.el(
        "div",
        { class: "row-text" },
        Friday.el("span", { class: "row-title" }, event.title),
        Friday.el("span", { class: "row-sub" }, [event.location, external ? event.calendar : "", event.for ? `for ${event.for}` : ""].filter(Boolean).join(" · ")),
      ),
    );
    if (!external) {
      const del = Friday.el("button", { class: "icon-btn tiny danger", type: "button", "aria-label": "Delete", html: "&#10005;" });
      Friday.armed(del, async () => {
        try {
          await Friday.api(`/api/calendar/${event.id}`, { method: "DELETE" });
          loadCalendar();
          changed();
        } catch (error) {
          Friday.toast(error.message, "error");
        }
      }, { label: "?" });
      li.append(Friday.el("div", { class: "row-actions" }, del));
    }
    return li;
  }

  // ---- automations -----------------------------------------------------------
  function describeSchedule(schedule) {
    if (!schedule || typeof schedule !== "object") return "";
    if (schedule.at) {
      const when = new Date(schedule.at);
      return Number.isNaN(when.getTime())
        ? String(schedule.at)
        : when.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    }
    const parts = [];
    if (schedule.repeat && schedule.repeat !== "once") parts.push(schedule.repeat);
    if (schedule.time) parts.push(`at ${schedule.time}`);
    if (schedule.days?.length) parts.push(schedule.days.join(", "));
    if (schedule.every) parts.push(`every ${schedule.every}`);
    return parts.join(" · ") || JSON.stringify(schedule);
  }

  function describeAction(action) {
    if (!action || typeof action !== "object") return "";
    if (action.text) return action.text;
    if (action.question) return action.question;
    if (action.tool) return `${action.tool}${action.arguments ? " " + JSON.stringify(action.arguments) : ""}`;
    if (action.type === "fade") {
      const from = action.brightness_from != null ? `from ${action.brightness_from}% ` : "";
      const over = action.minutes ? ` over ${action.minutes} min` : "";
      const to = action.brightness_to === 0 ? "off" : `${action.brightness_to}%`;
      return `Fade ${action.label || action.device} ${from}to ${to}${over}`;
    }
    if (action.type === "device") {
      const parts = [action.power && `turn ${action.power}`, action.brightness != null && `${action.brightness}%`, action.color_temp && `${action.color_temp}K`].filter(Boolean);
      return `${action.label || action.device}: ${parts.join(", ")}`;
    }
    return JSON.stringify(action);
  }

  function nextRun(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return `${dayLabel(iso)} ${date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }

  async function loadAutomations() {
    try {
      const data = await Friday.api("/api/automations?all=true");
      const items = data.automations || [];
      const active = items.filter((item) => item.enabled);
      const paused = items.filter((item) => !item.enabled);
      const fill = (group, list) => {
        const ul = group.querySelector("ul");
        ul.replaceChildren();
        for (const item of list) ul.append(renderAutomation(item));
        group.hidden = list.length === 0;
      };
      fill(activeGroup, active);
      fill(pausedGroup, paused);
      automationsEmpty.hidden = items.length > 0;
      const fades = data.fades_in_progress || [];
      automationsSub.textContent = fades.length ? `${fades.length} fade${fades.length === 1 ? "" : "s"} running` : `${active.length} active`;
      stat("stat-automations").textContent = String(active.length);
      stat("stat-automations-sub").textContent = paused.length ? `${paused.length} paused` : active.length ? "all running" : "none set";
    } catch (error) {
      Friday.toast(`Automations: ${error.message}`, "error");
    }
  }

  function renderAutomation(item) {
    const li = Friday.el("li", { class: "row" });
    const text = Friday.el(
      "div",
      { class: "row-text" },
      Friday.el("span", { class: "row-title" }, item.name || describeAction(item.action)),
      Friday.el(
        "span",
        { class: "row-sub" },
        [describeSchedule(item.schedule), item.enabled && item.next_run_at ? `next ${nextRun(item.next_run_at)}` : "", item.profile ? `for ${item.profile}` : ""]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    if (item.name && describeAction(item.action) !== item.name) {
      text.append(Friday.el("span", { class: "row-sub dim" }, describeAction(item.action)));
    }
    const controls = Friday.el("div", { class: "row-actions" });
    const sw = Friday.el("div");
    Friday.toggle(sw, {
      checked: Boolean(item.enabled),
      label: `${item.name} enabled`,
      onChange: async (on) => {
        try {
          await Friday.api(`/api/automations/${item.id}/enabled?enabled=${on}`, { method: "PUT" });
          loadAutomations();
          changed();
        } catch (error) {
          Friday.toast(error.message, "error");
        }
      },
    });
    const run = Friday.el("button", { class: "ghost tiny", type: "button" }, "Run now");
    run.addEventListener("click", async () => {
      run.disabled = true;
      try {
        await Friday.api(`/api/automations/${item.id}/run`, { method: "POST" });
        Friday.toast("Ran it.");
      } catch (error) {
        Friday.toast(error.message, "error");
      } finally {
        run.disabled = false;
      }
    });
    const del = Friday.el("button", { class: "icon-btn tiny danger", type: "button", "aria-label": "Delete", html: "&#10005;" });
    Friday.armed(del, async () => {
      try {
        await Friday.api(`/api/automations/${item.id}`, { method: "DELETE" });
        loadAutomations();
        changed();
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    }, { label: "?" });
    controls.append(sw, run, del);
    li.append(text, controls);
    return li;
  }

  // ---- lists -----------------------------------------------------------------
  async function loadLists() {
    try {
      const data = await Friday.api("/api/lists");
      const names = Object.keys(data).sort();
      if (!names.includes(currentList)) currentList = names[0] || null;
      listTabs.replaceChildren();
      for (const name of names) {
        const tab = Friday.el(
          "button",
          { class: `chip action ${name === currentList ? "active" : ""}`, type: "button" },
          name,
          Friday.el("span", { class: "count" }, String(data[name].length)),
        );
        tab.addEventListener("click", () => {
          currentList = name;
          localStorage.setItem("friday-list", name);
          loadLists();
        });
        listTabs.append(tab);
      }
      lists.replaceChildren();
      if (currentList) lists.append(renderList(currentList, data[currentList]));
      listsEmpty.hidden = names.length > 0;
      const open = names.reduce((sum, name) => sum + data[name].length, 0);
      stat("stat-lists").textContent = String(open);
      stat("stat-lists-sub").textContent = names.length ? `across ${names.length} list${names.length === 1 ? "" : "s"}` : "no lists";
    } catch (error) {
      Friday.toast(`Lists: ${error.message}`, "error");
    }
  }

  function renderList(name, items) {
    const section = Friday.el("section", { class: "list" });
    const ul = Friday.el("ul", { class: "list-items" });
    for (const item of items) {
      const li = Friday.el("li", { class: "list-item" }, Friday.el("span", {}, item));
      const done = Friday.el("button", { class: "icon-btn tiny", type: "button", "aria-label": `Done: ${item}`, html: "&#10003;" });
      done.addEventListener("click", async () => {
        try {
          await Friday.api(`/api/lists/${encodeURIComponent(name)}?item=${encodeURIComponent(item)}`, { method: "DELETE" });
          li.classList.add("gone");
          setTimeout(loadLists, 200);
        } catch (error) {
          Friday.toast(error.message, "error");
        }
      });
      li.append(done);
      ul.append(li);
    }
    if (!items.length) ul.append(Friday.el("li", { class: "list-item muted" }, "Empty."));
    const form = Friday.el(
      "form",
      { class: "inline-form small" },
      Friday.el("input", { type: "text", maxlength: "120", placeholder: `Add to ${name}…`, autocomplete: "off" }),
      Friday.el("button", { class: "primary", type: "submit" }, "Add"),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = form.querySelector("input");
      const value = input.value.trim();
      if (!value) return;
      try {
        await Friday.api(`/api/lists/${encodeURIComponent(name)}`, { method: "POST", body: { items: [value] } });
        input.value = "";
        loadLists();
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    });
    const clear = Friday.el("button", { class: "ghost tiny danger", type: "button" }, "Clear list");
    Friday.armed(clear, async () => {
      try {
        await Friday.api(`/api/lists/${encodeURIComponent(name)}`, { method: "DELETE" });
        loadLists();
      } catch (error) {
        Friday.toast(error.message, "error");
      }
    });
    section.append(ul, form, Friday.el("div", { class: "list-foot" }, clear));
    return section;
  }

  document.querySelector("#list-new-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.querySelector("#list-new-name");
    const name = input.value.trim().toLowerCase();
    if (!name) return;
    const first = prompt(`First item for "${name}"?`);
    if (!first) return;
    try {
      await Friday.api(`/api/lists/${encodeURIComponent(name)}`, { method: "POST", body: { items: [first] } });
      input.value = "";
      currentList = name;
      loadLists();
    } catch (error) {
      Friday.toast(error.message, "error");
    }
  });

  function loadAll() {
    loadCalendar();
    loadAutomations();
    loadLists();
  }
  document.querySelector("#home-refresh").addEventListener("click", loadAll);
  Friday.on("view", (v) => {
    if (v === "home") loadAll();
  });
  if (Friday.view === "home") loadAll();
})();
