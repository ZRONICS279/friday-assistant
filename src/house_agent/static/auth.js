// The house's pairing code. Every API call carries it; it is typed once,
// from Friday's startup log, and kept in this browser. A 401 from any call
// brings the prompt back. Loaded before the rest so nothing fetches without it.
(() => {
  const TOKEN_KEY = "house-agent-token";
  let apiToken = "";
  try { apiToken = localStorage.getItem(TOKEN_KEY) || ""; } catch (error) { apiToken = ""; }
  const nativeFetch = window.fetch.bind(window);

  function askForPairingCode() {
    const panel = document.querySelector("#pairing");
    if (!panel || !panel.hidden) return;
    panel.hidden = false;
    const field = document.querySelector("#pairing-code");
    if (field) { field.value = ""; field.focus(); }
  }

  window.fetch = async (url, options = {}) => {
    const target = typeof url === "string" ? url : url.url;
    if (target.startsWith("/api/") && apiToken) {
      const headers = new Headers(options.headers || {});
      headers.set("Authorization", `Bearer ${apiToken}`);
      options = { ...options, headers };
    }
    const response = await nativeFetch(url, options);
    if (response.status === 401 && target.startsWith("/api/")) askForPairingCode();
    return response;
  };

  window.FridayAuth = {
    withToken(params) {
      if (apiToken) params.set("token", apiToken);
      return params;
    },
    askForPairingCode,
  };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelector("#pairing-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const code = document.querySelector("#pairing-code").value.trim();
      if (!code) return;
      const check = await nativeFetch("/api/health", { headers: { Authorization: `Bearer ${code}` } });
      const body = check.ok ? await check.json() : {};
      if (!body.paired) {
        document.querySelector("#pairing-error").hidden = false;
        return;
      }
      apiToken = code;
      try { localStorage.setItem(TOKEN_KEY, code); } catch (error) { /* private window: this session only */ }
      location.reload();
    });
  });
})();
