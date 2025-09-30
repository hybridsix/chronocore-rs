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
