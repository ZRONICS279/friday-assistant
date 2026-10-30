// Past chats: a drawer listing every conversation on this house, newest
// first. Tap one to pick it back up; swipe it left (or use the bin) to
// forget it, messages and all.
(() => {
  const drawer = document.querySelector("#history");
  const list = document.querySelector("#history-list");
  const empty = document.querySelector("#history-empty");
  const toggle = document.querySelector("#history-toggle");
  const close = document.querySelector("#history-close");
  const scrim = document.querySelector("#scrim");
  let loaded = false;

  function setOpen(open) {
    drawer.hidden = !open;
    scrim.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("drawer-open", open);
    if (open) load();
  }

  async function load() {
    try {
      const data = await Friday.api("/api/conversations?limit=200");
      render(data.conversations || []);
      loaded = true;
    } catch (error) {
      Friday.toast(`Could not load history: ${error.message}`, "error");
    }
  }

  function render(items) {
    list.replaceChildren();
    for (const item of items) list.append(renderItem(item));
    empty.hidden = items.length > 0;
    markCurrent();
  }

  function markCurrent() {
    const current = typeof conversationId === "string" ? conversationId : null;
    for (const li of list.children) li.classList.toggle("current", li.dataset.id === current);
  }

  function renderItem(item) {
    const li = Friday.el("li", { class: "history-item", dataset: { id: item.id } });
    const body = Friday.el(
      "div",
      { class: "history-body" },
      Friday.el("span", { class: "history-title" }, item.title),
      Friday.el(
        "span",
        { class: "history-meta" },
        Friday.timeAgo(item.updated_at),
        " · ",
        `${item.messages} message${item.messages === 1 ? "" : "s"}`,
        item.profile ? ` · ${item.profile}` : "",
      ),
    );
    const bin = Friday.el("button", {
      class: "icon-btn history-delete",
      type: "button",
      "aria-label": "Delete chat",
      html: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>',
    });
    Friday.armed(bin, () => remove(item.id, li), { label: "Delete?" });
    li.append(body, bin);

    // Swipe left to reveal the delete; tap the body to open.
    let startX = 0;
    let startY = 0;
    let dx = 0;
    let swiping = false;
    body.addEventListener("pointerdown", (event) => {
      startX = event.clientX;
      startY = event.clientY;
      dx = 0;
      swiping = false;
    });
    body.addEventListener("pointermove", (event) => {
      if (event.pointerType === "mouse" || !event.buttons) return;
      const movedX = event.clientX - startX;
      const movedY = event.clientY - startY;
      if (!swiping && Math.abs(movedX) > 12 && Math.abs(movedX) > Math.abs(movedY)) {
        swiping = true;
        body.setPointerCapture(event.pointerId);
      }
      if (!swiping) return;
      dx = Math.max(-96, Math.min(0, movedX));
      li.style.setProperty("--swipe", `${dx}px`);
    });
    const end = () => {
      if (!swiping) return;
      swiping = false;
      li.classList.toggle("revealed", dx < -48);
      li.style.setProperty("--swipe", dx < -48 ? "-84px" : "0px");
    };
    body.addEventListener("pointerup", (event) => {
      if (swiping) {
        end();
        return;
      }
      if (li.classList.contains("revealed")) {
        li.classList.remove("revealed");
        li.style.setProperty("--swipe", "0px");
        return;
      }
      if (Math.abs(event.clientX - startX) < 8) open(item.id);
    });
    body.addEventListener("pointercancel", end);
    return li;
  }

  async function open(id) {
    try {
      await openConversation(id);
      setOpen(false);
      Friday.navigate("chat");
    } catch (error) {
      Friday.toast(`Could not open that chat: ${error.message}`, "error");
    }
  }

  async function remove(id, li) {
    try {
      await Friday.api(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
      li.classList.add("gone");
      setTimeout(() => {
        li.remove();
        empty.hidden = list.children.length > 0;
      }, 220);
      if (typeof conversationId === "string" && conversationId === id) {
        // The chat on screen is gone: start clean.
        document.querySelector("#new-chat").click();
      }
      Friday.toast("Chat deleted.");
    } catch (error) {
      Friday.toast(`Could not delete: ${error.message}`, "error");
    }
  }

  toggle.addEventListener("click", () => setOpen(drawer.hidden));
  close.addEventListener("click", () => setOpen(false));
  scrim.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !drawer.hidden) setOpen(false);
  });
  Friday.on("conversation", markCurrent);
  Friday.on("turn", () => {
    if (!drawer.hidden) load();
  });
  Friday.on("view", (view) => {
    if (view !== "chat" && !drawer.hidden) setOpen(false);
  });
})();
