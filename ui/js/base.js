/* =========================================================================
   PRS – Shared Frontend Helpers (production build)
   Exposes: window.PRS = { $, $$, qs, apiUrl, fmt, startWallClock, debounce,
                           throttle, onVisible, jsonFetch, NetStatus }
   -------------------------------------------------------------------------
   - Zero dependencies. No frameworks.
   - Safe to load on any page; everything is namespaced under PRS.
   - No UI side effects on its own.
   ========================================================================= */

(() => {
  // Guard: keep existing PRS if reloaded (hot reload / multiple pages)
  const PRS = (window.PRS = window.PRS || {});

  /* ---------------------------------------------------------------------------
 * Engine host resolver + status text normalizer (YAML-driven)
 * Depends on optional bootstrap object injected by the server/launcher:
 *   window.PRS_BOOT or window.__PRS_BOOTSTRAP
 * Looks under:
 *   app.client.engine.*   (mode, fixed_host, prefer_same_origin, allow_client_override)
 *   app.ui.net_status_text (ok, connecting, disconnected)
 * -------------------------------------------------------------------------*/
(function () {
  const PRS = (window.PRS = window.PRS || {});
  const BOOT =
    window.PRS_BOOT || window.BOOTSTRAP || window.__PRS_BOOTSTRAP || {};

  // Read YAML-backed defaults if provided by bootstrap
  const appCfg = (BOOT && BOOT.app) || {};
  const yamlClient = ((appCfg.client || {}).engine || {});
  const yamlUI = (appCfg.ui || {});

  // Client policy (with safe defaults)
  const MODE = (yamlClient.mode || "auto").toLowerCase(); // auto | localhost | fixed
  const FIXED = (yamlClient.fixed_host || "").trim();
  const PREFER_SAME = yamlClient.prefer_same_origin !== false; // default true
  const ALLOW_OVERRIDE = !!yamlClient.allow_client_override;

  function sameOriginAllowed() {
    // Only meaningful when pages are served via http(s) (not file://)
    return /^https?:/i.test(location.protocol) && PREFER_SAME;
  }

  function getDeviceOverride() {
    if (!ALLOW_OVERRIDE) return "";
    try {
      return (localStorage.getItem("cc.engine_host") || "").trim();
    } catch {
      return "";
    }
  }

  function resolveFromPolicy() {
    const LOCALHOST = "127.0.0.1:8000";
    if (MODE === "fixed" && FIXED) return FIXED;
    if (MODE === "localhost") return LOCALHOST;
    // MODE === auto
    if (sameOriginAllowed()) return "same-origin";
    if (FIXED) return FIXED;
    return LOCALHOST;
  }

  function resolveEngineHost() {
    // 1) Prefer same-origin if allowed and applicable
    if (sameOriginAllowed()) return "same-origin";
    // 2) Device override (if allowed)
    const override = getDeviceOverride();
    if (override) return override;
    // 3) YAML policy fallback
    return resolveFromPolicy();
  }

  const EFFECTIVE = resolveEngineHost();
  PRS.ALLOW_OVERRIDE = ALLOW_OVERRIDE;
  PRS.PREFER_SAME_ORIGIN = PREFER_SAME;
  PRS.EFFECTIVE_ENGINE = EFFECTIVE;

  // Build a URL for API calls. Returns relative paths when on same-origin.
  function url(path) {
    const p = String(path || "/").replace(/^\/+/, "");
    if (EFFECTIVE === "same-origin") return "/" + p;
    const base = EFFECTIVE.match(/^https?:\/\//i)
      ? EFFECTIVE
      : "http://" + EFFECTIVE;
    return base.replace(/\/+$/, "") + "/" + p;
  }
  PRS.url = PRS.url || url;

  // Net status texts (from YAML ui.net_status_text, with sensible defaults)
  const netTexts = (yamlUI.net_status_text || {});
  const TEXTS = {
    ok: netTexts.ok || "OK",
    connecting: netTexts.connecting || "Connecting…",
    disconnected: netTexts.disconnected || "Disconnected — retrying…",
  };
  PRS.NET_TEXT = PRS.NET_TEXT || TEXTS;

  // Small helper for displaying the effective host in a footer, etc.
  PRS.effectiveEngineLabel =
    PRS.effectiveEngineLabel ||
    function () {
      return EFFECTIVE === "same-origin" ? "same-origin" : EFFECTIVE;
    };

  // Patch/define setNetStatus to use standardized texts and classes
  const prevSetNetStatus = PRS.setNetStatus;
  PRS.setNetStatus = function setNetStatus(
    state,
    elMsg = document.getElementById("netMsg"),
    elDot = document.getElementById("netDot")
  ) {
    const s = String(state || "").toLowerCase();
    const text =
      s === "ok"
        ? TEXTS.ok
        : s === "connecting"
        ? TEXTS.connecting
        : TEXTS.disconnected;

    if (elMsg) elMsg.textContent = text;

    if (elDot) {
      elDot.classList.remove("ok", "connecting", "disconnected");
      elDot.classList.add(s === "ok" ? "ok" : s === "connecting" ? "connecting" : "disconnected");
      elDot.setAttribute("aria-label", text);
      elDot.title = text;
    }

    // Allow any prior behavior to run (non-breaking)
    if (typeof prevSetNetStatus === "function") {
      try {
        prevSetNetStatus(state, elMsg, elDot);
      } catch {}
    }
  };

  // Provide a safe JSON fetch wrapper if one isn't already defined
  if (!PRS.fetchJSON) {
    PRS.fetchJSON = async function (path, opts) {
      const u = url(path);
      const r = await fetch(u, opts);
      if (!r.ok) throw new Error(`HTTP ${r.status} for ${u}`);
      return r.json();
    };
  }
})();

  /* -----------------------------------------------------------------------
     DOM helpers
     --------------------------------------------------------------------- */
  const $  = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // Read a querystring value with a fallback
  function qs(name, fallback = null) {
    const v = new URLSearchParams(location.search).get(name);
    return v === null ? fallback : v;
  }

  /* -----------------------------------------------------------------------
     API URL helper
     - Builds a path with query params and an optional race_id
     --------------------------------------------------------------------- */
  function apiUrl(path, params = {}, raceId = null) {
    const url = new URL(path, location.origin);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    });
    if (raceId != null && !url.searchParams.has("race_id")) {
      url.searchParams.set("race_id", String(raceId));
    }
    return url.pathname + (url.search ? url.search : "");
  }

  /* -----------------------------------------------------------------------
     Formatters
     --------------------------------------------------------------------- */
  const fmt = {
    // Race clock mm:ss from milliseconds
    raceClock(ms) {
      if (ms == null || isNaN(ms)) return "--:--";
      const total = Math.max(0, Math.floor(ms / 1000));
      const mm = Math.floor(total / 60);
      const ss = String(total % 60).padStart(2, "0");
      return `${mm}:${ss}`;
    },
    // Lap seconds to 0.000 (or em dash if nullish)
    lapSeconds(sec) {
      return sec == null || isNaN(sec) ? "—" : Number(sec).toFixed(3);
    },
    // Generic ms to hh:mm:ss
    ms(ms) {
      if (ms == null || isNaN(ms)) return "--:--:--";
      const t = Math.max(0, Math.floor(ms / 1000));
      const hh = String(Math.floor(t / 3600)).padStart(2, "0");
      const mm = String(Math.floor((t % 3600) / 60)).padStart(2, "0");
      const ss = String(t % 60).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    },
  };

  /* -----------------------------------------------------------------------
     Time utilities
     --------------------------------------------------------------------- */
  function debounce(fn, wait = 200) {
    let t = null;
    return function debounced(...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), wait);
    };
  }

  function throttle(fn, wait = 200) {
    let last = 0, timer = null, trailingArgs = null, ctx = null;
    return function throttled(...args) {
      const now = Date.now();
      ctx = this;
      if (now - last >= wait) {
        last = now;
        fn.apply(ctx, args);
      } else {
        trailingArgs = args;
        clearTimeout(timer);
        timer = setTimeout(() => {
          last = Date.now();
          fn.apply(ctx, trailingArgs);
          trailingArgs = null;
        }, wait - (now - last));
      }
    };
  }

  // Run a callback only when the page is visible (after it becomes visible)
  function onVisible(cb) {
    if (document.visibilityState === "visible") { cb(); return; }
    const handler = () => {
      if (document.visibilityState === "visible") {
        document.removeEventListener("visibilitychange", handler);
        cb();
      }
    };
    document.addEventListener("visibilitychange", handler);
  }

  /* -----------------------------------------------------------------------
     JSON fetch with sane defaults and small retry
     - cache: "no-store" to avoid stale timing data
     - retries network errors (not 4xx/5xx) a couple times with backoff
     --------------------------------------------------------------------- */
  async function jsonFetch(path, { method = "GET", body, headers, retries = 2 } = {}) {
    const opts = {
      method,
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...(headers || {}) },
    };
    if (body !== undefined) opts.body = typeof body === "string" ? body : JSON.stringify(body);

    let attempt = 0, lastErr;
    while (attempt <= retries) {
      try {
        const res = await fetch(path, opts);
        // treat non-OK as errors but don't retry (server responded)
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          const err = new Error(`HTTP ${res.status} on ${path}: ${text.slice(0, 200)}`);
          err.status = res.status;
          throw err;
        }
        return await res.json();
      } catch (e) {
        lastErr = e;
        // Only retry on network-ish errors (when status is undefined)
        if (e && typeof e.status === "number") break;
        if (attempt++ >= retries) break;
        await new Promise(r => setTimeout(r, 250 * attempt)); // backoff
      }
    }
    throw lastErr || new Error("Request failed");
  }

  /* -----------------------------------------------------------------------
     Wall clock (local time) – used in footer on spectator.html
     --------------------------------------------------------------------- */
  function startWallClock(selector) {
    const el = $(selector);
    if (!el) return;
    const tick = () => {
      const d = new Date();
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      el.textContent = `${hh}:${mm}:${ss}`;
    };
    tick();
    return setInterval(tick, 1000);
  }

  /* -----------------------------------------------------------------------
     NetStatus widget
     - tiny helper to wire a status dot + message
     - call .ok("msg") or .err("msg")
     --------------------------------------------------------------------- */
  class NetStatus {
    constructor({ dotSel = "#netDot", msgSel = "#netMsg" } = {}) {
      this.dot = $(dotSel);
      this.msg = $(msgSel);
    }
    ok(message = "OK") {
      if (this.dot) this.dot.style.background = "var(--ok)";
      if (this.msg) this.msg.textContent = message;
    }
    err(message = "Disconnected — retrying…") {
      if (this.dot) this.dot.style.background = "var(--error)";
      if (this.msg) this.msg.textContent = message;
    }
  }

  /* -----------------------------------------------------------------------
     Export
     --------------------------------------------------------------------- */
  PRS.$ = $;
  PRS.$$ = $$;
  PRS.qs = qs;
  PRS.apiUrl = apiUrl;
  PRS.fmt = fmt;
  PRS.debounce = debounce;
  PRS.throttle = throttle;
  PRS.onVisible = onVisible;
  PRS.jsonFetch = jsonFetch;
  PRS.startWallClock = startWallClock;
  PRS.NetStatus = NetStatus;
})();
