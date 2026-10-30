// Shared bits for every section of the app: the section switcher, a JSON
// fetch that turns server errors into messages, toasts, tap-twice-to-confirm,
// and the slide controls the rest of the pages are built from.
window.Friday = (() => {
  const listeners = {};
  let current = document.body.dataset.view || "chat";

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escape = (text) => String(text ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

  async function api(path, { method = "GET", body, signal } = {}) {
    const options = { method, signal, headers: {} };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail;
      throw new Error(
        typeof detail === "string" ? detail : detail?.[0]?.msg || `Request failed (${response.status})`,
      );
    }
    return data;
  }

  // ---- toasts --------------------------------------------------------------
  const toasts = document.querySelector("#toasts");

  function toast(text, kind = "info", ms = 3200) {
    const item = document.createElement("div");
    item.className = `toast ${kind}`;
    item.textContent = text;
    toasts.append(item);
    requestAnimationFrame(() => item.classList.add("in"));
    setTimeout(() => {
      item.classList.remove("in");
      setTimeout(() => item.remove(), 300);
    }, ms);
    return item;
  }

  // ---- sections ------------------------------------------------------------
  function on(event, handler) {
    (listeners[event] ||= []).push(handler);
  }

  function emit(event, ...args) {
    for (const handler of listeners[event] || []) {
      try {
        handler(...args);
      } catch (error) {
        console.warn(`${event} handler failed`, error);
      }
    }
  }

  const MORE_VIEWS = ["home", "people", "settings"];
  const moreTab = document.querySelector("#nav-more");
  const moreSheet = document.querySelector("#more-sheet");
  const moreScrim = document.querySelector("#more-scrim");

  function setMoreOpen(open) {
    if (!moreSheet) return;
    moreSheet.hidden = !open;
    moreScrim.hidden = !open;
    moreTab.setAttribute("aria-expanded", String(open));
  }
  moreTab?.addEventListener("click", () => setMoreOpen(moreSheet.hidden));
  moreScrim?.addEventListener("click", () => setMoreOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && moreSheet && !moreSheet.hidden) setMoreOpen(false);
  });

  function navigate(view, { push = true } = {}) {
    if (!document.querySelector(`.view[data-view="${view}"]`)) view = "chat";
    const previous = current;
    current = view;
    document.body.dataset.view = view;
    for (const section of document.querySelectorAll(".view")) {
      section.hidden = section.dataset.view !== view;
    }
    for (const tab of document.querySelectorAll(".tab[data-view]")) {
      tab.setAttribute("aria-selected", String(tab.dataset.view === view));
    }
    // On a phone, the pages without a tab of their own live under More.
    moreTab?.setAttribute("aria-selected", String(MORE_VIEWS.includes(view)));
    setMoreOpen(false);
    if (push) {
      try {
        const url = view === "chat" ? location.pathname : `#${view}`;
        history.replaceState(null, "", url);
      } catch {}
    }
    if (previous !== view) emit("leave", previous);
    emit("view", view, previous);
  }

  for (const tab of document.querySelectorAll(".tab[data-view]")) {
    tab.addEventListener("click", () => navigate(tab.dataset.view));
  }
  // Anything marked data-go is a link to a section.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-go]");
    if (link) navigate(link.dataset.go);
  });
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1) || "chat", { push: false }));

  // ---- confirm by tapping twice -------------------------------------------
  // Destructive buttons ask once, in place: the label turns into "Sure?" for
  // a few seconds and a second tap does it. No modal, no accidental deletes.
  function armed(button, action, { label = "Sure?", ms = 3500 } = {}) {
    let timer = null;
    const original = button.innerHTML;
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!button.classList.contains("armed")) {
        button.classList.add("armed");
        button.dataset.original = original;
        button.textContent = label;
        timer = setTimeout(() => {
          button.classList.remove("armed");
          button.innerHTML = original;
        }, ms);
        return;
      }
      clearTimeout(timer);
      button.classList.remove("armed");
      button.innerHTML = original;
      button.disabled = true;
      try {
        await action();
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  // ---- sliders ---------------------------------------------------------------
  // A track you slide along. Pointer events only, so it behaves the same with
  // a finger, a mouse, or a pen; `touch-action: none` on the track keeps the
  // page from scrolling under a drag. onInput fires while sliding (throttled
  // by the caller if needed), onChange once on release.
  function slider(host, { min = 0, max = 100, value = 0, step = 1, onInput, onChange, format, label, track } = {}) {
    host.classList.add("slider");
    host.setAttribute("role", "slider");
    host.setAttribute("tabindex", "0");
    host.setAttribute("aria-valuemin", String(min));
    host.setAttribute("aria-valuemax", String(max));
    if (label) host.setAttribute("aria-label", label);
    if (track) host.style.setProperty("--track", track);
    host.innerHTML =
      '<div class="slider-fill"></div><div class="slider-thumb"></div>' +
      (format ? '<span class="slider-value"></span>' : "");
    const fill = host.querySelector(".slider-fill");
    const thumb = host.querySelector(".slider-thumb");
    const readout = host.querySelector(".slider-value");
    let current = clamp(value);
    let dragging = false;

    function clamp(v) {
      const stepped = Math.round((v - min) / step) * step + min;
      return Math.max(min, Math.min(max, stepped));
    }

    function paint() {
      const ratio = (current - min) / (max - min || 1);
      fill.style.width = `${ratio * 100}%`;
      thumb.style.left = `${ratio * 100}%`;
      host.setAttribute("aria-valuenow", String(current));
      if (readout) readout.textContent = format(current);
    }

    function fromEvent(event) {
      const rect = host.getBoundingClientRect();
      const ratio = (event.clientX - rect.left) / (rect.width || 1);
      return clamp(min + Math.max(0, Math.min(1, ratio)) * (max - min));
    }

    host.addEventListener("pointerdown", (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();
      host.setPointerCapture(event.pointerId);
      dragging = true;
      host.classList.add("dragging");
      current = fromEvent(event);
      paint();
      onInput?.(current);
    });
    host.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const next = fromEvent(event);
      if (next === current) return;
      current = next;
      paint();
      onInput?.(current);
    });
    const finish = (event) => {
      if (!dragging) return;
      dragging = false;
      host.classList.remove("dragging");
      try {
        host.releasePointerCapture(event.pointerId);
      } catch {}
      onChange?.(current);
    };
    host.addEventListener("pointerup", finish);
    host.addEventListener("pointercancel", finish);
    host.addEventListener("keydown", (event) => {
      const delta = { ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step }[event.key];
      if (!delta) return;
      event.preventDefault();
      current = clamp(current + delta * (event.shiftKey ? 10 : 1));
      paint();
      onInput?.(current);
      onChange?.(current);
    });
    paint();
    return {
      el: host,
      get value() {
        return current;
      },
      set(v) {
        if (dragging) return;
        current = clamp(v);
        paint();
      },
      get dragging() {
        return dragging;
      },
    };
  }

  // A toggle switch you can tap or slide.
  function toggle(host, { checked = false, onChange, label } = {}) {
    host.classList.add("switch");
    host.setAttribute("role", "switch");
    host.setAttribute("tabindex", "0");
    if (label) host.setAttribute("aria-label", label);
    host.innerHTML = '<span class="switch-knob"></span>';
    let state = Boolean(checked);
    let startX = 0;
    let moved = false;
    const paint = () => host.setAttribute("aria-checked", String(state));
    const setState = (next) => {
      if (next === state) return;
      state = next;
      paint();
      onChange?.(state);
    };
    host.addEventListener("pointerdown", (event) => {
      startX = event.clientX;
      moved = false;
      host.setPointerCapture(event.pointerId);
    });
    host.addEventListener("pointermove", (event) => {
      if (!host.hasPointerCapture(event.pointerId)) return;
      const dx = event.clientX - startX;
      if (Math.abs(dx) > 14) {
        moved = true;
        setState(dx > 0);
      }
    });
    host.addEventListener("pointerup", (event) => {
      try {
        host.releasePointerCapture(event.pointerId);
      } catch {}
      if (!moved) setState(!state);
    });
    host.addEventListener("keydown", (event) => {
      if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        setState(!state);
      }
    });
    paint();
    return {
      el: host,
      get checked() {
        return state;
      },
      set(v) {
        state = Boolean(v);
        paint();
      },
    };
  }

  // A round dial: a 270° arc you drag around, ticks lighting up as it
  // fills, the value large in the middle. Same contract as slider().
  function dial(host, { min = 0, max = 100, value = 0, step = 1, onInput, onChange, format = String, label, caption = "" } = {}) {
    const NS = "http://www.w3.org/2000/svg";
    const SIZE = 240;
    const C = SIZE / 2;
    const R = 92;
    const START = 135; // degrees, clockwise from +x
    const SWEEP = 270;
    const TICKS = 54;
    host.classList.add("dial");
    host.setAttribute("role", "slider");
    host.setAttribute("tabindex", "0");
    host.setAttribute("aria-valuemin", String(min));
    host.setAttribute("aria-valuemax", String(max));
    if (label) host.setAttribute("aria-label", label);
    const point = (deg, r) => {
      const rad = (deg * Math.PI) / 180;
      return [C + r * Math.cos(rad), C + r * Math.sin(rad)];
    };
    const arcPath = (from, to) => {
      const [x1, y1] = point(from, R);
      const [x2, y2] = point(to, R);
      return `M ${x1} ${y1} A ${R} ${R} 0 ${to - from > 180 ? 1 : 0} 1 ${x2} ${y2}`;
    };
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${SIZE} ${SIZE}`);
    svg.setAttribute("aria-hidden", "true");
    const ticks = [];
    for (let i = 0; i < TICKS; i += 1) {
      const deg = START + (SWEEP * i) / (TICKS - 1);
      const [x1, y1] = point(deg, R + 14);
      const [x2, y2] = point(deg, R + (i % 9 === 0 ? 24 : 20));
      const line = document.createElementNS(NS, "line");
      Object.entries({ x1, y1, x2, y2, class: "dial-tick" }).forEach(([k, v]) => line.setAttribute(k, v));
      svg.append(line);
      ticks.push(line);
    }
    const track = document.createElementNS(NS, "path");
    track.setAttribute("class", "dial-track");
    track.setAttribute("d", arcPath(START, START + SWEEP));
    const fill = document.createElementNS(NS, "path");
    fill.setAttribute("class", "dial-fill");
    const knob = document.createElementNS(NS, "circle");
    knob.setAttribute("class", "dial-knob");
    knob.setAttribute("r", "11");
    svg.append(track, fill, knob);
    const readout = el("div", { class: "dial-readout" }, el("span", { class: "dial-value" }), el("span", { class: "dial-caption" }, caption));
    host.replaceChildren(svg, readout);
    const valueEl = readout.querySelector(".dial-value");
    let current = clamp(value);
    let dragging = false;

    function clamp(v) {
      const stepped = Math.round((v - min) / step) * step + min;
      return Math.max(min, Math.min(max, stepped));
    }

    function paint() {
      const ratio = (current - min) / (max - min || 1);
      const end = START + SWEEP * ratio;
      fill.setAttribute("d", ratio > 0.002 ? arcPath(START, end) : "");
      const [kx, ky] = point(end, R);
      knob.setAttribute("cx", kx);
      knob.setAttribute("cy", ky);
      const lit = Math.round(ratio * (TICKS - 1));
      ticks.forEach((tick, i) => tick.classList.toggle("lit", i <= lit && ratio > 0));
      valueEl.textContent = format(current);
      host.setAttribute("aria-valuenow", String(current));
    }

    function fromEvent(event) {
      const rect = svg.getBoundingClientRect();
      const x = ((event.clientX - rect.left) / rect.width) * SIZE - C;
      const y = ((event.clientY - rect.top) / rect.height) * SIZE - C;
      let deg = (Math.atan2(y, x) * 180) / Math.PI; // -180..180
      deg = (deg - START + 720) % 360; // 0..360 from the start of the arc
      if (deg > SWEEP) deg = deg - SWEEP < (360 - SWEEP) / 2 ? SWEEP : 0; // the gap: snap to the nearer end
      return clamp(min + (deg / SWEEP) * (max - min));
    }

    host.addEventListener("pointerdown", (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();
      host.setPointerCapture(event.pointerId);
      dragging = true;
      host.classList.add("dragging");
      current = fromEvent(event);
      paint();
      onInput?.(current);
    });
    host.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const next = fromEvent(event);
      // No jumping across the gap from 0 to 100 in one move.
      if (next === current || Math.abs(next - current) > (max - min) * 0.5) return;
      current = next;
      paint();
      onInput?.(current);
    });
    const finish = (event) => {
      if (!dragging) return;
      dragging = false;
      host.classList.remove("dragging");
      try {
        host.releasePointerCapture(event.pointerId);
      } catch {}
      onChange?.(current);
    };
    host.addEventListener("pointerup", finish);
    host.addEventListener("pointercancel", finish);
    host.addEventListener("keydown", (event) => {
      const delta = { ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step }[event.key];
      if (!delta) return;
      event.preventDefault();
      current = clamp(current + delta * (event.shiftKey ? 10 : 1));
      paint();
      onInput?.(current);
      onChange?.(current);
    });
    paint();
    return {
      el: host,
      get value() {
        return current;
      },
      set(v) {
        if (dragging) return;
        current = clamp(v);
        paint();
      },
      get dragging() {
        return dragging;
      },
    };
  }

  // ---- weather pictures ----------------------------------------------------
  // WMO weather codes, folded into the handful of pictures worth drawing.
  // Shared: the chat's glance column and the phone home screen both draw it.
  const SKY = {
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
    partly: '<path d="M8 4v1.5M3.8 6.2l1 1M2 10.5h1.5M12.3 6.2l-1 1"/><path d="M5.5 11.5a3.2 3.2 0 0 1 5.9-2"/><path d="M8.5 19.5h9a3.5 3.5 0 0 0 .3-7A5 5 0 0 0 8.2 13a3.3 3.3 0 0 0 .3 6.5z"/>',
    cloud: '<path d="M7 18.5h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 11.5a3.5 3.5 0 0 0 1 7z"/>',
    fog: '<path d="M4 9h16M3 13h18M5 17h14"/>',
    rain: '<path d="M7 14.5h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 7.5a3.5 3.5 0 0 0 1 7z"/><path d="M8 18l-1 2.5M12 18l-1 2.5M16 18l-1 2.5"/>',
    snow: '<path d="M7 14.5h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 7.5a3.5 3.5 0 0 0 1 7z"/><path d="M8 19h.01M12 18h.01M16 19h.01M10 21.5h.01M14 21.5h.01"/>',
    storm: '<path d="M7 14.5h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 7.5a3.5 3.5 0 0 0 1 7z"/><path d="M12.5 15l-2 3.5h3l-2 3.5"/>',
  };

  function skyIcon(code, daylight) {
    const c = Number(code);
    let name = "cloud";
    if (c <= 1) name = daylight === false ? "moon" : "sun";
    else if (c === 2) name = "partly";
    else if (c === 3) name = "cloud";
    else if (c === 45 || c === 48) name = "fog";
    else if ((c >= 71 && c <= 77) || c === 85 || c === 86) name = "snow";
    else if (c >= 95) name = "storm";
    else if (c >= 51) name = "rain";
    return `<svg viewBox="0 0 24 24">${SKY[name]}</svg>`;
  }

  function timeAgo(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return "just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    if (days < 7) return `${days}d ago`;
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function throttle(fn, ms) {
    let last = 0;
    let pending = null;
    let timer = null;
    return (...args) => {
      const now = Date.now();
      pending = args;
      if (now - last >= ms) {
        last = now;
        fn(...pending);
        pending = null;
      } else if (!timer) {
        timer = setTimeout(() => {
          timer = null;
          last = Date.now();
          if (pending) fn(...pending);
          pending = null;
        }, ms - (now - last));
      }
    };
  }

  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key === "html") node.innerHTML = value;
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  };

  return {
    api,
    toast,
    on,
    emit,
    navigate,
    armed,
    slider,
    dial,
    toggle,
    escape,
    timeAgo,
    skyIcon,
    throttle,
    el,
    get view() {
      return current;
    },
  };
})();

// Land on the section named in the URL hash, if any.
if (location.hash.length > 1) Friday.navigate(location.hash.slice(1), { push: false });
