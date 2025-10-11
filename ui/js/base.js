/* ==========================================================================
   ChronoCore / CCRS base helpers
   - Classic <script> (non-module) file that exposes helpers via window.CCRS.
   - NO legacy PRS alias; only window.CCRS is exported.
   - Keep this file tiny, dependency-free, and battle-tested.
   --------------------------------------------------------------------------
   Provided helpers:
     - $                → tiny selector
     - fetchJSON        → GET/POST/etc. returning parsed JSON (throws on !ok)
     - postJSON         → POST convenience with JSON body (returns Response)
     - makePoller       → simple polling utility with start/stop
     - setNetStatus     → updates #netDot/#netMsg if present
   Notes:
     - All functions are safe to use on pages that may or may not render the
       status elements. Missing elements are handled gracefully.
     - If you later add more helpers (fmt, debounce, etc.), export them here.
   ========================================================================== */
// === CCRS Global Init Hardening (inserted by ChatGPT) ===
(function () {
  // Ensure a single global CCRS object without clobbering prior values.
  // We extend any existing object rather than reassigning.
  const existing = (typeof window !== 'undefined' && window.CCRS && typeof window.CCRS === 'object') ? window.CCRS : {};

  // Create a new object that inherits existing enumerable props (no overwrite of functions unless intentional).
  const CCRS = Object.assign({}, existing);

  // Preserve existing apiBase if present; otherwise default to empty string.
  if (typeof CCRS.apiBase !== 'string') {
    CCRS.apiBase = '';
  }

  // Ensure apiUrl is a stable function. Do not overwrite if one already exists.
  if (typeof CCRS.apiUrl !== 'function') {
    CCRS.apiUrl = function apiUrl(path) {
      const base = (typeof CCRS.apiBase === 'string' && CCRS.apiBase) ? CCRS.apiBase : '';
      return base + String(path || '');
    };
  }

  // Re-export to the global namespace *once*.
  window.CCRS = CCRS;
})();

/* backend/static/js/base.js */
/* global window, document, fetch, AbortController */

(function () {
  // Use existing global CCRS if present, otherwise create a local object and
  // attach to window later. We intentionally avoid creating a separate
  // `PRS` local object since this file now targets `CCRS` property access.
  const CCRS = (typeof window !== 'undefined' && window.CCRS && typeof window.CCRS === 'object') ? window.CCRS : {};

  /* ---------- DOM helpers ---------- */
  CCRS.$ = (sel, root = document) => root.querySelector(sel);
  CCRS.$$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* ---------- Formatting ---------- */
  CCRS.fmtClock = (ms) => {
    if (ms == null || Number.isNaN(ms)) return "--:--";
    const s = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${String(r).padStart(2, "0")}`;
  };

  CCRS.fmtSecs3 = (secs) => {
    if (secs == null || Number.isNaN(secs)) return "—";
    return Number(secs).toFixed(3);
  };

  /* ---------- Net / fetch helpers ---------- */
  CCRS.fetchJSON = async (url, opts = {}) => {
    const timeoutMs = opts.timeout ?? 8000;
    const controller = new AbortController();
    const to = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(url, { signal: controller.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
      return await res.json();
    } finally {
      clearTimeout(to);
    }
  };

  /* Simple poller with back-off on error. */
  CCRS.makePoller = (fn, intervalMs, onError) => {
    let timer = null;
    let stopped = false;

    const tick = async () => {
      try {
        await fn();
      } catch (err) {
        onError?.(err);
      } finally {
        if (!stopped) {
          timer = setTimeout(tick, intervalMs);
        }
      }
    };

    return {
      start() {
        if (stopped) return;
        tick();
      },
      stop() {
        stopped = true;
        if (timer) clearTimeout(timer);
      },
    };
  };

  /* ---------- Status / clocks ---------- */
  CCRS.setNetStatus = (ok, msg) => {
    const dot = CCRS.$("#netDot");
    const text = CCRS.$("#netMsg");
    if (!dot || !text) return;
    dot.style.background = ok ? "var(--ok)" : "var(--error)";
    text.textContent = msg;
  };

  CCRS.startWallClock = (selectorOrEl) => {
    const el =
      typeof selectorOrEl === "string" ? CCRS.$(selectorOrEl) : selectorOrEl;
    if (!el) return null;

    const tick = () => {
      const now = new Date();
      el.textContent = now.toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
      });
    };

    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id); // returns a cancel function
  };

  /* ---------- Misc utilities ---------- */
  CCRS.debounce = (fn, ms = 200) => {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  };

  CCRS.clamp = (n, min, max) => Math.min(max, Math.max(min, n));

  /* expose */
  window.CCRS = CCRS;
})();

// === CCRS Post-Init Safety (appended by ChatGPT) ===
;(function () {
  try {
    var g = (typeof window !== 'undefined') ? window : globalThis;
    if (!g) return;
    g.CCRS = g.CCRS || {};
    if (typeof g.CCRS.apiBase !== 'string') g.CCRS.apiBase = g.CCRS.apiBase || '';
    if (typeof g.CCRS.apiUrl !== 'function') {
      g.CCRS.apiUrl = function apiUrl(path) {
        var base = (typeof g.CCRS.apiBase === 'string' && g.CCRS.apiBase) ? g.CCRS.apiBase : '';
        return base + String(path || '');
      };
    }
  } catch (e) {}
})();

