/* backend/static/js/base.js */
/* global window, document, fetch, AbortController */

(function () {
  const PRS = {};

  /* ---------- DOM helpers ---------- */
  PRS.$ = (sel, root = document) => root.querySelector(sel);
  PRS.$$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* ---------- Formatting ---------- */
  PRS.fmtClock = (ms) => {
    if (ms == null || Number.isNaN(ms)) return "--:--";
    const s = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${String(r).padStart(2, "0")}`;
  };

  PRS.fmtSecs3 = (secs) => {
    if (secs == null || Number.isNaN(secs)) return "—";
    return Number(secs).toFixed(3);
  };

  /* ---------- Net / fetch helpers ---------- */
  PRS.fetchJSON = async (url, opts = {}) => {
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
  PRS.makePoller = (fn, intervalMs, onError) => {
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
  PRS.setNetStatus = (ok, msg) => {
    const dot = PRS.$("#netDot");
    const text = PRS.$("#netMsg");
    if (!dot || !text) return;
    dot.style.background = ok ? "var(--ok)" : "var(--error)";
    text.textContent = msg;
  };

  PRS.startWallClock = (selectorOrEl) => {
    const el =
      typeof selectorOrEl === "string" ? PRS.$(selectorOrEl) : selectorOrEl;
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
  PRS.debounce = (fn, ms = 200) => {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  };

  PRS.clamp = (n, min, max) => Math.min(max, Math.max(min, n));

  /* expose */
  window.PRS = PRS;
})();
