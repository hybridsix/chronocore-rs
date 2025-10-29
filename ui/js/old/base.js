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
(function (w, d) {
  'use strict';

  /* ------------------------------
   * 1) Tiny query selector helper
   * ------------------------------ */
  function $(selector, root) {
    // Default to document if no explicit root is passed
    return (root || d).querySelector(selector);
  }

  /* -------------------------------------------
   * 2) Robust JSON fetch with clear error text
   * ------------------------------------------- */
  async function fetchJSON(url, options = {}) {
    // NOTE: callers must handle exceptions (try/catch); we throw on !ok.
    const res = await fetch(url, options);
    if (!res.ok) {
      // Try to give the operator/devs actionable diagnostics
      let detail = '';
      try { detail = await res.text(); } catch (_) { /* ignore */ }
  const msg = `HTTP ${res.status} ${res.statusText} - ${url}${detail ? ` - ${detail.slice(0, 240)}` : ''}`;
      const err = new Error(msg);
      err.response = res; // hand back Response for callers that need status
      throw err;
    }
    // Some endpoints might return empty; content-type gate avoids JSON parse errors
    const ct = res.headers.get('content-type') || '';
    return ct.includes('application/json') ? res.json() : null;
  }

  /* -------------------------------------------
   * 3) JSON POST convenience (returns Response)
   * ------------------------------------------- */
  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    });
  }

  /* ----------------------------------------------------
   * 4) makePoller(fn, intervalMs, onFail?) → {start,stop}
   * ----------------------------------------------------
   * - Calls async fn() repeatedly with a delay between calls.
   * - If fn throws, onFail(err) runs (if provided) and polling continues.
   * - .start() is idempotent; .stop() cancels the next scheduled tick.
   */
  function makePoller(fn, intervalMs = 2000, onFail) {
    let running = false;
    let timer = null;

    async function tick() {
      try {
        await fn();
      } catch (err) {
        if (onFail) onFail(err);
      } finally {
        if (running) timer = setTimeout(tick, intervalMs);
      }
    }

    return {
      start() {
        if (!running) { running = true; tick(); }
      },
      stop() {
        running = false;
        if (timer) clearTimeout(timer);
        timer = null;
      }
    };
  }

  /* ------------------------------------------------
   * 5) setNetStatus(ok:boolean, msg:string|undefined)
   * ------------------------------------------------
   * - Optional UI sugar: if #netDot/#netMsg exist, update them.
   * - Does nothing if elements are absent.
   */
  function setNetStatus(ok, msg) {
    const dot = $('#netDot');
    const text = $('#netMsg');
    if (dot) dot.classList.toggle('bad', !ok);
    if (text) text.textContent = msg || (ok ? 'OK' : 'Offline');
  }

  // -------------------------------
  // 6) Export the public API (CCRS)
  // -------------------------------
  w.CCRS = { $, fetchJSON, postJSON, makePoller, setNetStatus };

})(window, document);
