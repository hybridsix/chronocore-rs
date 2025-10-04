/* ============================================================================
 * Operator — Race Control
 * ----------------------------------------------------------------------------
 * Responsibilities:
 *  - Poll the engine state and keep the UI in sync (flag banner, clock, table)
 *  - Post flag changes (pre/green/yellow/red/white/blue/checkered)
 *  - Show deterministic net status copy and the effective engine host
 *
 * Dependencies:
 *  - base.js (window.CCRS namespace with helpers like $, $$, startWallClock,
 *             setNetStatus, fmtClock, fetchJSON, and our new url() resolver)
 *  - CSS: base.css + spectator.css (flag theme classes) + operator.css
 *
 * Notes:
 *  - All network calls use CCRS.url()/CCRS.fetchJSON(), so host is YAML-driven.
 *  - We keep comments verbose for field debuggability.
 * ==========================================================================*/

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Safe references and tiny fallbacks (don’t fight existing helpers)
  // ---------------------------------------------------------------------------
  const CCRS = (window.CCRS = window.CCRS || {});
  const $ = CCRS.$ || ((sel, root) => (root || document).querySelector(sel));
  const $$ = CCRS.$$ || ((sel, root) => Array.from((root || document).querySelectorAll(sel)));

  // DOM cache — we keep these here to avoid repeated lookups inside the poller.
  let elFlagBanner, elClock, elRows, elNetMsg, elNetDot, elEngineHost;

  // Current UI state cache to reduce churn/reflows.
  let currentFlag = "";
  let lastClockMs = -1;

  // Polling handle
  let pollTimer = null;

  // ---------------------------------------------------------------------------
  // DOM ready
  // ---------------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", () => {
    // Core elements used across updates
    elFlagBanner = $("#flagBanner");
    elClock = $("#raceClock");
    elRows = $("#rows");
    elNetMsg = $("#netMsg");
    elNetDot = $("#netDot");
    elEngineHost = $("#engineHost"); // Optional: place to show effective engine host

    // Start a visible wall clock in the footer (provided by base.js)
    if (typeof CCRS.startWallClock === "function") CCRS.startWallClock();

    // Show the effective engine host for operator confidence
    if (elEngineHost && typeof CCRS.effectiveEngineLabel === "function") {
      elEngineHost.textContent = CCRS.effectiveEngineLabel();
      elEngineHost.setAttribute("title", "Effective Engine Host");
    }

    // Wire up all flag buttons (data-flag="pre|green|yellow|red|white|blue|checkered")
    bindFlagButtons();

    // Initial status: attempting connection
    CCRS.setNetStatus("connecting", elNetMsg, elNetDot);

    // Begin polling engine state
    startPolling();
  });

  // ---------------------------------------------------------------------------
  // Event wiring
  // ---------------------------------------------------------------------------
  function bindFlagButtons() {
    const buttons = $$('[data-flag]');
    buttons.forEach(btn => {
      btn.addEventListener("click", async () => {
        const flag = (btn.getAttribute("data-flag") || "").toLowerCase();
        if (!flag) return;
        await postFlag(flag);
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Networking — all through YAML-driven resolver
  // ---------------------------------------------------------------------------

  // Poll /race/state, update UI. Called on an interval.
  async function pollState() {
    try {
      const snapshot = await CCRS.fetchJSON("/race/state");
      // If we got here, network is up
      CCRS.setNetStatus("ok", elNetMsg, elNetDot);

      // 1) Flag banner
      if (snapshot && snapshot.flag) {
        updateFlagBanner(String(snapshot.flag).toLowerCase());
      }

      // 2) Race clock
      const ms = (snapshot && typeof snapshot.clock_ms === "number") ? snapshot.clock_ms : 0;
      updateClock(ms);

      // 3) Standings table
      if (snapshot && Array.isArray(snapshot.standings)) {
        renderStandings(snapshot.standings);
      }

      // 4) Optional: show current mode / limit if the UI has elements for it
      const elMode = $("#modeLabel");
      if (elMode && snapshot && snapshot.race_type) {
        elMode.textContent = snapshot.race_type; // e.g., "sprint"
      }
      const elCap = $("#modeCap");
      if (elCap && snapshot && snapshot.limit) {
        // snapshot.limit might look like { type: 'time'|'laps', value: N }
        const { type, value } = snapshot.limit || {};
        if (type === "time") elCap.textContent = `${Math.floor((value || 0) / 60)} min cap`;
        else if (type === "laps") elCap.textContent = `${value || 0} laps cap`;
        else elCap.textContent = "";
      }

    } catch (err) {
      // Network error or non-2xx response
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
      // Keep UI as-is; poller will retry.
    }
  }

  // Post /engine/flag with {flag}
  async function postFlag(flag) {
    try {
      CCRS.setNetStatus("connecting", elNetMsg, elNetDot);
      const res = await fetch(CCRS.url("/engine/flag"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ flag })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Fast feedback: update banner immediately (poller will confirm)
      updateFlagBanner(flag);
      CCRS.setNetStatus("ok", elNetMsg, elNetDot);
    } catch (e) {
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    }
  }

  // ---------------------------------------------------------------------------
  // Poll loop — prefer CCRS.makePoller if present; otherwise setInterval fallback
  // ---------------------------------------------------------------------------
  function startPolling() {
    // If base.js provides a makePoller with jitter/backoff, use it
    if (typeof CCRS.makePoller === "function") {
      CCRS.makePoller(pollState, 1000); // ~1s cadence; base.js may smooth this
      return;
    }
    // Simple fallback
    clearInterval(pollTimer);
    pollTimer = setInterval(pollState, 1000);
    // Run once immediately so the UI isn’t empty
    pollState();
  }

  // ---------------------------------------------------------------------------
  // UI updates
  // ---------------------------------------------------------------------------

  // Apply .flag--{name} on #flagBanner and keep a readable label inside
  function updateFlagBanner(flag) {
    if (!elFlagBanner) return;
    if (flag === currentFlag) return;

    // Remove any previous flag class
    elFlagBanner.classList.remove(
      "flag--pre","flag--green","flag--yellow","flag--red",
      "flag--white","flag--checkered","flag--blue","flag--none"
    );

    // Apply new flag class (spectator.css provides colors/animations)
    elFlagBanner.classList.add(`flag--${flag}`);

    // Update inner label if there’s a dedicated text node
    const label = $("#flagLabel", elFlagBanner);
    if (label) label.textContent = flag.toUpperCase();

    // ARIA live region support
    elFlagBanner.setAttribute("aria-live", "polite");
    elFlagBanner.setAttribute("aria-label", `Flag: ${flag}`);

    currentFlag = flag;
  }

  // Format and render the race clock. Uses base.js fmtClock if available.
  function updateClock(ms) {
    if (!elClock) return;
    if (ms === lastClockMs) return;
    lastClockMs = ms;

    // Prefer CCRS.fmtClock which handles mm:ss.t (or mm:ss)
    let text = "";
    if (typeof CCRS.fmtClock === "function") {
      text = CCRS.fmtClock(ms);
    } else {
      // very simple fallback: mm:ss
      const totalSec = Math.max(0, Math.floor(ms / 1000));
      const m = Math.floor(totalSec / 60).toString().padStart(2, "0");
      const s = (totalSec % 60).toString().padStart(2, "0");
      text = `${m}:${s}`;
    }
    elClock.textContent = text;
  }

  // Render compact standings rows into <tbody id="rows">
  // Expected fields: position, car, team, laps, last_ms, best_ms (names may vary;
  // we’re defensive here and try common variants).
  function renderStandings(rows) {
    if (!elRows || !Array.isArray(rows)) return;

    // Build a single HTML string for minimal reflow
    let html = "";
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i] || {};
      // Common keys with fallbacks
      const pos = r.position != null ? r.position : i + 1;
      const car = r.car != null ? r.car : (r.car_num || r.number || "");
      const team = r.team != null ? r.team : (r.name || r.driver || "");
      const laps = r.laps != null ? r.laps : (r.total_laps || 0);
      const lastMs = r.last_ms != null ? r.last_ms : (r.last || 0);
      const bestMs = r.best_ms != null ? r.best_ms : (r.best || 0);

      const lastStr = formatLap(lastMs);
      const bestStr = formatLap(bestMs);

      html += `
        <tr>
          <td class="pos">${pos}</td>
          <td class="car">${escapeHtml(String(car ?? ""))}</td>
          <td class="team">${escapeHtml(String(team ?? ""))}</td>
          <td class="laps">${laps}</td>
          <td class="lap last">${lastStr}</td>
          <td class="lap best">${bestStr}</td>
        </tr>`;
    }

    elRows.innerHTML = html;
  }

  // Format a lap time in ms to a short string (e.g., 1:23.456).
  function formatLap(ms) {
    const n = Number(ms);
    if (!isFinite(n) || n <= 0) return "–";
    const totalMs = Math.floor(n);
    const minutes = Math.floor(totalMs / 60000);
    const seconds = Math.floor((totalMs % 60000) / 1000);
    const millis = totalMs % 1000;
    return `${minutes}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
  }

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
})();



