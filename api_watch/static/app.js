/* API Watch browser client. No secrets are stored outside the current page. */
(() => {
  "use strict";
  const API = "/api/v1";
  const root = document.querySelector("#app");
  const modal = document.querySelector("#modal");
  const state = {
    user: null,
    csrf: null,
    route: "overview",
    monitors: [],
    incidents: [],
    detail: null,
    range: "24h",
    detailTab: "overview",
    checkFilters: { trigger: "", outcome: "", config_version: "" },
    checkCursor: null,
    checkStack: [],
    checks: null,
    incidentFilter: "",
    incidentMonitor: "",
    incidentCursor: null,
    incidentStack: [],
    tokenSecret: null,
    notificationCursor: null,
    notificationStack: [],
    lastRefresh: null,
    loading: false,
  };
  let navigationId = 0;
  let lastFocus = null;
  let modalGeneration = 0;
  let authMode = "login";
  let resetToken = null;
  const numericSources = new WeakMap();
  const icons = {
    pulse: '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    monitor:
      '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4m-5-10h3l2-3 2 6 2-3h2"/>',
    incident:
      '<path d="M10.3 4.5a2 2 0 0 1 3.4 0l8 13.7a2 2 0 0 1-1.7 3H4a2 2 0 0 1-1.7-3zM12 9v4m0 4h.01"/>',
    settings:
      '<path d="m9.5 3-.6 2.1-2 .9-2-.5-2.4 4.1L4 11.2v2l-1.5 1.6 2.4 4.1 2-.5 2 .9.6 2.1h5l.6-2.1 2-.9 2 .5 2.4-4.1-1.5-1.6v-2l1.5-1.6-2.4-4.1-2 .5-2-.9L14.5 3z"/><circle cx="12" cy="12" r="3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    external:
      '<path d="M14 3h7v7m0-7-11 11M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
    book: '<path d="M4 3h13a2 2 0 0 1 2 2v16H6a2 2 0 0 1-2-2zm0 14h15M8 7h7m-7 4h5"/>',
    logout:
      '<path d="M9 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4m5-4 4-4-4-4m-6 4h12"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    play: '<path d="m8 4 12 8-12 8z"/>',
    refresh:
      '<path d="M20 7v5h-5M4 17v-5h5m-4-4a8 8 0 0 1 13-3l2 2M4 17l2 2a8 8 0 0 0 13-3"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    edit: '<path d="m15 4 5 5M4 20l5-1 12-12a2 2 0 0 0-5-5L4 14z"/>',
    trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    globe:
      '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
    chevron: '<path d="m9 5 7 7-7 7"/>',
    back: '<path d="M19 12H5m6-6-6 6 6 6"/>',
    telegram: '<path d="m21 3-4 18-6-5-4 3v-6L21 3 3 10l4 3m4 3 6-9"/>',
    key: '<circle cx="8" cy="8" r="5"/><path d="m12 12 9 9m-4-4 3-3m-6 0 3-3"/>',
    shield:
      '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
    copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
    layers: '<path d="m12 3 10 5-10 5L2 8zm-10 9 10 5 10-5M2 16l10 5 10-5"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/>',
  };
  const icon = (name) =>
    `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.info}</svg>`;
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  const attr = esc;
  const label = (value) =>
    ({
      unknown: "Awaiting data",
      healthy: "Healthy",
      unhealthy: "Down",
      up: "Healthy",
      down: "Down",
      degraded: "Degraded",
      suspected: "Suspected failure",
      recovering: "Recovering",
      active: "Active",
      paused: "Paused",
      open: "Open",
      resolved: "Resolved",
      aborted: "Aborted",
      success: "Success",
      failure: "Failure",
      blocked: "Blocked",
      interrupted: "Interrupted",
      cancelled: "Cancelled",
      skipped: "Skipped",
      pending: "Pending",
      sending: "Sending",
      sent: "Sent",
      failed: "Failed",
      suppressed: "Suppressed",
      running: "Running",
      queued: "Queued",
      finished: "Finished",
    })[value] || String(value || "Unknown").replaceAll("_", " ");
  const status = (value) =>
    `<span class="status ${attr(/^[a-z_]+$/.test(value || "") ? value : "unknown")}">${esc(label(value))}</span>`;
  const date = (value) => {
    if (!value) return "—";
    const d = new Date(value);
    return Number.isNaN(d.getTime())
      ? "—"
      : new Intl.DateTimeFormat("en-US", {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        }).format(d);
  };
  const fullDate = (value) =>
    value
      ? new Date(value).toLocaleString("en-US", { timeZoneName: "short" })
      : "—";
  const relative = (value) => {
    if (!value) return "No checks yet";
    const seconds = Math.max(
      0,
      Math.floor((Date.now() - new Date(value).getTime()) / 1000),
    );
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  };
  const ms = (value) =>
    value == null
      ? "—"
      : `${Number(value).toLocaleString("en-US", { maximumFractionDigits: 1 })} ms`;
  const percent = (value) =>
    value == null
      ? "—"
      : `${Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 })}%`;
  const interval = (seconds) =>
    seconds >= 3600 ? `${seconds / 3600}h` : `${seconds / 60}m`;
  const duration = (a, b) => {
    if (!a) return "—";
    let seconds = Math.max(
      0,
      Math.round(
        ((b ? new Date(b).getTime() : Date.now()) - new Date(a).getTime()) /
          1000,
      ),
    );
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    if (seconds < 86400)
      return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
    return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
  };
  const toLocalInput = (value) => {
    const time = new Date(value);
    return new Date(time.getTime() - time.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  };
  const hostname = (url) => {
    try {
      return new URL(url).hostname;
    } catch {
      return "Endpoint";
    }
  };
  function json(value, parent = null, key = null, depth = 0) {
    if (typeof value === "number")
      return (
        numericSources.get(parent)?.get(String(key)) || JSON.stringify(value)
      );
    if (!value || typeof value !== "object") return JSON.stringify(value);
    const entries = Array.isArray(value)
      ? value.map((v, i) => json(v, value, i, depth + 1))
      : Object.entries(value).map(
          ([k, v]) => JSON.stringify(k) + ": " + json(v, value, k, depth + 1),
        );
    if (!entries.length) return Array.isArray(value) ? "[]" : "{}";
    const padding = "  ".repeat(depth + 1),
      close = "  ".repeat(depth);
    return (
      (Array.isArray(value) ? "[" : "{") +
      "\n" +
      padding +
      entries.join(",\n" + padding) +
      "\n" +
      close +
      (Array.isArray(value) ? "]" : "}")
    );
  }
  function parseExact(text) {
    let supported = false;
    JSON.parse("0", (_key, value, context) => {
      supported = Boolean(context?.source);
      return value;
    });
    if (supported)
      return JSON.parse(text, function (key, value, context) {
        if (typeof value === "number" && context?.source) {
          if (!numericSources.has(this)) numericSources.set(this, new Map());
          numericSources.get(this).set(key, context.source);
        }
        return value;
      });
    // Older browsers get the same lossless form roundtrip by replacing numeric
    // tokens only outside JSON strings before the native parser validates them.
    const marker = "__aw_number_" + crypto.randomUUID().replaceAll("-", "");
    let output = "",
      inside = false,
      escaped = false;
    for (let i = 0; i < text.length; i++) {
      const char = text[i];
      if (inside) {
        output += char;
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') inside = false;
        continue;
      }
      if (char === '"') {
        inside = true;
        output += char;
        continue;
      }
      if (char === "-" || /[0-9]/.test(char)) {
        const match = text
          .slice(i)
          .match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
        if (match) {
          output +=
            "{" + JSON.stringify(marker) + ":" + JSON.stringify(match[0]) + "}";
          i += match[0].length - 1;
          continue;
        }
      }
      output += char;
    }
    return JSON.parse(output, function (key, value) {
      if (
        value &&
        typeof value === "object" &&
        Object.keys(value).length === 1 &&
        Object.hasOwn(value, marker)
      ) {
        if (!numericSources.has(this)) numericSources.set(this, new Map());
        numericSources.get(this).set(key, value[marker]);
        return Number(value[marker]);
      }
      return value;
    });
  }
  const resource = (id) => encodeURIComponent(id);
  const items = (data) => (Array.isArray(data) ? data : data?.items || []);
  const query = (object) =>
    new URLSearchParams(
      Object.entries(object).filter(([, v]) => v !== "" && v != null),
    ).toString();
  const empty = (title, copy, action = "", symbol = "monitor") =>
    `<div class="empty"><div class="empty-icon">${icon(symbol)}</div><h3>${esc(title)}</h3><p>${esc(copy)}</p>${action}</div>`;
  const newButton =
    '<button class="button primary" data-action="create-monitor">' +
    icon("plus") +
    " Create monitor</button>";
  const loading =
    '<div class="loading-row"><span class="spinner"></span> Loading your data…</div>';

  // Keep JSON literals entered in the form intact, including numbers outside
  // JavaScript's safe integer range. Validation parses; serialization uses text.
  class JsonLiteral {
    constructor(text, name) {
      try {
        JSON.parse(text);
      } catch {
        throw new Error(`${name} must be valid JSON.`);
      }
      this.text = text;
    }
  }
  function serialize(value, parent = null, key = null) {
    if (value instanceof JsonLiteral) return value.text;
    if (typeof value === "number")
      return (
        numericSources.get(parent)?.get(String(key)) || JSON.stringify(value)
      );
    if (Array.isArray(value))
      return "[" + value.map((v, i) => serialize(v, value, i)).join(",") + "]";
    if (value && typeof value === "object")
      return (
        "{" +
        Object.entries(value)
          .filter(([, v]) => v !== undefined)
          .map(([k, v]) => JSON.stringify(k) + ":" + serialize(v, value, k))
          .join(",") +
        "}"
      );
    return JSON.stringify(value);
  }
  async function api(
    path,
    { method = "GET", data, headers = {}, allowAnonymous = false } = {},
  ) {
    const allHeaders = { Accept: "application/json", ...headers };
    if (data !== undefined) allHeaders["Content-Type"] = "application/json";
    if (!["GET", "HEAD"].includes(method) && state.csrf)
      allHeaders["X-CSRF-Token"] = state.csrf;
    let response;
    try {
      response = await fetch(API + path, {
        method,
        headers: allHeaders,
        body: data === undefined ? undefined : serialize(data),
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch {
      throw new Error(
        "Cannot reach API Watch. Check your connection and try again.",
      );
    }
    const body =
      response.status === 204
        ? null
        : await response
            .text()
            .then((text) => parseExact(text))
            .catch(() => null);
    if (!response.ok) {
      const detail = body?.error || {};
      const error = new Error(
        detail.message ||
          (response.status === 429
            ? "Too many requests. Please wait and try again."
            : `The request could not be completed (${response.status}).`),
      );
      error.code = detail.code;
      error.details = detail.details;
      error.requestId = detail.request_id;
      error.status = response.status;
      error.retryAfter = response.headers.get("Retry-After");
      if (response.status === 401 && state.user && !allowAnonymous) {
        state.user = null;
        state.csrf = null;
        closeModal();
        renderAuth("login");
        toast("Your session expired. Please sign in again.", true);
      }
      throw error;
    }
    return body;
  }
  function errorMarkup(error) {
    let result = esc(error.message || error);
    if (Array.isArray(error.details) && error.details.length)
      result +=
        "<ul>" +
        error.details
          .map(
            (d) =>
              `<li>${esc(d.field || d.loc?.join(".") || "")}${d.field || d.loc ? ": " : ""}${esc(d.message || d.msg || d.code || "")}</li>`,
          )
          .join("") +
        "</ul>";
    if (error.retryAfter)
      result += `<div>Please retry in ${esc(error.retryAfter)} seconds.</div>`;
    if (error.requestId)
      result += `<div class="tiny muted">Request ${esc(error.requestId)}</div>`;
    return result;
  }
  function formError(form, error) {
    const target = form.querySelector("[data-form-error]");
    if (target) {
      target.innerHTML = errorMarkup(error);
      target.focus();
      target.scrollIntoView({ block: "nearest" });
    } else toast(error.message, true);
  }
  function toast(message, isError = false) {
    const node = document.createElement("div");
    node.className = "toast" + (isError ? " error" : "");
    node.setAttribute("role", isError ? "alert" : "status");
    node.innerHTML =
      icon(isError ? "incident" : "check") +
      `<span>${esc(message)}</span><button class="icon-button" aria-label="Dismiss notification">${icon("close")}</button>`;
    node.querySelector("button").addEventListener("click", () => node.remove());
    document.querySelector("#toast-region").append(node);
    setTimeout(() => node.remove(), isError ? 11000 : 6500);
  }
  async function busy(button, task) {
    if (button?.disabled) return;
    const original = button?.innerHTML;
    if (button) {
      button.disabled = true;
      button.innerHTML = '<span class="spinner"></span> Please wait';
    }
    try {
      return await task();
    } catch (error) {
      toast(error.message, true);
    } finally {
      if (button?.isConnected) {
        button.disabled = false;
        button.innerHTML = original;
      }
    }
  }
  function openModal(content, { wide = false } = {}) {
    modalGeneration++;
    lastFocus = document.activeElement;
    modal.className = wide ? "wide" : "";
    modal.innerHTML = content;
    if (!modal.open) modal.showModal();
    const focus =
      modal.querySelector("[autofocus]") || modal.querySelector("input,button");
    requestAnimationFrame(() => focus?.focus());
  }
  function closeModal() {
    modalGeneration++;
    if (modal.open) modal.close();
    modal.replaceChildren();
    state.tokenSecret = null;
    lastFocus?.focus?.();
  }
  modal.addEventListener("cancel", () => {
    modalGeneration++;
    state.tokenSecret = null;
  });
  modal.addEventListener("close", () => {
    modal.replaceChildren();
    state.tokenSecret = null;
  });
  function modalHeader(title, copy) {
    return `<div class="modal-head"><div><h2 id="modal-title">${esc(title)}</h2>${copy ? `<p>${esc(copy)}</p>` : ""}</div><button class="icon-button" data-action="close-modal" aria-label="Close dialog">${icon("close")}</button></div>`;
  }
  function confirmAction(
    title,
    copy,
    buttonLabel,
    action,
    { danger = true, password = false } = {},
  ) {
    openModal(
      modalHeader(title, copy) +
        `<form data-form="confirm"><div class="modal-body"><div class="form-error" data-form-error tabindex="-1" role="alert"></div>${password ? '<div class="field"><label for="confirm-password">Current password</label><input id="confirm-password" name="password" class="input" type="password" required autocomplete="current-password" autofocus></div>' : '<p class="small muted">This action applies only to your account.</p>'}</div><div class="modal-footer"><button type="button" class="button ghost" data-action="close-modal">Cancel</button><button type="submit" class="button ${danger ? "danger" : "primary"}">${esc(buttonLabel)}</button></div></form>`,
    );
    modal.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector("[type=submit]");
      if (button.disabled) return;
      button.disabled = true;
      try {
        await action(new FormData(form));
        closeModal();
      } catch (error) {
        formError(form, error);
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    });
  }
  function authLocation() {
    const hash = location.hash.slice(1);
    if (hash.startsWith("reset=")) {
      resetToken = decodeURIComponent(hash.slice(6));
      history.replaceState(
        null,
        "",
        location.pathname + location.search + "#reset",
      );
      return "reset";
    }
    return hash.split("?")[0];
  }
  function renderAuth(mode = "login") {
    authMode = mode;
    const titles = {
      login: ["Welcome back", "Sign in to see how your endpoints are doing."],
      register: [
        "Create your account",
        "Start keeping an eye on the services you depend on.",
      ],
      forgot: [
        "Reset your password",
        "We will send reset instructions if an account exists.",
      ],
      reset: [
        "Choose a new password",
        "Use the reset token from your password reset link.",
      ],
    };
    const [title, copy] = titles[mode];
    const $resetInput = `<div class="field"><label for="reset-token">Reset token</label><input id="reset-token" class="input mono" name="token" value="${attr(resetToken || "")}" required autocomplete="off" spellcheck="false" autofocus><small>Paste the token from the reset email. It can only be used once.</small></div>`;
    root.innerHTML = `<div class="auth-layout"><aside class="auth-story"><a class="brand" href="#login"><span class="brand-mark">${icon("pulse")}</span> API Watch</a><h1>A little peace of mind.<br><span>For every endpoint.</span></h1><p>Know when your services go down, understand what happened, and be the first to know when they recover.</p><div class="auth-pulse" aria-hidden="true"><svg viewBox="0 0 450 110"><path d="M0 56h94l12-9 12 9h45l11-16 13 35 19-66 22 92 16-45h38l10-12 11 12h147"/></svg></div><div class="auth-features"><span>${icon("clock")} Scheduled checks</span><span>${icon("incident")} Clear incidents</span><span>${icon("telegram")} Telegram alerts</span></div><footer>BUILT TO KEEP YOU INFORMED · API WATCH 1.0</footer></aside><main id="main" class="auth-main"><div class="auth-card"><div class="eyebrow">YOUR MONITORING WORKSPACE</div><h2>${title}</h2><p>${copy}</p><form data-form="auth"><div class="form-error" data-form-error tabindex="-1" role="alert"></div>${mode !== "reset" ? '<div class="field"><label for="auth-email">Email address</label><input class="input" id="auth-email" name="email" type="email" maxlength="254" placeholder="you@example.com" required autocomplete="email" autofocus></div>' : $resetInput}${mode !== "forgot" ? `<div class="field"><div class="auth-password-row"><label for="auth-password">${mode === "reset" ? "New password" : "Password"}</label>${mode === "login" ? '<a href="#forgot">Forgot password?</a>' : ""}</div><input id="auth-password" class="input" name="password" type="password" required ${mode !== "login" ? 'minlength="12" maxlength="128"' : ""} autocomplete="${mode === "login" ? "current-password" : "new-password"}" placeholder="${mode === "login" ? "Enter your password" : "At least 12 characters"}">${mode !== "login" ? "<small>12–128 characters. Use a unique password for this account.</small>" : ""}</div>` : ""}<button class="button primary" type="submit">${{ login: "Sign in", register: "Create account", forgot: "Send reset instructions", reset: "Reset password" }[mode]} ${icon("arrow")}</button></form><p class="auth-switch">${mode === "login" ? 'New to API Watch? <a href="#register">Create an account</a>' : mode === "register" ? 'Already have an account? <a href="#login">Sign in</a>' : '<a href="#login">Back to sign in</a>'}</p>${mode === "forgot" ? '<p class="auth-switch">Have a reset token? <a href="#reset">Choose a new password</a></p>' : ""}<a class="auth-docs" href="/docs" target="_blank" rel="noopener">${icon("book")} Explore the API documentation ${icon("external")}</a></div></main></div>`;
    document.title = `${title} — API Watch`;
  }
  async function submitAuth(form) {
    const data = new FormData(form);
    const button = form.querySelector("[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    form.querySelector("[data-form-error]").textContent = "";
    try {
      if (authMode === "register") {
        await api("/auth/register", {
          method: "POST",
          data: { email: data.get("email"), password: data.get("password") },
          allowAnonymous: true,
        });
        toast("Your account is ready. Sign in to get started.");
        location.hash = "login";
        renderAuth("login");
      } else if (authMode === "login") {
        const result = await api("/auth/login", {
          method: "POST",
          data: { email: data.get("email"), password: data.get("password") },
          allowAnonymous: true,
        });
        state.user = result.user;
        state.csrf = result.csrf_token;
        if (!state.csrf) state.csrf = (await api("/auth/csrf")).csrf_token;
        if (location.hash === "#overview") await navigate();
        else location.hash = "overview";
      } else if (authMode === "forgot") {
        await api("/auth/password/forgot", {
          method: "POST",
          data: { email: data.get("email") },
          allowAnonymous: true,
        });
        form.innerHTML = `<div class="notice">${icon("mail")}<div>If an account exists, reset instructions will be sent. Check your inbox and spam folder.</div></div><a href="#reset" class="button primary">Enter reset token ${icon("arrow")}</a>`;
      } else {
        await api("/auth/password/reset", {
          method: "POST",
          data: {
            token: data.get("token"),
            new_password: data.get("password"),
          },
          allowAnonymous: true,
        });
        resetToken = null;
        state.user = null;
        state.csrf = null;
        toast("Password reset. Sign in with your new password.");
        location.hash = "login";
        renderAuth("login");
      }
    } catch (error) {
      formError(form, error);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }
  function shell(route) {
    const active = route.startsWith("monitor/") ? "monitors" : route;
    const navigation = [
      ["overview", "Overview"],
      ["monitors", "Monitors"],
      ["incidents", "Incidents"],
      ["settings", "Settings"],
    ]
      .map(
        ([id, text]) =>
          `<a class="nav-link ${active === id ? "active" : ""}" href="#${id}" ${active === id ? 'aria-current="page"' : ""}>${text}${id === "monitors" ? `<span class="count" id="nav-monitor-count">${state.monitors.length}</span>` : ""}</a>`,
      )
      .join("");
    root.innerHTML = `<header class="site-header"><div class="site-header-inner"><a href="#overview" class="brand"><span class="brand-mark">${icon("pulse")}</span> API Watch</a><button class="icon-button mobile-menu" data-action="menu" aria-label="Toggle navigation" aria-controls="primary-navigation" aria-expanded="false">${icon("menu")}</button><nav class="nav-list" id="primary-navigation" aria-label="Main navigation">${navigation}</nav><div class="header-account"><a class="header-docs" href="/docs" target="_blank" rel="noopener">API docs ${icon("external")}</a><span class="profile-email" title="${attr(state.user.email)}">${esc(state.user.email)}</span><button class="icon-button" data-action="logout" aria-label="Sign out" title="Sign out">${icon("logout")}</button></div></div></header><main class="main" id="main" tabindex="-1">${loading}</main>`;
  }
  const footer = () =>
    `<footer class="page-footer"><span>API Watch · Your services, in sight.</span><span id="refresh-label">${state.lastRefresh ? "Updated " + new Date(state.lastRefresh).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "Updates every 15 seconds"} · <a href="/docs" target="_blank" rel="noopener">REST API ${icon("external")}</a></span></footer>`;
  function setMain(content) {
    const main = document.querySelector("#main");
    if (main) {
      main.innerHTML = content + footer();
      document
        .querySelector("#nav-monitor-count")
        ?.replaceChildren(
          document.createTextNode(String(state.monitors.length)),
        );
    }
  }
  function heading(title, copy, actions = newButton) {
    return `<div class="page-heading"><div><h1>${esc(title)}</h1><p>${esc(copy)}</p></div><div class="heading-actions">${actions}</div></div>`;
  }
  function metric(title, value, note, symbol = "pulse", className = "") {
    return `<div class="metric"><div class="metric-top">${esc(title)}${icon(symbol)}</div><div class="metric-value ${className}">${value}</div><div class="metric-note">${esc(note)}</div></div>`;
  }
  function monitorHealth(m) {
    return m.state === "paused" ? "paused" : m.health_status || "unknown";
  }
  function openIncidentMarker(m) {
    if (!m.open_incident_id) return "";
    return `<button class="incident-flag" data-action="incident-detail" data-id="${attr(m.open_incident_id)}" aria-label="View open incident for ${attr(m.name)}">${icon("incident")} Open incident</button>`;
  }
  function monitorRow(m) {
    return `<tr class="interactive"><td><a href="#monitor/${attr(m.id)}" class="monitor-title"><span class="monitor-icon">${icon("globe")}</span>${esc(m.name)}</a><p class="endpoint" title="${attr(m.url)}">${esc(m.url)}</p></td><td>${status(monitorHealth(m))}${openIncidentMarker(m)}${m.busy ? '<p class="tiny muted mt-4">Check in progress</p>' : ""}</td><td class="mono">${ms(m.last_response_time_ms ?? m.last_check?.response_time_ms)}</td><td class="muted" title="${attr(fullDate(m.last_checked_at))}">${relative(m.last_checked_at)}</td><td><span class="method">${esc(m.method)}</span> <span class="muted small">/ ${esc(interval(m.interval_seconds))}</span></td><td><div class="actions"><button class="icon-button" data-action="run-monitor" data-id="${attr(m.id)}" title="${m.state === "paused" ? "Resume to run a diagnostic check" : m.busy ? "A check is already in progress" : "Run diagnostic check"}" ${m.state === "paused" || m.busy ? "disabled" : ""} aria-label="Run diagnostic check for ${attr(m.name)}">${icon("play")}</button><button class="icon-button" data-action="toggle-monitor" data-id="${attr(m.id)}" title="${m.state === "paused" ? "Resume" : "Pause"}" aria-label="${m.state === "paused" ? "Resume" : "Pause"} ${attr(m.name)}">${icon(m.state === "paused" ? "play" : "pause")}</button><a class="icon-button" href="#monitor/${attr(m.id)}" aria-label="View ${attr(m.name)}">${icon("chevron")}</a></div></td></tr>`;
  }
  function monitorTable(list) {
    return `<div class="table-wrap"><table><thead><tr><th>Monitor</th><th>Status</th><th>Response time</th><th>Last checked</th><th>Schedule</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${list.map(monitorRow).join("")}</tbody></table></div>`;
  }
  function renderOverview() {
    const monitors = state.monitors;
    const open =
      state.openIncidents || state.incidents.filter((i) => i.status === "open");
    const healthy = monitors.filter(
      (m) =>
        m.state !== "paused" && ["healthy", "up"].includes(m.health_status),
    ).length;
    const paused = monitors.filter((m) => m.state === "paused").length;
    setMain(
      heading("Overview", "A clear view of the services you depend on.") +
        `<div class="metrics">${metric("Total monitors", String(monitors.length), "Across your workspace", "monitor")}${metric("Healthy endpoints", String(healthy), "Confirmed by scheduled checks", "check", "positive")}${metric("Open incidents", String(open.length), open.length ? "Needs your attention" : "No confirmed outages", "incident", open.length ? "negative" : "")}${metric("Paused monitors", String(paused), "Excluded from scheduled checks", "pause")}</div><div class="panel"><div class="panel-head"><div><h2>Your monitors <span class="inline-tag">${monitors.length}</span></h2><p>Latest scheduled observations, all in one place.</p></div><a class="button ghost small" href="#monitors">View all monitors ${icon("arrow")}</a></div>${monitors.length ? monitorTable(monitors.slice(0, 8)) : empty("Your first monitor starts here", "Add a website or API endpoint. We will check it on a schedule and keep a history of what happens.", newButton)}${monitors.length ? `<div class="table-footer"><span>${monitors.filter((m) => m.state === "active").length} active · ${paused} paused</span><span>Scheduled checks run in the background</span></div>` : ""}</div><div class="grid-two"><section class="panel"><div class="panel-head"><div><h2>Recent incidents</h2><p>Confirmed failures and recoveries.</p></div><a href="#incidents" class="button ghost small">View incidents ${icon("arrow")}</a></div>${
          state.incidents.length
            ? `<div class="activity-list">${state.incidents
                .slice(0, 4)
                .map(
                  (i) =>
                    `<div class="activity-item"><span class="activity-dot ${i.status === "open" ? "open" : ""}"></span><div class="activity-content"><h3><a class="incident-link" href="#monitor/${attr(i.monitor_id)}?tab=incidents">${esc(i.monitor_name || state.monitors.find((m) => m.id === i.monitor_id)?.name || "Deleted monitor")}</a> ${status(i.status)}</h3><p>${esc(i.reason || i.error_code || i.abort_reason || "Incident detected")} · ${esc(duration(i.started_at, i.resolved_at || i.ended_at))}</p></div><span class="activity-time">${date(i.detected_at || i.started_at || i.created_at)}</span></div>`,
                )
                .join("")}</div>`
            : empty(
                "All quiet for now",
                "Confirmed incidents will appear here. An empty history does not mean checks have already passed.",
                "",
                "check",
              )
        }</section><section class="panel"><div class="panel-head"><h2>Make it your watch</h2><span class="monitor-icon">${icon("telegram")}</span></div><div class="panel-body"><p class="small muted">Close the tab. Keep the peace of mind. Connect Telegram to get a message when a service goes down and when it recovers.</p><div class="notice notice-space">${icon("info")}<span>Alerts follow your failure and recovery thresholds. A single slow request does not need to become a noisy incident.</span></div><a href="#settings" class="button ghost">Set up Telegram ${icon("arrow")}</a></div></section></div>`,
    );
  }
  function renderMonitors() {
    setMain(
      heading(
        "Monitors",
        "Keep an eye on every website and API that matters.",
      ) +
        `<section class="panel"><div class="panel-head"><div><h2>All monitors <span class="inline-tag">${state.monitors.length} / 20</span></h2><p>Scheduled checks continue while you are away.</p></div></div><div class="toolbar"><label class="search">${icon("search")}<span class="sr-only">Search monitors</span><input class="input" type="search" id="monitor-search" placeholder="Search by name or endpoint…" value="${attr(state.monitorSearch || "")}"></label><label><span class="sr-only">Filter monitor status</span><select id="monitor-status" class="select"><option value="">All statuses</option><option value="active" ${state.monitorStatus === "active" ? "selected" : ""}>Active</option><option value="paused" ${state.monitorStatus === "paused" ? "selected" : ""}>Paused</option><option value="healthy" ${state.monitorStatus === "healthy" ? "selected" : ""}>Healthy</option><option value="unhealthy" ${state.monitorStatus === "unhealthy" ? "selected" : ""}>Down</option><option value="degraded" ${state.monitorStatus === "degraded" ? "selected" : ""}>Degraded</option><option value="recovering" ${state.monitorStatus === "recovering" ? "selected" : ""}>Recovering</option><option value="unknown" ${state.monitorStatus === "unknown" ? "selected" : ""}>Awaiting data</option></select></label></div><div id="monitor-results"></div></section>`,
    );
    renderMonitorResults();
  }
  function renderMonitorResults() {
    const term = (state.monitorSearch || "").toLowerCase();
    const filter =
      { healthy: "up", unhealthy: "down" }[state.monitorStatus] ||
      state.monitorStatus;
    const filtered = state.monitors.filter(
      (m) =>
        (!term || `${m.name} ${m.url}`.toLowerCase().includes(term)) &&
        (!filter ||
          (filter === "active" || filter === "paused"
            ? m.state === filter
            : m.state !== "paused" && m.health_status === filter)),
    );
    const target = document.querySelector("#monitor-results");
    if (target)
      target.innerHTML = filtered.length
        ? monitorTable(filtered) +
          `<div class="table-footer"><span>${filtered.length} monitor${filtered.length === 1 ? "" : "s"}</span><span>Public HTTP & HTTPS endpoints</span></div>`
        : state.monitors.length
          ? empty(
              "No matching monitors",
              "Try another search or choose a different status.",
              "",
              "search",
            )
          : empty(
              "Nothing to watch yet",
              "Add your first endpoint to start collecting response times, checks, and incidents.",
              newButton,
            );
  }
  function incidentTable(data, { paging = true } = {}) {
    const list = items(data);
    return list.length
      ? `<div class="table-wrap"><table><thead><tr><th>Monitor / incident</th><th>Status</th><th>Started</th><th>Ended</th><th>Duration</th><th>Reason</th></tr></thead><tbody>${list.map((i) => `<tr class="interactive"><td><button class="button link" data-action="incident-detail" data-id="${attr(i.id)}">${esc(i.monitor_name || state.monitors.find((m) => m.id === i.monitor_id)?.name || "Deleted monitor")}</button><p class="tiny muted mono mt-4">${esc(i.id.slice(0, 8))} · config v${esc(i.config_version)}</p></td><td>${status(i.status)}</td><td title="${attr(fullDate(i.started_at))}">${date(i.started_at)}</td><td title="${attr(fullDate(i.resolved_at || i.ended_at))}">${date(i.resolved_at || i.ended_at)}</td><td class="mono">${esc(duration(i.started_at, i.resolved_at || i.ended_at))}</td><td class="error-code">${esc(i.reason || i.error_code || i.abort_reason || "—")}</td></tr>`).join("")}</tbody></table></div>${paging ? `<div class="table-footer"><span>${list.length} incident${list.length === 1 ? "" : "s"} on this page</span><div class="actions"><button class="button ghost small" data-action="incidents-prev" ${state.incidentStack.length ? "" : "disabled"}>Previous</button><button class="button ghost small" data-action="incidents-next" ${data.has_more ? "" : "disabled"}>Next ${icon("arrow")}</button></div></div>` : ""}`
      : empty(
          "No incidents in this view",
          "Incidents open after the configured number of consecutive failures. Adjust the filters to explore your history.",
          "",
          "check",
        );
  }
  function renderIncidents() {
    setMain(
      heading(
        "Incidents",
        "Understand when your services needed attention.",
        '<button class="button ghost" data-action="refresh">' +
          icon("refresh") +
          " Refresh</button>",
      ) +
        `<section class="panel"><div class="panel-head"><div><h2>Incident history</h2><p>Pausing a monitor does not mark an incident as recovered.</p></div></div><div class="filters"><div class="field"><label for="incident-status">Status</label><select id="incident-status" class="select"><option value="">All statuses</option>${["open", "resolved", "aborted"].map((v) => `<option value="${v}" ${state.incidentFilter === v ? "selected" : ""}>${label(v)}</option>`).join("")}</select></div><div class="field"><label for="incident-monitor">Monitor</label><select id="incident-monitor" class="select"><option value="">All monitors</option>${state.monitors.map((m) => `<option value="${attr(m.id)}" ${state.incidentMonitor === m.id ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select></div>${incidentPeriodControls()}</div>${incidentCustomPeriod()}${incidentTable(state.incidentPage)}</section><div class="notice">${icon("info")}<span><strong>Open</strong> means a confirmed issue is still being observed. <strong>Resolved</strong> means recovery was confirmed. <strong>Aborted</strong> means monitoring changed or was removed before recovery could be confirmed.</span></div>`,
    );
  }
  function period() {
    const length =
      { "1h": 3600000, "24h": 86400000, "7d": 604800000, "30d": 2592000000 }[
        state.range
      ] || 86400000;
    const end = state.customTo ? new Date(state.customTo) : new Date();
    if (!state.periodBounds)
      state.periodBounds = {
        from: state.customFrom
          ? new Date(state.customFrom).toISOString()
          : new Date(end - length).toISOString(),
        to: end.toISOString(),
      };
    return {
      ...state.periodBounds,
      config_version: state.checkFilters.config_version || undefined,
    };
  }
  function incidentPeriod() {
    if (!state.incidentRange || state.incidentRange === "all") return {};
    if (!state.incidentBounds) {
      const length =
        { "1h": 3600000, "24h": 86400000, "7d": 604800000, "30d": 2592000000 }[
          state.incidentRange
        ] || 86400000;
      const end =
        state.incidentRange === "custom"
          ? new Date(state.incidentTo)
          : new Date();
      state.incidentBounds = {
        from:
          state.incidentRange === "custom"
            ? new Date(state.incidentFrom).toISOString()
            : new Date(end - length).toISOString(),
        to: end.toISOString(),
      };
    }
    return { ...state.incidentBounds };
  }
  function incidentPeriodControls() {
    return `<div class="field"><label for="incident-range">Period</label><select id="incident-range" class="select">${[
      ["all", "All retained history"],
      ["1h", "Last hour"],
      ["24h", "Last 24 hours"],
      ["7d", "Last 7 days"],
      ["30d", "Last 30 days"],
      ["custom", "Custom period"],
    ]
      .map(
        ([value, text]) =>
          `<option value="${value}" ${(state.incidentRange || "all") === value ? "selected" : ""}>${text}</option>`,
      )
      .join("")}</select></div>`;
  }
  function incidentCustomPeriod() {
    if (state.incidentRange !== "custom") return "";
    return `<form data-form="incident-period" class="range-custom incident-custom"><div class="field"><label for="incident-from">From (your timezone)</label><input id="incident-from" name="from" class="input" type="datetime-local" value="${attr(state.incidentFrom || "")}" required></div><div class="field"><label for="incident-to">To (your timezone)</label><input id="incident-to" name="to" class="input" type="datetime-local" value="${attr(state.incidentTo || "")}" required></div><button class="button" type="submit">Apply period</button></form>`;
  }
  async function applyIncidentFilters() {
    state.incidentFilter = document.querySelector("#incident-status").value;
    state.incidentMonitor = document.querySelector("#incident-monitor").value;
    state.incidentRange = document.querySelector("#incident-range").value;
    if (
      state.incidentRange === "custom" &&
      (!state.incidentFrom || !state.incidentTo)
    ) {
      state.incidentFrom = toLocalInput(Date.now() - 86400000);
      state.incidentTo = toLocalInput(Date.now());
    }
    state.incidentBounds = null;
    state.incidentCursor = null;
    state.incidentStack = [];
    await navigate({ refresh: true });
  }
  async function submitIncidentPeriod(form) {
    const data = new FormData(form);
    const start = new Date(data.get("from")),
      end = new Date(data.get("to"));
    if (!Number.isFinite(+start) || !Number.isFinite(+end) || start >= end) {
      toast("Choose a valid period with an end after its start.", true);
      return;
    }
    if (end - start > 2592000000) {
      toast("Choose a custom period of no more than 30 days.", true);
      return;
    }
    state.incidentFrom = data.get("from");
    state.incidentTo = data.get("to");
    state.incidentBounds = null;
    state.incidentCursor = null;
    state.incidentStack = [];
    await busy(form.querySelector("[type=submit]"), () =>
      navigate({ refresh: true }),
    );
  }
  function observationCoverage(stats) {
    return `<section class="coverage-summary" aria-label="Other scheduled outcomes"><div><h3>Other scheduled outcomes</h3><p>Excluded from the successful-check rate. Paused time creates no samples.</p></div><dl>${[
      ["Blocked", stats.blocked_count],
      ["Interrupted", stats.interrupted_count],
      ["Cancelled", stats.cancelled_count],
      ["Skipped slots", stats.skipped_slot_count],
    ]
      .map(
        ([title, value]) =>
          `<div><dt>${title}</dt><dd>${Number(value || 0).toLocaleString("en-US")}</dd></div>`,
      )
      .join("")}</dl></section>`;
  }
  function latencySummary(stats) {
    return `<dl class="latency-summary"><div><dt>Minimum</dt><dd>${ms(stats.min_response_time_ms)}</dd></div><div><dt>Median</dt><dd>${ms(stats.p50_response_time_ms)}</dd></div><div><dt>Maximum</dt><dd>${ms(stats.max_response_time_ms)}</dd></div><div><dt>Successful samples</dt><dd>${Number(stats.latency_sample_count || 0).toLocaleString("en-US")}</dd></div></dl>`;
  }
  function rangeSelect() {
    return `<label><span class="sr-only">Statistics period</span><select class="select" id="detail-range">${[
      ["1h", "Last hour"],
      ["24h", "Last 24 hours"],
      ["7d", "Last 7 days"],
      ["30d", "Last 30 days"],
      ["custom", "Custom period"],
    ]
      .map(
        ([v, t]) =>
          `<option value="${v}" ${state.range === v ? "selected" : ""}>${t}</option>`,
      )
      .join("")}</select></label>`;
  }
  function renderChart(series, key = "avg_response_time_ms", rate = false) {
    const data = items(series);
    const chartColor = rate ? "var(--green)" : "var(--accent)";
    const valid = data.filter(
      (b) => b[key] != null && Number.isFinite(Number(b[key])),
    );
    if (!valid.length)
      return empty(
        "No data for this period",
        rate
          ? "Scheduled successes and failures will form this chart. Manual checks and unevaluated outcomes are excluded."
          : "Response times appear after successful scheduled checks. Missing measurements stay empty.",
        "",
        "pulse",
      );
    // Match SVG coordinates to the visible panel so axis labels stay legible
    // on narrow screens instead of shrinking an 820px canvas into a phone.
    const panelWidth = document.querySelector("#main")?.clientWidth || 930;
    const width = Math.max(250, Math.min(1200, panelWidth - 110)),
      height = 185,
      left = 51,
      right = 15,
      top = 14,
      bottom = 27,
      w = width - left - right,
      h = height - top - bottom;
    const minTime = new Date(data[0].bucket_start).getTime();
    const endTime = new Date(
      data[data.length - 1].bucket_end || data[data.length - 1].bucket_start,
    ).getTime();
    const maxValue = rate
      ? 100
      : Math.max(1, ...valid.map((b) => Number(b[key]))) * 1.15;
    let segments = [],
      segment = [];
    data.forEach((b, index) => {
      if (b[key] == null) {
        if (segment.length) segments.push(segment);
        segment = [];
        return;
      }
      const time = new Date(b.bucket_start).getTime();
      const x =
        left +
        (endTime > minTime ? (time - minTime) / (endTime - minTime) : 0.5) * w;
      const y =
        top +
        h -
        (Math.min(maxValue, Math.max(0, Number(b[key]))) / maxValue) * h;
      segment.push({ x, y, b });
      if (index === data.length - 1 && segment.length) segments.push(segment);
    });
    if (segment.length && !segments.includes(segment)) segments.push(segment);
    const path = (points) =>
      points
        .map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)},${p.y.toFixed(2)}`)
        .join(" ");
    const axis = [0, 0.25, 0.5, 0.75, 1]
      .map((r) => {
        const y = top + h - r * h;
        return `<line class="grid-line" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"/><text x="${left - 10}" y="${y + 3}" text-anchor="end">${Math.round(r * maxValue)}${rate ? "%" : ""}</text>`;
      })
      .join("");
    const times = [0, 0.25, 0.5, 0.75, 1]
      .map(
        (r) =>
          `<text x="${left + r * w}" y="${height - 5}" text-anchor="${r === 0 ? "start" : r === 1 ? "end" : "middle"}">${esc(new Date(minTime + (endTime - minTime) * r).toLocaleString("en-US", { ...(endTime - minTime > 172800000 ? { month: "short", day: "numeric" } : { hour: "2-digit", minute: "2-digit" }) }))}</text>`,
      )
      .join("");
    return `<div class="chart-wrap"><svg class="chart ${rate ? "chart-rate" : "chart-latency"}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${rate ? "Successful scheduled checks as a percentage" : "Average response time of successful scheduled checks in milliseconds"}. Empty intervals have no connecting line."><defs><linearGradient id="${rate ? "rateFill" : "chartFill"}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${chartColor}" stop-opacity=".12"/><stop offset="100%" stop-color="${chartColor}" stop-opacity="0"/></linearGradient></defs>${axis}${segments
      .map(
        (points) =>
          `${points.length > 1 ? `<path d="${path(points)} L${points.at(-1).x},${top + h} L${points[0].x},${top + h} Z" fill="url(#${rate ? "rateFill" : "chartFill"})"/>` : ""}<path d="${path(points)}" class="line"/>${points
            .filter(
              (_, i) =>
                points.length < 30 || i % Math.ceil(points.length / 60) === 0,
            )
            .map(
              (p) =>
                `<circle cx="${p.x}" cy="${p.y}" r="2" fill="${chartColor}"><title>${esc(date(p.b.bucket_start))}: ${rate ? percent(p.b[key]) : ms(p.b[key])}</title></circle>`,
            )
            .join("")}`,
      )
      .join("")}${times}</svg></div>`;
  }
  function renderChecks() {
    const data = state.checks;
    const list = items(data);
    return `<div class="filters"><div class="field"><label for="check-trigger">Trigger</label><select id="check-trigger" class="select"><option value="">All checks</option>${["scheduled", "manual"].map((v) => `<option value="${v}" ${state.checkFilters.trigger === v ? "selected" : ""}>${v === "manual" ? "Manual diagnostics" : "Scheduled"}</option>`).join("")}</select></div><div class="field"><label for="check-outcome">Outcome</label><select id="check-outcome" class="select"><option value="">All outcomes</option>${["success", "failure", "blocked", "interrupted", "cancelled", "skipped"].map((v) => `<option value="${v}" ${state.checkFilters.outcome === v ? "selected" : ""}>${label(v)}</option>`).join("")}</select></div><div class="field"><label for="check-version">Configuration</label><select id="check-version" class="select"><option value="">All versions</option>${Array.from(
      { length: state.detail?.config_version || 1 },
      (_, i) => i + 1,
    )
      .reverse()
      .map(
        (v) =>
          `<option value="${v}" ${String(v) === state.checkFilters.config_version ? "selected" : ""}>Version ${v}</option>`,
      )
      .join(
        "",
      )}</select></div></div>${list.length ? `<div class="table-wrap"><table><thead><tr><th>Time / trigger</th><th>Outcome</th><th>HTTP</th><th>Response</th><th>Config</th><th>Details</th></tr></thead><tbody>${list.map((c) => `<tr class="interactive"><td><button class="button link" data-action="check-detail" data-id="${attr(c.id)}">${date(c.started_at || c.scheduled_at || c.created_at)}</button><p class="tiny muted mt-4">${esc(c.trigger)} check</p></td><td>${status(c.outcome || c.status)}</td><td class="mono">${esc(c.http_status ?? "—")}</td><td class="mono">${ms(c.response_time_ms)}</td><td class="muted">v${esc(c.config_version)}</td><td class="error-code">${esc(c.error_code || "—")}</td></tr>`).join("")}</tbody></table></div><div class="table-footer"><span>${list.length} checks on this page · times shown in your timezone</span><div class="actions"><button class="button ghost small" data-action="checks-prev" ${state.checkStack.length ? "" : "disabled"}>Previous</button><button class="button ghost small" data-action="checks-next" ${data.has_more ? "" : "disabled"}>Next ${icon("arrow")}</button></div></div>` : empty("No checks in this view", "A new monitor needs its first scheduled check. Try a wider period or clear the history filters.", "", "clock")}`;
  }
  function configView(m) {
    const rows = [
      ["Endpoint", m.url],
      ["Method", m.method],
      ["Interval", `${m.interval_seconds} seconds`],
      ["Timeout", ms(m.timeout_ms)],
      ["Expected HTTP status", m.expected_status],
      ["Maximum response time", ms(m.max_response_time_ms)],
      ["Failure threshold", `${m.failure_threshold} consecutive failures`],
      ["Recovery threshold", `${m.recovery_threshold} consecutive successes`],
      [
        "Authorization",
        m.auth?.type === "bearer"
          ? `Bearer · ${m.auth.configured ? "configured" : "not configured"}`
          : "None",
      ],
      ["Notifications", m.notifications_enabled ? "Enabled" : "Disabled"],
      ["Configuration version", `v${m.config_version}`],
      ["Record version", m.record_version],
    ];
    return `<div class="panel-body"><dl class="config-grid">${rows.map(([key, value]) => `<div class="config-item"><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>${m.description ? `<p class="small muted break my-18">${esc(m.description)}</p>` : ""}<div class="form-section"><h3>Public headers</h3><pre class="assertion-code">${esc(json(m.headers || []))}</pre><h3 class="mt-18">Secret headers</h3><p class="small muted mt-7">${m.secret_headers?.length ? m.secret_headers.map((h) => `${esc(h.name)} · configured`).join("<br>") : "No secret headers configured. Values are never returned by the API."}</p><h3 class="mt-18">Query parameters</h3><pre class="assertion-code">${esc(json(m.query_params || []))}</pre><h3 class="mt-18">JSON assertions</h3><pre class="assertion-code">${esc(json(m.json_assertions || []))}</pre>${m.body?.enabled ? `<h3 class="mt-18">JSON request body</h3><pre class="assertion-code">${esc(json(m.body.json, m.body, "json"))}</pre>` : ""}</div></div>`;
  }
  function renderDetail() {
    const m = state.detail;
    const s = state.stats || {};
    const versions = s.config_versions || s.configuration_versions || [];
    const tab = state.detailTab;
    setMain(
      `<a class="back-link" href="#monitors">${icon("back")} Back to monitors</a>` +
        heading(
          m.name,
          `${m.method} · ${m.url}`,
          `<button class="button ghost" data-action="edit-monitor" data-id="${attr(m.id)}">${icon("edit")} Edit</button><button class="button ghost" data-action="toggle-monitor" data-id="${attr(m.id)}">${icon(m.state === "paused" ? "play" : "pause")} ${m.state === "paused" ? "Resume" : "Pause"}</button><button class="button primary" data-action="run-monitor" data-id="${attr(m.id)}" ${m.state === "paused" || m.busy ? "disabled" : ""} title="${m.state === "paused" ? "Resume the monitor to run a diagnostic check" : m.busy ? "A check is already in progress" : "Run a diagnostic check"}">${icon("play")} Run check</button>`,
        ) +
        `<div class="actions detail-health">${status(monitorHealth(m))}${openIncidentMarker(m)}<span class="tiny muted">${m.state === "paused" ? "Scheduled checks paused · requests already sent may finish at the target" : `Checked ${relative(m.last_checked_at)}`} · config v${esc(m.config_version)}</span><button class="icon-button ml-auto" data-action="delete-monitor" data-id="${attr(m.id)}" aria-label="Delete monitor" title="Delete monitor">${icon("trash")}</button></div><div class="tabs" role="tablist" aria-label="Monitor views">${[
          ["overview", "Overview"],
          ["history", "Check history"],
          ["incidents", "Incidents"],
          ["configuration", "Configuration"],
        ]
          .map(
            ([v, t]) =>
              `<button class="tab ${tab === v ? "active" : ""}" role="tab" aria-selected="${tab === v}" data-action="detail-tab" data-tab="${v}">${t}</button>`,
          )
          .join(
            "",
          )}<span class="ml-auto">${rangeSelect()}</span></div>${state.range === "custom" ? `<form data-form="custom-period" class="range-custom"><div class="field"><label for="custom-from">From (your timezone)</label><input class="input" id="custom-from" name="from" type="datetime-local" value="${attr(state.customFrom || "")}" required></div><div class="field"><label for="custom-to">To (your timezone)</label><input class="input" id="custom-to" name="to" type="datetime-local" value="${attr(state.customTo || "")}" required></div><button class="button" type="submit">Apply period</button></form>` : ""}${m.state === "paused" ? `<div class="notice warning">${icon("pause")}<span>This monitor is paused. Its last observation remains visible, but no scheduled requests are sent. Resume to start collecting data again.</span></div>` : ""}${versions.length > 1 || versions.some((version) => Number(version) !== Number(m.config_version)) ? `<div class="notice warning">${icon("layers")}<span>This period includes checks from configuration${versions.length === 1 ? "" : "s"} ${versions.map((v) => "v" + esc(v)).join(", ")}; the current configuration is v${esc(m.config_version)}. Use the configuration filter in check history to inspect one version.</span></div>` : ""}${tab === "overview" ? `<div class="metrics">${metric("Successful checks", percent(s.success_rate_percent), `${s.success_count || 0} passed / ${s.evaluated_count || 0} evaluated`, "check", s.success_rate_percent == null ? "" : "positive")}${metric("Average response", ms(s.avg_response_time_ms), `${s.latency_sample_count || 0} successful samples`, "clock")}${metric("95th percentile", ms(s.p95_response_time_ms), "Successful scheduled checks only", "pulse")}${metric("Failed checks", String(s.failure_count || 0), `${s.skipped_count || s.skipped_slot_count || 0} skipped · ${s.blocked_count || 0} blocked`, "incident", s.failure_count ? "negative" : "")}</div>${observationCoverage(s)}<section class="panel"><div class="panel-head"><div><h2>Response time</h2><p>Average response time of successful scheduled checks · milliseconds</p></div><div class="chart-legend"><span><i></i>Successful checks</span></div></div><div class="panel-body">${renderChart(state.series)}${latencySummary(s)}</div></section><section class="panel"><div class="panel-head"><div><h2>Successful checks</h2><p>A sample of observed availability. Unmeasured time is not counted as uptime.</p></div></div><div class="panel-body">${renderChart(state.series, "success_rate_percent", true)}</div></section><section class="panel"><div class="panel-head"><h2>Latest checks</h2><button class="button ghost small" data-action="detail-tab" data-tab="history">View history ${icon("arrow")}</button></div>${renderChecks()}</section>` : tab === "history" ? `<section class="panel"><div class="panel-head"><div><h2>Check history</h2><p>Manual diagnostics do not affect incidents or availability statistics.</p></div></div>${renderChecks()}</section>` : tab === "incidents" ? `<section class="panel"><div class="panel-head"><h2>Incidents for this monitor</h2></div>${incidentTable(state.monitorIncidents)}</section>` : `<section class="panel"><div class="panel-head"><div><h2>Current configuration</h2><p>Pause the monitor before changing how requests are executed.</p></div><button class="button ghost small" data-action="edit-monitor" data-id="${attr(m.id)}">${icon("edit")} Edit settings</button></div>${configView(m)}</section>`}`,
    );
  }
  async function loadDetail(id) {
    const monitor = await api(`/monitors/${resource(id)}`);
    const p = period();
    const [stats, series, checks, incidents] = await Promise.all([
      api(`/monitors/${resource(id)}/stats?` + query(p)),
      api(
        `/monitors/${resource(id)}/stats/series?` +
          query({
            ...p,
            bucket:
              state.range === "1h"
                ? "1m"
                : state.range === "24h"
                  ? "5m"
                  : state.range === "7d"
                    ? "1h"
                    : undefined,
          }),
      ),
      api(
        `/monitors/${resource(id)}/checks?` +
          query({
            ...p,
            ...state.checkFilters,
            cursor: state.checkCursor,
            limit: 20,
          }),
      ),
      api(
        "/incidents?" +
          query({
            monitor_id: id,
            from: p.from,
            to: p.to,
            cursor: state.incidentCursor,
            limit: 20,
          }),
      ),
    ]);
    return { monitor, stats, series, checks, incidents };
  }
  async function navigate({ refresh = false } = {}) {
    if (!state.user) return;
    let route = location.hash.slice(1) || "overview";
    if (["login", "register", "forgot", "reset"].includes(route))
      route = "overview";
    const [path, search = ""] = route.split("?");
    const changed = state.route !== path;
    state.route = path;
    if (changed) {
      state.checkCursor = null;
      state.checkStack = [];
      state.incidentCursor = null;
      state.incidentStack = [];
      state.incidentBounds = null;
      state.notificationCursor = null;
      state.notificationStack = [];
      state.tokenCursor = null;
      state.tokenStack = [];
      state.checkFilters = { trigger: "", outcome: "", config_version: "" };
      state.customFrom = null;
      state.customTo = null;
      state.range = "24h";
      state.periodBounds = null;
      state.detailTab = new URLSearchParams(search).get("tab") || "overview";
    }
    if (refresh && !state.checkCursor && !state.incidentCursor)
      state.periodBounds = null;
    if (!refresh && search) {
      const requestedTab = new URLSearchParams(search).get("tab");
      if (requestedTab) state.detailTab = requestedTab;
    }
    if (refresh && path === "incidents" && !state.incidentCursor)
      state.incidentBounds = null;
    const id = ++navigationId;
    if (!refresh || !document.querySelector(".site-header")) shell(path);
    state.loading = true;
    document.title = `${path.startsWith("monitor/") ? "Monitor" : { overview: "Overview", monitors: "Monitors", incidents: "Incidents", settings: "Settings" }[path] || "Overview"} — API Watch`;
    try {
      if (path.startsWith("monitor/")) {
        const monitorId = path.slice(8);
        const [result, monitorList] = await Promise.all([
          loadDetail(monitorId),
          api("/monitors?limit=100"),
        ]);
        if (id !== navigationId) return;
        state.monitors = items(monitorList);
        state.detail = result.monitor;
        state.stats = result.stats;
        state.series = result.series;
        state.checks = result.checks;
        state.monitorIncidents = result.incidents;
        const existing = state.monitors.findIndex(
          (m) => m.id === state.detail.id,
        );
        if (existing >= 0) state.monitors[existing] = state.detail;
        document.title = `${state.detail.name} — API Watch`;
        state.lastRefresh = Date.now();
        renderDetail();
      } else if (path === "settings") {
        const [telegram, tokens, notifications, monitorList] =
          await Promise.all([
            api("/integrations/telegram"),
            api(
              "/api-tokens?" + query({ limit: 20, cursor: state.tokenCursor }),
            ),
            api(
              "/notifications?" +
                query({ limit: 10, cursor: state.notificationCursor }),
            ),
            api("/monitors?limit=100"),
          ]);
        if (id !== navigationId) return;
        state.monitors = items(monitorList);
        state.telegram = telegram;
        state.tokens = tokens;
        state.notifications = notifications;
        state.lastRefresh = Date.now();
        renderSettings();
      } else {
        const [monitors, incidents, openIncidents] = await Promise.all([
          api("/monitors?limit=100"),
          api(
            "/incidents?" +
              query({
                ...(path === "incidents" ? incidentPeriod() : {}),
                limit: path === "incidents" ? 20 : 100,
                status: path === "incidents" ? state.incidentFilter : undefined,
                monitor_id:
                  path === "incidents" ? state.incidentMonitor : undefined,
                cursor: path === "incidents" ? state.incidentCursor : undefined,
              }),
          ),
          path === "overview"
            ? api("/incidents?status=open&limit=100")
            : Promise.resolve(null),
        ]);
        if (id !== navigationId) return;
        if (openIncidents) state.openIncidents = items(openIncidents);
        state.monitors = items(monitors);
        state.incidents = items(incidents);
        state.incidentPage = incidents;
        state.lastRefresh = Date.now();
        if (path === "incidents") renderIncidents();
        else if (path === "monitors") renderMonitors();
        else renderOverview();
      }
    } catch (error) {
      if (id !== navigationId || !state.user) return;
      if (refresh) {
        toast(error.message, true);
      } else
        setMain(
          heading(
            "Something needs attention",
            "We could not load this view.",
            '<button class="button" data-action="refresh">' +
              icon("refresh") +
              " Try again</button>",
          ) +
            `<div class="notice error">${icon("incident")}<div>${errorMarkup(error)}</div></div>`,
        );
    } finally {
      if (id === navigationId) state.loading = false;
    }
  }

  function renderSettings() {
    const tg = state.telegram;
    const tokens = items(state.tokens);
    const notifications = items(state.notifications);
    setMain(
      heading(
        "Settings",
        "Connect your alerts, manage API access, and keep your account secure.",
        "",
      ) +
        `<div class="settings-grid"><section class="panel"><div class="panel-head"><div class="integration-header"><span class="integration-icon">${icon("telegram")}</span><div><h2>Telegram</h2><p>Know when your services need you.</p></div></div>${status(tg.connected ? "sent" : tg.configured ? "unknown" : "paused")}</div><div class="panel-body"><div class="connection-state"><h3>${tg.connected ? "Connected to your private chat" : tg.configured ? "No chat connected" : "Integration unavailable"}</h3><p>${tg.connected ? `Connected ${esc(date(tg.connected_at))}. Enable notifications on each monitor to receive incident alerts.` : tg.configured ? "Generate a one-time link, open it in Telegram, then press Start to confirm your private chat." : "Telegram is not configured on this deployment. Contact the operator to enable it. Monitoring continues independently."}</p></div><div class="actions"><button class="button ${tg.connected ? "ghost" : "primary"}" data-action="telegram-link" ${tg.configured ? "" : "disabled"}>${icon("telegram")} ${tg.connected ? "Reconnect Telegram" : "Connect Telegram"}</button>${tg.connected ? `<button class="button ghost" data-action="telegram-test">Send test</button><button class="button ghost" data-action="telegram-disconnect">Disconnect</button>` : ""}</div></div></section><section class="panel"><div class="panel-head"><div><h2>Your account</h2><p>Personal workspace and sign-in security.</p></div><span class="monitor-icon">${icon("shield")}</span></div><div class="panel-body"><p class="account-email">${esc(state.user.email)}</p><p class="small muted mb-20">Your email is a sign-in identifier and has not been verified.</p><button class="button ghost" data-action="password-change">${icon("key")} Change password</button><p class="field-note mt-13">Changing your password signs out all sessions and revokes API tokens.</p></div></section><section class="panel full"><div class="panel-head"><div><h2>API tokens <span class="inline-tag">5 active tokens max</span></h2><p>Use your own REST API with scoped, revocable access.</p></div><button class="button primary small" data-action="token-create">${icon("plus")} Create token</button></div>${tokens.length ? `<div class="table-wrap"><table><thead><tr><th>Name / prefix</th><th>Permission</th><th>Expires</th><th>Last used</th><th>Status</th><th></th></tr></thead><tbody>${tokens.map((t) => `<tr><td>${esc(t.name)}<p class="tiny mono muted mt-4">${esc(t.prefix)}…</p></td><td>${t.scope === "read_write" ? "Read & write" : "Read only"}</td><td>${date(t.expires_at)}</td><td>${date(t.last_used_at)}</td><td>${t.revoked_at ? '<span class="status">Revoked</span>' : new Date(t.expires_at) <= new Date() ? '<span class="status">Expired</span>' : '<span class="status success">Active</span>'}</td><td><button class="button ghost small" data-action="token-revoke" data-id="${attr(t.id)}" ${t.revoked_at ? "disabled" : ""}>Revoke</button></td></tr>`).join("")}</tbody></table></div>` : empty("Your API, your workflow", "Create a read-only token for reporting or a read-and-write token for managing monitors from your tools.", "", "key")}${tokenPagination()}<div class="panel-body pt-17 ${tokens.length ? "divider" : ""}"><p class="small muted">Tokens are shown once. Keep them in a secret manager and send them in the Authorization header.</p><pre class="api-example">curl '${esc(location.origin)}/api/v1/monitors' \
  --header 'Authorization: Bearer &lt;your-api-token&gt;'</pre><a class="button link small" href="/docs" target="_blank" rel="noopener">Explore the REST API ${icon("external")}</a></div></section><section class="panel full"><div class="panel-head"><div><h2>Notification delivery</h2><p>Delivery errors do not stop monitoring or incident detection.</p></div><button class="icon-button" data-action="refresh" title="Refresh deliveries" aria-label="Refresh deliveries">${icon("refresh")}</button></div>${notifications.length ? `<div class="table-wrap"><table><thead><tr><th>Event</th><th>Status</th><th>Attempts</th><th>Created</th><th>Details</th></tr></thead><tbody>${notifications.map((n) => `<tr><td>${esc(String(n.event_type || n.type || "Notification").replaceAll("_", " "))}</td><td>${status(n.status)}</td><td>${esc(n.attempts ?? 0)}</td><td>${date(n.created_at)}</td><td class="error-code">${esc(n.error_message || n.last_error || n.error_code || "—")}</td></tr>`).join("")}</tbody></table></div><div class="table-footer"><span>Latest deliveries</span><div class="actions"><button class="button ghost small" data-action="notifications-prev" ${state.notificationStack.length ? "" : "disabled"}>Previous</button><button class="button ghost small" data-action="notifications-next" ${state.notifications.has_more ? "" : "disabled"}>Next ${icon("arrow")}</button></div></div>` : empty("No deliveries yet", "Connect Telegram and send a test message. Incident notifications will appear here as they are queued.", "", "telegram")}</section><section class="panel full danger-zone"><div class="panel-head"><div><h2>Delete account</h2><p>Permanently remove your account, monitors, history, integrations, and saved credentials.</p></div><button class="button danger small" data-action="account-delete">Delete account</button></div></section></div>`,
    );
  }
  function tokenPagination() {
    if (!state.tokens?.has_more && !state.tokenStack?.length) return "";
    return `<div class="table-footer"><span>Token history</span><div class="actions"><button class="button ghost small" data-action="tokens-prev" ${state.tokenStack?.length ? "" : "disabled"}>Previous</button><button class="button ghost small" data-action="tokens-next" ${state.tokens.has_more ? "" : "disabled"}>Next ${icon("arrow")}</button></div></div>`;
  }
  function pairRow(pair = {}, kind = "headers") {
    const saved = kind === "secret_headers" && pair.configured;
    return `<div class="pair-row" ${saved ? 'data-saved="true"' : ""}><input class="input" data-pair-name value="${attr(pair.name || "")}" placeholder="${kind === "query_params" ? "Parameter" : "Header name"}" aria-label="${kind === "query_params" ? "Parameter" : "Header"} name" ${saved ? "readonly" : ""}><input class="input ${kind === "secret_headers" ? "mono" : ""}" data-pair-value value="${attr(saved ? "" : pair.value || "")}" type="${kind === "secret_headers" ? "password" : "text"}" autocomplete="${kind === "secret_headers" ? "new-password" : "off"}" placeholder="${saved ? "Saved · leave blank to keep" : "Value"}" aria-label="${saved ? "Replacement secret value; leave blank to keep" : "Value"}"><button type="button" class="icon-button" data-action="remove-pair" aria-label="Remove ${kind === "query_params" ? "parameter" : "header"}">${icon("close")}</button></div>`;
  }
  function assertionRow(assertion = {}) {
    return `<div class="assertion-row"><input class="input mono" data-assert-pointer value="${attr(assertion.pointer ?? "")}" placeholder="/data/status" aria-label="JSON Pointer"><select class="select" data-assert-operator aria-label="Assertion operator"><option value="exists" ${assertion.operator === "exists" ? "selected" : ""}>Exists</option><option value="equals" ${assertion.operator === "equals" || !assertion.operator ? "selected" : ""}>Equals</option></select><input class="input mono" data-assert-expected value="${attr(assertion.operator === "equals" ? json(assertion.expected, assertion, "expected") : assertion.operator === "exists" ? "" : '"ok"')}" placeholder="JSON value: &quot;ok&quot;" aria-label="Expected JSON value" ${assertion.operator === "exists" ? "disabled" : ""}><button type="button" class="icon-button" data-action="remove-assertion" aria-label="Remove assertion">${icon("close")}</button></div>`;
  }
  function pairSection(title, copy, kind, pairs = []) {
    return `<div class="field"><span class="field-label">${esc(title)}</span><p class="field-note pair-note">${esc(copy)}</p><div class="section-labels"><span>Name</span><span>${kind === "secret_headers" ? "Secret value" : "Value"}</span></div><div class="pair-list" data-pairs="${kind}">${pairs.map((p) => pairRow(p, kind)).join("")}</div><button type="button" class="button ghost small list-add" data-action="add-pair" data-kind="${kind}">${icon("plus")} Add ${kind === "query_params" ? "parameter" : "header"}</button></div>`;
  }
  function monitorForm(m = null) {
    const editing = !!m;
    const locked = editing && m.state !== "paused";
    const v = m || {
      method: "GET",
      url: "",
      name: "",
      description: "",
      interval_seconds: 60,
      timeout_ms: 10000,
      expected_status: 200,
      max_response_time_ms: null,
      failure_threshold: 3,
      recovery_threshold: 2,
      notifications_enabled: false,
      headers: [],
      secret_headers: [],
      query_params: [],
      json_assertions: [],
      auth: { type: "none" },
      body: { enabled: false },
      state: "active",
    };
    const bodyAllowed = !["GET", "HEAD", "OPTIONS"].includes(v.method);
    openModal(
      modalHeader(
        editing ? "Edit monitor" : "Create a monitor",
        editing
          ? "Keep your monitor settings aligned with your service."
          : "One endpoint. Clear expectations. Continuous monitoring.",
      ) +
        `<form data-form="monitor" autocomplete="off" data-id="${attr(m?.id || "")}"><div class="modal-body"><div class="form-error" data-form-error tabindex="-1" role="alert"></div>${locked ? `<div class="notice warning">${icon("pause")}<span>This monitor is active. You can update its name, description, and notifications. Pause it first to edit request settings and success conditions.</span></div>` : ""}<div class="form-section"><h3>The essentials</h3><p>Give your endpoint a name you will recognize in an alert.</p><div class="field"><label for="monitor-name">Monitor name</label><input id="monitor-name" class="input" name="name" autocomplete="off" value="${attr(v.name)}" placeholder="Production API" minlength="1" maxlength="100" required autofocus></div><div class="field"><label for="monitor-description">Description <span class="optional">(optional)</span></label><textarea class="input" id="monitor-description" name="description" autocomplete="off" maxlength="1000" rows="2" placeholder="What does this endpoint tell you?">${esc(v.description)}</textarea></div></div><fieldset class="execution-fields" ${locked ? "disabled" : ""}><legend class="sr-only">Request and execution configuration</legend><div class="form-section"><h3>HTTP request</h3><p>Public HTTP/HTTPS endpoints on their standard ports. Redirects are not followed.</p><div class="field-row url-row"><div class="field"><label for="monitor-method">Method</label><select class="input" id="monitor-method" name="method">${["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"].map((method) => `<option ${v.method === method ? "selected" : ""}>${method}</option>`).join("")}</select></div><div class="field"><label for="monitor-url">Endpoint URL</label><input id="monitor-url" class="input" name="url" type="url" autocomplete="url" value="${attr(v.url)}" placeholder="https://api.example.com/health" maxlength="2048" ${locked ? "" : "required"}><small>Pasted query parameters will be moved into the query parameter list when saved.</small></div></div><div id="side-effect-notice" ${bodyAllowed ? "" : "hidden"}><div class="notice warning">${icon("incident")}<span>This request runs repeatedly. Write methods may create, change, or delete data in the external service.</span></div><div class="field"><label class="check-label"><input type="checkbox" name="side_effects_acknowledged" ${v.side_effects_acknowledged ? "checked" : ""}> I understand this scheduled request may change data at the target.</label></div></div>${pairSection("Query parameters", "Order and repeated parameter names are preserved.", "query_params", v.query_params)}${pairSection("Public headers", "These values are visible in monitor settings. Put credentials in secret headers instead.", "headers", v.headers)}${pairSection("Secret headers", "Encrypted at rest. Saved values are never returned. A blank existing secret is preserved.", "secret_headers", v.secret_headers)}</div><div class="form-section"><h3>Authorization</h3><p>External API credentials are sent only to this monitor’s HTTPS target.</p><div class="field"><label for="monitor-auth">Authorization type</label><select id="monitor-auth" class="input" name="auth_type"><option value="none" ${v.auth?.type !== "bearer" ? "selected" : ""}>No built-in authorization</option><option value="bearer" ${v.auth?.type === "bearer" ? "selected" : ""}>Bearer token</option></select></div><div class="field" id="bearer-field" ${v.auth?.type === "bearer" ? "" : "hidden"}><label for="monitor-token">Bearer token ${v.auth?.configured ? '<span class="optional">(already configured)</span>' : ""}</label><input id="monitor-token" class="input mono" name="bearer_token" type="password" autocomplete="new-password" placeholder="${v.auth?.configured ? "Leave blank to keep the saved token" : "Enter external API token"}"><small>${v.auth?.configured ? "To replace the token, enter a new value. Switching to no authorization removes it." : "Requires an HTTPS endpoint. The token is never shown after saving."}</small></div></div><div class="form-section" id="body-section" ${bodyAllowed ? "" : "hidden"}><h3>Request body</h3><p>JSON only, up to 64 KiB. JSON null and no body are different.</p><div class="field"><label class="check-label"><input type="checkbox" name="body_enabled" id="body-enabled" ${v.body?.enabled ? "checked" : ""}> Include a JSON request body</label></div><div class="field" id="body-json-field" ${v.body?.enabled ? "" : "hidden"}><label for="body-json">JSON body</label><textarea id="body-json" class="input mono" name="body_json" spellcheck="false" rows="5" placeholder='{"example": true}'>${esc(v.body?.enabled ? json(v.body.json, v.body, "json") : "")}</textarea></div></div><div class="form-section"><h3>Schedule & expectations</h3><p>Every condition must pass for the check to succeed.</p><div class="field-row three"><div class="field"><label for="monitor-interval">Check interval (seconds)</label><input id="monitor-interval" name="interval_seconds" class="input" type="number" min="60" max="86400" step="1" value="${v.interval_seconds}" required></div><div class="field"><label for="monitor-timeout">Timeout (ms)</label><input id="monitor-timeout" name="timeout_ms" class="input" type="number" min="1000" max="30000" step="1" value="${v.timeout_ms}" required></div><div class="field"><label for="monitor-http-status">Expected HTTP status</label><input id="monitor-http-status" name="expected_status" class="input" type="number" min="200" max="599" step="1" value="${v.expected_status}" required></div></div><div class="field"><label for="monitor-response">Maximum response time (ms) <span class="optional">(optional)</span></label><input id="monitor-response" name="max_response_time_ms" class="input" type="number" min="1" max="30000" step="1" value="${v.max_response_time_ms ?? ""}" placeholder="Leave empty to use the timeout only"><small>A complete response slower than this value is a failed check.</small></div><div class="field"><span class="field-label">JSON assertions <span class="optional">(optional)</span></span><p class="field-note mb-10">Use JSON Pointer, for example /data/status or /items/0/id. Escape / as ~1 and ~ as ~0. An empty pointer selects the root. All assertions must pass.</p><div class="pair-list" id="assertion-list">${(v.json_assertions || []).map(assertionRow).join("")}</div><button class="button ghost small list-add" type="button" data-action="add-assertion">${icon("plus")} Add assertion</button><small>Expected values are JSON: use "ok" for a string, true for a boolean, or null. HEAD requests cannot have JSON assertions.</small></div></div><div class="form-section"><h3>Incident confirmation</h3><p>Confirm a sustained issue before opening an incident, and a stable recovery before closing it.</p><div class="field-row"><div class="field"><label for="failure-threshold">Failures to open an incident</label><input id="failure-threshold" name="failure_threshold" class="input" type="number" min="1" max="10" step="1" value="${v.failure_threshold}" required></div><div class="field"><label for="recovery-threshold">Successes to confirm recovery</label><input id="recovery-threshold" name="recovery_threshold" class="input" type="number" min="1" max="10" step="1" value="${v.recovery_threshold}" required></div></div></div></fieldset><div class="form-section"><h3>Notifications & state</h3><p>Incidents are recorded even if notifications are disabled.</p><div class="field"><label class="check-label"><input name="notifications_enabled" type="checkbox" ${v.notifications_enabled ? "checked" : ""}> Send incident and recovery notifications to my connected Telegram chat</label></div>${!editing ? '<label class="check-label"><input name="enabled" type="checkbox" checked> Start scheduled checks immediately</label>' : '<p class="field-note">Editing request settings on a paused monitor creates a new configuration version and aborts any open incident. Resume it when you are ready.</p>'}</div></div><div class="modal-footer"><span class="muted">${editing ? "Saved secrets stay hidden." : "You can pause or change this monitor later."}</span><button class="button ghost" data-action="close-modal" type="button">Cancel</button><button class="button primary" type="submit">${editing ? "Save changes" : "Create monitor"} ${icon("arrow")}</button></div></form>`,
      { wide: true },
    );
    modal.querySelector("form")._monitor = m;
  }
  function readPairs(form, kind) {
    return Array.from(
      form.querySelectorAll(`[data-pairs="${kind}"] .pair-row`),
    ).flatMap((row) => {
      const name = row.querySelector("[data-pair-name]").value.trim();
      const value = row.querySelector("[data-pair-value]").value;
      if (!name && !value) return [];
      if (!name)
        throw new Error("Every header and query parameter needs a name.");
      if (row.dataset.removed === "true") return [{ name, remove: true }];
      if (kind === "secret_headers" && row.dataset.saved === "true" && !value)
        return [];
      return [{ name, value }];
    });
  }
  function pruneUnchangedExecution(payload, monitor) {
    const canonical = (value) => serialize(parseExact(serialize({ value })));
    for (const key of Object.keys(payload)) {
      if (["name", "description", "notifications_enabled"].includes(key))
        continue;
      if (key === "secret_headers") {
        if (!payload[key].length) delete payload[key];
        continue;
      }
      if (key === "auth") {
        if (payload.auth.type === monitor.auth?.type && !payload.auth.token)
          delete payload.auth;
        continue;
      }
      if (canonical(payload[key]) === canonical(monitor[key]))
        delete payload[key];
    }
  }
  async function submitMonitor(form) {
    const button = form.querySelector("[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    form.querySelector("[data-form-error]").textContent = "";
    try {
      const data = new FormData(form);
      const m = form._monitor;
      const locked = m && m.state !== "paused";
      const payload = {
        name: data.get("name").trim(),
        description: data.get("description"),
        notifications_enabled: data.has("notifications_enabled"),
      };
      if (!locked) {
        const method = data.get("method");
        let url;
        try {
          url = new URL(data.get("url"));
        } catch {
          throw new Error("Enter a valid absolute HTTP or HTTPS URL.");
        }
        const params = readPairs(form, "query_params");
        for (const [name, value] of url.searchParams)
          params.push({ name, value });
        url.search = "";
        const assertions = Array.from(
          form.querySelectorAll(".assertion-row"),
        ).map((row) => {
          const operator = row.querySelector("[data-assert-operator]").value;
          const assertion = {
            pointer: row.querySelector("[data-assert-pointer]").value,
            operator,
          };
          if (operator === "equals")
            assertion.expected = new JsonLiteral(
              row.querySelector("[data-assert-expected]").value,
              "Expected assertion value",
            );
          return assertion;
        });
        if (method === "HEAD" && assertions.length)
          throw new Error("HEAD requests cannot include JSON assertions.");
        const bodyEnabled =
          data.has("body_enabled") &&
          !["GET", "HEAD", "OPTIONS"].includes(method);
        const bearer = data.get("bearer_token");
        const auth =
          data.get("auth_type") === "bearer"
            ? { type: "bearer", ...(bearer ? { token: bearer } : {}) }
            : { type: "none" };
        Object.assign(payload, {
          url: url.href,
          method,
          headers: readPairs(form, "headers"),
          secret_headers: readPairs(form, "secret_headers"),
          query_params: params,
          body: bodyEnabled
            ? {
                enabled: true,
                json: new JsonLiteral(data.get("body_json"), "Request body"),
              }
            : { enabled: false },
          auth,
          side_effects_acknowledged: data.has("side_effects_acknowledged"),
          interval_seconds: Number(data.get("interval_seconds")),
          timeout_ms: Number(data.get("timeout_ms")),
          expected_status: Number(data.get("expected_status")),
          max_response_time_ms: data.get("max_response_time_ms")
            ? Number(data.get("max_response_time_ms"))
            : null,
          json_assertions: assertions,
          failure_threshold: Number(data.get("failure_threshold")),
          recovery_threshold: Number(data.get("recovery_threshold")),
        });
        if (
          bodyEnabled &&
          new TextEncoder().encode(data.get("body_json")).length > 65536
        )
          throw new Error("The JSON request body must not exceed 64 KiB.");
        if (
          ["POST", "PUT", "PATCH", "DELETE"].includes(method) &&
          !payload.side_effects_acknowledged
        )
          throw new Error(
            "Confirm that this repeated request may change data at the target.",
          );
      }
      if (m) {
        pruneUnchangedExecution(payload, m);
        payload.expected_version = m.record_version;
      } else payload.enabled = data.has("enabled");
      const result = await api(
        m ? `/monitors/${resource(m.id)}` : "/monitors",
        { method: m ? "PATCH" : "POST", data: payload },
      );
      if (m) {
        const index = state.monitors.findIndex((item) => item.id === m.id);
        if (index >= 0) state.monitors[index] = result;
      } else state.monitors.unshift(result);
      closeModal();
      toast(
        m
          ? "Monitor updated."
          : "Monitor created. Its first check will run in the background.",
      );
      if (!m) {
        location.hash = `monitor/${result.id}`;
      } else await navigate({ refresh: true });
    } catch (error) {
      formError(form, error);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }
  function tokenCreate() {
    openModal(
      modalHeader(
        "Create API token",
        "Give each tool a separate token with only the access it needs.",
      ) +
        `<form data-form="token"><div class="modal-body"><div class="form-error" data-form-error tabindex="-1" role="alert"></div><div class="field"><label for="token-name">Token name</label><input id="token-name" name="name" class="input" placeholder="CI reports" maxlength="100" required autofocus></div><div class="field-row"><div class="field"><label for="token-scope">Permission</label><select id="token-scope" name="scope" class="input"><option value="read">Read only</option><option value="read_write">Read & write</option></select></div><div class="field"><label for="token-expiry">Expires in (days)</label><input id="token-expiry" name="expires_days" type="number" min="1" max="365" step="1" value="90" class="input" required></div></div><div class="notice">${icon("shield")}<span>Read-only tokens can view monitoring data. Read-and-write tokens can also manage monitors and run diagnostics. Account and integration settings always require a browser session.</span></div></div><div class="modal-footer"><button class="button ghost" data-action="close-modal" type="button">Cancel</button><button class="button primary" type="submit">Create token</button></div></form>`,
    );
  }
  async function submitToken(form) {
    const button = form.querySelector("[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    try {
      const data = new FormData(form);
      const result = await api("/api-tokens", {
        method: "POST",
        data: {
          name: data.get("name"),
          scope: data.get("scope"),
          expires_days: Number(data.get("expires_days")),
        },
      });
      state.tokenSecret = result.token;
      modal.innerHTML =
        modalHeader(
          "Save your API token",
          "This is the only time the complete token is shown.",
        ) +
        `<div class="modal-body"><div class="token-secret"><p>Copy this token now and store it securely. Closing this dialog clears it from the page.</p><code id="created-token">${esc(result.token)}</code><div class="actions"><button class="button small" data-action="copy-token">${icon("copy")} Copy token</button></div></div><p class="small muted">You can revoke this token at any time. Never add it to source code or a public repository.</p></div><div class="modal-footer"><button class="button primary" data-action="token-done">I saved my token</button></div>`;
    } catch (error) {
      formError(form, error);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }
  function passwordChange() {
    openModal(
      modalHeader(
        "Change password",
        "This signs out every session and revokes your API tokens.",
      ) +
        `<form data-form="password"><div class="modal-body"><div class="form-error" data-form-error tabindex="-1" role="alert"></div><div class="field"><label for="current-password">Current password</label><input id="current-password" name="current_password" class="input" type="password" required autocomplete="current-password" autofocus></div><div class="field"><label for="new-password">New password</label><input id="new-password" name="new_password" class="input" type="password" minlength="12" maxlength="128" required autocomplete="new-password"><small>12–128 characters. Use a password you do not use elsewhere.</small></div></div><div class="modal-footer"><button class="button ghost" type="button" data-action="close-modal">Cancel</button><button class="button primary" type="submit">Change password</button></div></form>`,
    );
  }
  async function submitPassword(form) {
    const button = form.querySelector("[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    try {
      const data = new FormData(form);
      await api("/auth/password", {
        method: "POST",
        data: {
          current_password: data.get("current_password"),
          new_password: data.get("new_password"),
        },
      });
      closeModal();
      state.user = null;
      state.csrf = null;
      location.hash = "login";
      renderAuth("login");
      toast("Password changed. Sign in again with your new password.");
    } catch (error) {
      formError(form, error);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }
  function currentMonitor(id) {
    return state.detail?.id === id
      ? state.detail
      : state.monitors.find((m) => m.id === id);
  }
  async function refreshChecks() {
    const id = state.detail?.id;
    if (!id) return;
    const p = period();
    state.checks = await api(
      `/monitors/${resource(id)}/checks?` +
        query({
          ...p,
          ...state.checkFilters,
          cursor: state.checkCursor,
          limit: 20,
        }),
    );
    renderDetail();
  }
  async function showCheck(id, quiet = false) {
    if (!quiet)
      openModal(
        modalHeader(
          "Check details",
          "A diagnostic view of one observed request.",
        ) + loading,
      );
    const generation = modalGeneration;
    try {
      const c = await api(`/checks/${resource(id)}`);
      if (!modal.open || generation !== modalGeneration) return;
      const fields = [
        ["Trigger", c.trigger],
        ["Outcome", label(c.outcome || c.status)],
        ["HTTP status", c.http_status ?? "—"],
        ["Response time", ms(c.response_time_ms)],
        ["Elapsed time", ms(c.elapsed_ms)],
        ["Configuration", `v${c.config_version}`],
        ["Scheduled", fullDate(c.scheduled_at)],
        ["Started", fullDate(c.started_at)],
        ["Finished", fullDate(c.finished_at)],
        ["Error code", c.error_code || "—"],
        [
          "Failed assertions",
          json(c.failed_assertion_indices || c.failed_assertions || []),
        ],
        ["Request ID", c.id],
      ];
      modal.innerHTML =
        modalHeader(
          "Check details",
          c.trigger === "manual"
            ? "Manual diagnostics do not affect statistics or incidents."
            : "A scheduled observation of your endpoint.",
        ) +
        `<div class="modal-body">${status(c.outcome || c.status)}<dl class="check-detail">${fields.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>${c.error_message ? `<div class="notice error">${icon("info")}<span>${esc(c.error_message)}</span></div>` : ""}</div><div class="modal-footer"><button class="button ghost" data-action="check-detail" data-id="${attr(id)}">${icon("refresh")} Refresh</button><button class="button primary" data-action="close-modal">Done</button></div>`;
      if (["queued", "running"].includes(c.status))
        setTimeout(() => {
          if (modal.open && generation === modalGeneration) showCheck(id, true);
        }, 2000);
    } catch (error) {
      if (modal.open && generation === modalGeneration)
        modal.innerHTML =
          modalHeader("Could not load check", "") +
          `<div class="modal-body"><div class="form-error">${errorMarkup(error)}</div></div>`;
    }
  }
  async function showIncident(id) {
    openModal(
      modalHeader(
        "Incident details",
        "Timeline of a confirmed monitoring issue.",
      ) + loading,
    );
    const generation = modalGeneration;
    try {
      const i = await api(`/incidents/${resource(id)}`);
      if (!modal.open || generation !== modalGeneration) return;
      const fields = [
        [
          "Monitor",
          i.monitor_name ||
            state.monitors.find((m) => m.id === i.monitor_id)?.name ||
            "Deleted monitor",
        ],
        ["Configuration", `v${i.config_version}`],
        ["First failure", fullDate(i.started_at)],
        ["Confirmed", fullDate(i.detected_at || i.created_at)],
        ["Recovered", fullDate(i.recovered_at || i.resolved_at)],
        ["Ended", fullDate(i.ended_at || i.resolved_at)],
        ["Duration", duration(i.started_at, i.resolved_at || i.ended_at)],
        ["Reason", i.reason || i.error_code || i.abort_reason || "—"],
      ];
      modal.innerHTML =
        modalHeader(
          "Incident " + i.id.slice(0, 8),
          i.status === "aborted"
            ? "Monitoring ended before recovery could be confirmed."
            : "Failures and recoveries are confirmed by consecutive scheduled checks.",
        ) +
        `<div class="modal-body">${status(i.status)}<dl class="check-detail">${fields.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl></div><div class="modal-footer"><a class="button ghost" href="#monitor/${attr(i.monitor_id)}?tab=incidents" data-action="close-modal">View monitor</a><button class="button primary" data-action="close-modal">Done</button></div>`;
    } catch (error) {
      if (modal.open && generation === modalGeneration)
        modal.innerHTML =
          modalHeader("Could not load incident", "") +
          `<div class="modal-body"><div class="form-error">${errorMarkup(error)}</div></div>`;
    }
  }

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-form]");
    if (!form) return;
    event.preventDefault();
    if (form.dataset.form === "auth") submitAuth(form);
    if (form.dataset.form === "monitor") submitMonitor(form);
    if (form.dataset.form === "token") submitToken(form);
    if (form.dataset.form === "password") submitPassword(form);
    if (form.dataset.form === "incident-period") submitIncidentPeriod(form);
    if (form.dataset.form === "custom-period") {
      const data = new FormData(form);
      try {
        const from = new Date(data.get("from")),
          to = new Date(data.get("to"));
        if (!Number.isFinite(+from) || !Number.isFinite(+to) || from >= to)
          throw new Error("Choose a valid period with an end after its start.");
        if (to - from > 2592000000)
          throw new Error("Choose a period of no more than 30 days.");
        state.customFrom = data.get("from");
        state.customTo = data.get("to");
        state.periodBounds = null;
        state.checkCursor = null;
        state.checkStack = [];
        navigate({ refresh: true });
      } catch (error) {
        toast(error.message, true);
      }
    }
  });
  document.addEventListener("input", (event) => {
    if (event.target.id === "monitor-search") {
      state.monitorSearch = event.target.value;
      renderMonitorResults();
    }
  });
  document.addEventListener("change", async (event) => {
    const el = event.target;
    try {
      if (el.id === "monitor-status") {
        state.monitorStatus = el.value;
        renderMonitorResults();
      }
      if (
        ["incident-status", "incident-monitor", "incident-range"].includes(
          el.id,
        )
      ) {
        await applyIncidentFilters();
      }
      if (el.id === "detail-range") {
        state.range = el.value;
        state.periodBounds = null;
        state.checkCursor = null;
        state.checkStack = [];
        if (el.value === "custom") {
          const local = (value) => {
            const d = new Date(value);
            return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
              .toISOString()
              .slice(0, 16);
          };
          state.customFrom = local(Date.now() - 86400000);
          state.customTo = local(Date.now());
          renderDetail();
        } else {
          state.customFrom = null;
          state.customTo = null;
          await navigate({ refresh: true });
        }
      }
      if (["check-trigger", "check-outcome", "check-version"].includes(el.id)) {
        state.checkFilters = {
          trigger: document.querySelector("#check-trigger").value,
          outcome: document.querySelector("#check-outcome").value,
          config_version: document.querySelector("#check-version").value,
        };
        state.checkCursor = null;
        state.checkStack = [];
        await navigate({ refresh: true });
      }
      if (el.id === "monitor-method") {
        const allowed = !["GET", "HEAD", "OPTIONS"].includes(el.value);
        document.querySelector("#body-section").hidden = !allowed;
        document.querySelector("#side-effect-notice").hidden = !allowed;
      }
      if (el.id === "monitor-auth")
        document.querySelector("#bearer-field").hidden = el.value !== "bearer";
      if (el.id === "body-enabled")
        document.querySelector("#body-json-field").hidden = !el.checked;
      if (el.matches("[data-assert-operator]"))
        el
          .closest(".assertion-row")
          .querySelector("[data-assert-expected]").disabled =
          el.value === "exists";
    } catch (error) {
      toast(error.message, true);
    }
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    const id = button.dataset.id;
    try {
      if (action === "close-modal") {
        closeModal();
        return;
      }
      if (action === "menu") {
        const navigation = document.querySelector("#primary-navigation");
        const open = navigation.classList.toggle("open");
        button.setAttribute("aria-expanded", String(open));
        return;
      }
      if (action === "logout") {
        await busy(button, async () => {
          await api("/auth/logout", { method: "POST" });
          state.user = null;
          state.csrf = null;
          state.monitors = [];
          state.detail = null;
          location.hash = "login";
          renderAuth("login");
        });
        return;
      }
      if (action === "refresh") {
        await busy(button, () => navigate({ refresh: true }));
        return;
      }
      if (action === "create-monitor") {
        monitorForm();
        return;
      }
      if (action === "edit-monitor") {
        const m = await api(`/monitors/${resource(id)}`);
        monitorForm(m);
        return;
      }
      if (action === "toggle-monitor") {
        await busy(button, async () => {
          const m =
            currentMonitor(id) || (await api(`/monitors/${resource(id)}`));
          await api(
            `/monitors/${resource(id)}/${m.state === "paused" ? "resume" : "pause"}`,
            { method: "POST", data: { expected_version: m.record_version } },
          );
          toast(
            m.state === "paused"
              ? "Monitor resumed. Its next check is due now."
              : "Monitor paused. New scheduled requests are stopped.",
          );
          await navigate({ refresh: true });
        });
        return;
      }
      if (action === "delete-monitor") {
        const m =
          currentMonitor(id) || (await api(`/monitors/${resource(id)}`));
        confirmAction(
          "Delete " + m.name + "?",
          "Checks will stop and saved request credentials will be removed. Open incidents end without a recovery notice.",
          "Delete monitor",
          async () => {
            await api(
              `/monitors/${resource(id)}?` +
                query({ expected_version: m.record_version }),
              { method: "DELETE" },
            );
            state.monitors = state.monitors.filter((item) => item.id !== id);
            toast("Monitor deleted.");
            if (state.route.startsWith("monitor/")) location.hash = "monitors";
            else await navigate({ refresh: true });
          },
        );
        return;
      }
      if (action === "run-monitor") {
        await busy(button, async () => {
          const result = await api(`/monitors/${resource(id)}/run-now`, {
            method: "POST",
            headers: { "Idempotency-Key": crypto.randomUUID() },
          });
          toast(
            "Diagnostic check queued. Scheduled status and incidents are unchanged.",
          );
          await showCheck(result.check_id || result.id);
        });
        return;
      }
      if (action === "detail-tab") {
        state.detailTab = button.dataset.tab;
        history.replaceState(
          null,
          "",
          "#" + state.route + "?tab=" + encodeURIComponent(state.detailTab),
        );
        renderDetail();
        return;
      }
      if (action === "check-detail") {
        await showCheck(id);
        return;
      }
      if (action === "incident-detail") {
        await showIncident(id);
        return;
      }
      if (action === "checks-next") {
        if (state.checks?.has_more) {
          state.checkStack.push(state.checkCursor);
          state.checkCursor = state.checks.next_cursor;
          await refreshChecks();
        }
        return;
      }
      if (action === "checks-prev") {
        state.checkCursor = state.checkStack.pop() || null;
        await refreshChecks();
        return;
      }
      if (action === "incidents-next") {
        const data = state.route.startsWith("monitor/")
          ? state.monitorIncidents
          : state.incidentPage;
        if (data?.has_more) {
          state.incidentStack.push(state.incidentCursor);
          state.incidentCursor = data.next_cursor;
          await navigate({ refresh: true });
        }
        return;
      }
      if (action === "incidents-prev") {
        state.incidentCursor = state.incidentStack.pop() || null;
        await navigate({ refresh: true });
        return;
      }
      if (action === "notifications-next") {
        if (state.notifications?.has_more) {
          state.notificationStack.push(state.notificationCursor);
          state.notificationCursor = state.notifications.next_cursor;
          await navigate({ refresh: true });
        }
        return;
      }
      if (action === "notifications-prev") {
        state.notificationCursor = state.notificationStack.pop() || null;
        await navigate({ refresh: true });
        return;
      }
      if (action === "tokens-next") {
        if (state.tokens?.has_more) {
          state.tokenStack ??= [];
          state.tokenStack.push(state.tokenCursor);
          state.tokenCursor = state.tokens.next_cursor;
          await navigate({ refresh: true });
        }
        return;
      }
      if (action === "tokens-prev") {
        state.tokenCursor = state.tokenStack.pop() || null;
        await navigate({ refresh: true });
        return;
      }
      if (action === "add-pair") {
        const target = modal.querySelector(
          `[data-pairs="${button.dataset.kind}"]`,
        );
        const max = button.dataset.kind === "query_params" ? 100 : 20;
        if (target.children.length >= max) {
          toast(`A maximum of ${max} entries is supported.`, true);
          return;
        }
        target.insertAdjacentHTML(
          "beforeend",
          pairRow({}, button.dataset.kind),
        );
        target.lastElementChild.querySelector("input").focus();
        return;
      }
      if (action === "remove-pair") {
        const row = button.closest(".pair-row");
        if (row.dataset.saved === "true") {
          if (row.dataset.removed === "true") {
            row.dataset.removed = "false";
            row.classList.remove("secret-removal");
            row.querySelector("[data-pair-value]").disabled = false;
            button.setAttribute("aria-label", "Remove secret header");
            button.innerHTML = icon("close");
          } else {
            row.dataset.removed = "true";
            row.classList.add("secret-removal");
            row.querySelector("[data-pair-value]").disabled = true;
            button.setAttribute("aria-label", "Undo secret header removal");
            button.innerHTML = icon("refresh");
          }
        } else row.remove();
        return;
      }
      if (action === "add-assertion") {
        const list = document.querySelector("#assertion-list");
        if (list.children.length >= 20) {
          toast("A monitor can have at most 20 JSON assertions.", true);
          return;
        }
        list.insertAdjacentHTML("beforeend", assertionRow());
        list.lastElementChild.querySelector("input").focus();
        return;
      }
      if (action === "remove-assertion") {
        button.closest(".assertion-row").remove();
        return;
      }
      if (action === "telegram-link") {
        await busy(button, async () => {
          const result = await api("/integrations/telegram/link", {
            method: "POST",
          });
          let url;
          try {
            url = new URL(result.url);
            if (url.protocol !== "https:" || url.hostname !== "t.me")
              throw new Error();
          } catch {
            throw new Error("The server returned an invalid Telegram link.");
          }
          openModal(
            modalHeader(
              "Connect Telegram",
              "Confirm the connection in your private chat with the bot.",
            ) +
              `<div class="modal-body"><div class="notice">${icon("telegram")}<span>Open Telegram and press <strong>Start</strong>. This link expires ${esc(date(result.expires_at))} and can only be used once.</span></div><a class="button primary" href="${attr(url.href)}" target="_blank" rel="noreferrer noopener">Open Telegram ${icon("external")}</a><p class="field-note mt-15">Return here after pressing Start, then check the connection. You can enable notifications separately on each monitor.</p></div><div class="modal-footer"><button class="button ghost" data-action="close-modal">Later</button><button class="button primary" data-action="telegram-verify">Check connection</button></div>`,
          );
        });
        return;
      }
      if (action === "telegram-verify") {
        await busy(button, async () => {
          const result = await api("/integrations/telegram");
          if (result.connected) {
            closeModal();
            toast(
              "Telegram connected. Enable alerts on the monitors you want to hear about.",
            );
            await navigate({ refresh: true });
          } else
            toast(
              "Not connected yet. Press Start in the Telegram bot and check again.",
              true,
            );
        });
        return;
      }
      if (action === "telegram-test") {
        await busy(button, async () => {
          const result = await api("/integrations/telegram/test", {
            method: "POST",
          });
          toast("Test message queued. Its delivery status is shown below.");
          await navigate({ refresh: true });
          if (result.notification_id) {
            const n = await api(
              `/notifications/${resource(result.notification_id)}`,
            );
            if (n.status === "failed")
              toast(
                n.error_message ||
                  "Test delivery failed. Check the notification history.",
                true,
              );
          }
        });
        return;
      }
      if (action === "telegram-disconnect") {
        confirmAction(
          "Disconnect Telegram?",
          "Pending messages for this connection will be cancelled. Checks and incident history continue.",
          "Disconnect",
          async () => {
            await api("/integrations/telegram", { method: "DELETE" });
            toast("Telegram disconnected.");
            await navigate({ refresh: true });
          },
        );
        return;
      }
      if (action === "token-create") {
        tokenCreate();
        return;
      }
      if (action === "copy-token") {
        try {
          await navigator.clipboard.writeText(state.tokenSecret || "");
          toast("Token copied. Store it securely.");
        } catch {
          const selection = window.getSelection();
          const range = document.createRange();
          range.selectNodeContents(document.querySelector("#created-token"));
          selection.removeAllRanges();
          selection.addRange(range);
          toast("Select and copy the highlighted token.");
        }
        return;
      }
      if (action === "token-done") {
        state.tokenCursor = null;
        state.tokenStack = [];
        closeModal();
        await navigate({ refresh: true });
        return;
      }
      if (action === "token-revoke") {
        confirmAction(
          "Revoke this API token?",
          "Tools using it will immediately lose API access. You cannot restore a revoked token.",
          "Revoke token",
          async () => {
            await api(`/api-tokens/${resource(id)}`, { method: "DELETE" });
            toast("API token revoked.");
            await navigate({ refresh: true });
          },
        );
        return;
      }
      if (action === "password-change") {
        passwordChange();
        return;
      }
      if (action === "account-delete") {
        confirmAction(
          "Permanently delete your account?",
          "This cannot be undone. Your monitors, results, incidents, integrations, and credentials will be deleted.",
          "Delete my account",
          async (data) => {
            await api("/auth/account", {
              method: "DELETE",
              data: { password: data.get("password") },
            });
            state.user = null;
            state.csrf = null;
            state.monitors = [];
            state.detail = null;
            location.hash = "login";
            renderAuth("login");
            toast("Your account and monitoring data have been deleted.");
          },
          { password: true },
        );
        return;
      }
    } catch (error) {
      toast(error.message, true);
    }
  });
  function closeNavigation(restoreFocus = false) {
    const navigation = document.querySelector("#primary-navigation.open");
    if (!navigation) return;
    navigation.classList.remove("open");
    const toggle = document.querySelector('[data-action="menu"]');
    toggle?.setAttribute("aria-expanded", "false");
    if (restoreFocus) toggle?.focus();
  }
  document.addEventListener("click", (event) => {
    const navigation = document.querySelector("#primary-navigation.open");
    if (
      navigation &&
      (event.target.closest("#primary-navigation a") ||
        (!navigation.contains(event.target) &&
          !event.target.closest('[data-action="menu"]')))
    ) {
      closeNavigation();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeNavigation(true);
  });
  window.addEventListener("hashchange", () => {
    if (modal.open) closeModal();
    if (!state.user) {
      const mode = authLocation();
      renderAuth(
        ["login", "register", "forgot", "reset"].includes(mode)
          ? mode
          : "login",
      );
    } else navigate();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.user && !modal.open && !state.loading)
      navigate({ refresh: true });
  });
  setInterval(() => {
    if (
      !document.hidden &&
      state.user &&
      !modal.open &&
      !state.loading &&
      !document.activeElement?.matches("input,select,textarea")
    )
      navigate({ refresh: true });
  }, 15000);
  (async () => {
    try {
      if (authLocation() === "reset") {
        renderAuth("reset");
        return;
      }
      state.user = await api("/auth/me", { allowAnonymous: true });
      state.csrf = (await api("/auth/csrf")).csrf_token;
      await navigate();
    } catch (error) {
      if (error.status === 401) {
        const mode = authLocation();
        renderAuth(
          ["register", "forgot", "reset"].includes(mode) ? mode : "login",
        );
      } else {
        renderAuth("login");
        toast(error.message, true);
      }
    }
  })();
})();
