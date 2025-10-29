/* ============================================================================
 * Operator - Race Setup
 * ----------------------------------------------------------------------------
 * Responsibilities:
 *  - Load event + race mode YAML and render operator-friendly previews
 *  - Let operator choose a race mode (affects min lap + time/lap caps)
 *  - Load entrants (paste JSON, or pull from current /race/state snapshot)
 *  - POST entrants to engine (/engine/load) with merge/replace semantics
 *  - Trigger pre/green flags as part of preflight
 *
 * Dependencies:
 *  - base.js (window.CCRS namespace with $, $$, fmtClock, fetchJSON, url(), etc.)
 *  - js-yaml (global `jsyaml`) for client-side YAML parsing
 *  - CSS: base.css + operator.css (+ spectator.css if flag banner is shown)
 *
 * Notes:
 *  - All API calls use CCRS.url()/CCRS.fetchJSON() so engine host is YAML-driven.
 *  - YAML static files are fetched via relative paths first; if not found, we
 *    fall back to host-resolved URLs (helps when the UI is desktop `file://`).
 *  - Comments are intentionally verbose for in-the-field troubleshooting.
 * ==========================================================================*/

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Safe references + micro-helpers (don’t fight existing base.js helpers)
  // ---------------------------------------------------------------------------
  const CCRS = (window.CCRS = window.CCRS || {});
  const $ = CCRS.$ || ((sel, root) => (root || document).querySelector(sel));
  const $$ = CCRS.$$ || ((sel, root) => Array.from((root || document).querySelectorAll(sel)));

  // YAML parsing guard
  const Y = (typeof window.jsyaml !== "undefined" ? window.jsyaml : null);

  // DOM cache (populate on DOMContentLoaded)
  let elModeSelect, elModePreview, elEventPreview;
  let elEntrantsTextarea, elUseEnabledBtn, elInsertSampleBtn;
  let elLoadMergeBtn, elLoadReplaceBtn, elSetPreBtn, elGoGreenBtn;
  let elNetMsg, elNetDot, elEngineHost;

  // In-memory YAML snapshots
  let EVENT = null;     // { event: { name, date, location, timezone, branding? } }
  let MODES = null;     // { modes: { sprint: {...}, endurance: {...}, ... } }

  // ---------------------------------------------------------------------------
  // DOM Ready
  // ---------------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", () => {
    // Resolve core elements
    elModeSelect      = $("#modeSelect");
    elModePreview     = $("#modePreview");
    elEventPreview    = $("#eventPreview");
    elEntrantsTextarea= $("#entrantsJson");
    elUseEnabledBtn   = $("#btnUseEnabledEntrants");
    elInsertSampleBtn = $("#btnInsertSampleJson");
    elLoadMergeBtn    = $("#btnLoadMerge");
    elLoadReplaceBtn  = $("#btnLoadReplace");
    elSetPreBtn       = $("#btnSetPre");
    elGoGreenBtn      = $("#btnGoGreen");
    elNetMsg          = $("#netMsg");
    elNetDot          = $("#netDot");
    elEngineHost      = $("#engineHost"); // optional: a small label in footer/header

    // Visible wall clock in footer (if provided by base.js)
    if (typeof CCRS.startWallClock === "function") CCRS.startWallClock();

    // Show effective engine host for operator confidence
    if (elEngineHost && typeof CCRS.effectiveEngineLabel === "function") {
      elEngineHost.textContent = CCRS.effectiveEngineLabel();
      elEngineHost.setAttribute("title", "Effective Engine Host");
    }

    // Net status: we begin in "connecting" while we fetch YAML
    CCRS.setNetStatus("connecting", elNetMsg, elNetDot);

    // Wire buttons
    bindButtons();

    // Kick off YAML loads (event + modes), then render selects/previews
    bootstrapFromYaml().then(() => {
      CCRS.setNetStatus("ok", elNetMsg, elNetDot);
    }).catch(() => {
      // Even if YAML fails, the operator can still paste entrants manually.
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    });
  });

  // ---------------------------------------------------------------------------
  // Event wiring
  // ---------------------------------------------------------------------------
  function bindButtons() {
    if (elModeSelect) {
      elModeSelect.addEventListener("change", () => {
        renderModePreview(getSelectedModeKey());
      });
    }

    if (elUseEnabledBtn) {
      elUseEnabledBtn.addEventListener("click", useEnabledEntrantsFromState);
    }

    if (elInsertSampleBtn) {
      elInsertSampleBtn.addEventListener("click", insertSampleEntrantsJson);
    }

    if (elLoadMergeBtn) {
      elLoadMergeBtn.addEventListener("click", () => submitEntrants(true));
    }

    if (elLoadReplaceBtn) {
      elLoadReplaceBtn.addEventListener("click", () => submitEntrants(false));
    }

    if (elSetPreBtn) {
      elSetPreBtn.addEventListener("click", () => postFlag("pre"));
    }

    if (elGoGreenBtn) {
      elGoGreenBtn.addEventListener("click", () => postFlag("green"));
    }
  }

  // ---------------------------------------------------------------------------
  // YAML bootstrap (event + modes)
  // ---------------------------------------------------------------------------
  async function bootstrapFromYaml() {
    // We try relative paths first (same-origin), then fall back to host-resolved
    // URLs. This is friendlier to both the browser-served UI and desktop modes.
    const eventPaths = ["/config/event.yaml", CCRS.url("/config/event.yaml")];
    const modePaths  = ["/config/race_modes.yaml", CCRS.url("/config/race_modes.yaml")];

    EVENT = await loadYamlFirstAvailable(eventPaths).catch(() => null);
    MODES = await loadYamlFirstAvailable(modePaths).catch(() => null);

    // Render event and modes if available
    if (EVENT && EVENT.event) renderEventPreview(EVENT.event);
    if (MODES && MODES.modes) populateModes(MODES.modes);
  }

  async function loadYamlFirstAvailable(paths) {
    if (!Array.isArray(paths) || !paths.length) throw new Error("no YAML paths");
    let lastErr = null;
    for (const p of paths) {
      try {
        const text = await fetchText(p);
        const doc = parseYaml(text);
        if (doc) return doc;
      } catch (e) {
        lastErr = e;
        // try next
      }
    }
    throw (lastErr || new Error("YAML load failed"));
  }

  function parseYaml(text) {
    if (!text || !Y) return null;
    try {
      return Y.load(text);
    } catch (e) {
      console.warn("YAML parse error", e);
      return null;
    }
  }

  async function fetchText(path) {
    // If the path already has http(s) or starts with "/", just fetch it.
    // Otherwise, resolve via CCRS.url (host-driven).
    const isAbs = /^https?:/i.test(path) || path.startsWith("/");
    const u = isAbs ? path : CCRS.url(path);
    const r = await fetch(u, { cache: "no-cache" });
    if (!r.ok) throw new Error(`HTTP ${r.status} for ${u}`);
    return r.text();
  }

  // ---------------------------------------------------------------------------
  // UI rendering - Event + Mode previews
  // ---------------------------------------------------------------------------
  function renderEventPreview(ev) {
    if (!elEventPreview || !ev) return;
    const name = ev.name || "";
    const date = ev.date || "";
    const location = ev.location || "";
    const tz = ev.timezone || "";

    elEventPreview.innerHTML = `
      <div class="kv">
        <div class="k">Event</div><div class="v">${escapeHtml(name)}</div>
      </div>
      <div class="kv">
        <div class="k">Date</div><div class="v">${escapeHtml(date)}</div>
      </div>
      <div class="kv">
        <div class="k">Location</div><div class="v">${escapeHtml(location)}</div>
      </div>
      <div class="kv">
        <div class="k">Timezone</div><div class="v">${escapeHtml(tz)}</div>
      </div>`;
  }

  function populateModes(modesByKey) {
    if (!elModeSelect || !modesByKey) return;
    const keys = Object.keys(modesByKey);
    if (!keys.length) return;

    // Build <option> list using label if present
    elModeSelect.innerHTML = keys.map(k => {
      const m = modesByKey[k] || {};
      const label = (m.label || k);
      return `<option value="${escapeHtml(k)}">${escapeHtml(label)}</option>`;
    }).join("");

    // Render preview for the first (or currently-selected) mode
    const preferred = getSelectedModeKey() || keys[0];
    setSelectedModeKey(preferred);
    renderModePreview(preferred);
  }

  function renderModePreview(key) {
    if (!elModePreview || !MODES || !MODES.modes) return;
    const m = MODES.modes[key] || {};
    const label = m.label || key;
    const minLap = m.min_lap_s != null ? Number(m.min_lap_s) : null;
    const limit = m.limit || {}; // { type: 'time'|'laps', value: N }

    let capText = "";
    if (limit && limit.type === "time") {
      capText = `${Math.floor(Number(limit.value || 0)/60)} min cap`;
    } else if (limit && limit.type === "laps") {
      capText = `${Number(limit.value || 0)} laps cap`;
    }

    // Render compact key values with human-readable labels
    elModePreview.innerHTML = `
      <div class="kv">
        <div class="k">Mode</div><div class="v">${escapeHtml(label)}</div>
      </div>
      <div class="kv">
  <div class="k">Min Lap</div><div class="v">${minLap != null ? `${minLap}s` : "-"}</div>
      </div>
      <div class="kv">
  <div class="k">Limit</div><div class="v">${escapeHtml(capText || "-")}</div>
      </div>`;
  }

  // Helpers for the <select>
  function getSelectedModeKey() {
    return elModeSelect && elModeSelect.value ? elModeSelect.value : "";
  }
  function setSelectedModeKey(k) {
    if (!elModeSelect) return;
    const opt = Array.from(elModeSelect.options).find(o => o.value === k);
    if (opt) elModeSelect.value = k;
  }

  // ---------------------------------------------------------------------------
  // Entrants - from /race/state or pasted JSON
  // ---------------------------------------------------------------------------
  async function useEnabledEntrantsFromState() {
    try {
      CCRS.setNetStatus("connecting", elNetMsg, elNetDot);
      const snapshot = await CCRS.fetchJSON("/race/state");
      CCRS.setNetStatus("ok", elNetMsg, elNetDot);

      // Different engines expose entrants differently; we try common fields.
      const list =
        (snapshot && snapshot.entrants_enabled) ||
        (snapshot && snapshot.entrants) ||
        [];

      // Normalize to a compact JSON array so ops can glance/edit if needed
      const simplified = list.map(e => ({
        id: e.id ?? e.entrant_id ?? undefined,
        number: e.number ?? e.car ?? e.car_num ?? undefined,
        name: e.name ?? e.team ?? e.driver ?? "",
        tag: e.tag ?? e.transponder ?? undefined,
        enabled: e.enabled !== false
      }));

      elEntrantsTextarea.value = JSON.stringify(simplified, null, 2);
      elEntrantsTextarea.focus();
      elEntrantsTextarea.setSelectionRange(0, 0);
    } catch (e) {
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    }
  }

  function insertSampleEntrantsJson() {
    const sample = [
      { number: 2,  name: "Blue Shells",      tag: "1234567", enabled: true  },
      { number: 12, name: "Thunder Lizards",  tag: "2345678", enabled: true  },
      { number: 42, name: "Byte Me",          tag: "3456789", enabled: true  }
    ];
    elEntrantsTextarea.value = JSON.stringify(sample, null, 2);
    elEntrantsTextarea.focus();
    elEntrantsTextarea.setSelectionRange(0, 0);
  }

  // ---------------------------------------------------------------------------
  // Submit entrants to engine (/engine/load)
  // ---------------------------------------------------------------------------
  async function submitEntrants(merge) {
    const modeKey = getSelectedModeKey();
    const payload = parseEntrantsFromTextarea();
    if (!Array.isArray(payload)) {
  flashTextareaError("Invalid JSON - please paste an array of entrants.");
      return;
    }

    try {
      CCRS.setNetStatus("connecting", elNetMsg, elNetDot);

      // Body schema:
      //   entrants: [ {number, name, tag, enabled?} ... ]
      //   race_type: <mode key>  (so engine applies mode limits/labels)
      //   merge: true|false      (engine interprets replace/merge)
      const body = {
        entrants: payload,
        race_type: modeKey || undefined,
        merge: !!merge
      };

      const res = await fetch(CCRS.url("/engine/load"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      // Optional: read a small diff summary if the backend returns one
      // (safe if response has no body)
      let diff = "";
      try {
        const j = await res.json();
        if (j && typeof j === "object") {
          const add = j.added ?? j.add ?? 0;
          const upd = j.updated ?? j.upd ?? 0;
          const rem = j.removed ?? j.rem ?? 0;
          diff = `Loaded entrants - +${add} new, ${upd} updated, ${rem} removed.`;
        }
      } catch {
        diff = merge ? "Loaded entrants (merged)." : "Loaded entrants (replaced).";
      }

      annotateSuccess(diff);
      CCRS.setNetStatus("ok", elNetMsg, elNetDot);
    } catch (e) {
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    }
  }

  function parseEntrantsFromTextarea() {
    const raw = (elEntrantsTextarea && elEntrantsTextarea.value) ? elEntrantsTextarea.value.trim() : "";
    if (!raw) return [];
    try {
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return null;
      // Quick normalization: keep only common fields if operator pasted extras
      return arr.map(e => ({
        id:      e.id ?? undefined,
        number:  e.number ?? e.car ?? e.car_num ?? undefined,
        name:    e.name ?? e.team ?? e.driver ?? "",
        tag:     e.tag ?? e.transponder ?? undefined,
        enabled: e.enabled !== false
      }));
    } catch {
      return null;
    }
  }

  function flashTextareaError(msg) {
    if (!elEntrantsTextarea) return;
    elEntrantsTextarea.classList.add("error");
    elEntrantsTextarea.setAttribute("aria-invalid", "true");
    elEntrantsTextarea.title = msg;
    setTimeout(() => {
      elEntrantsTextarea.classList.remove("error");
      elEntrantsTextarea.removeAttribute("aria-invalid");
      elEntrantsTextarea.title = "";
    }, 1500);
  }

  function annotateSuccess(msg) {
    const el = $("#loadResult");
    if (!el) return;
    el.textContent = msg || "Loaded.";
    el.classList.add("pulse");
    setTimeout(() => el.classList.remove("pulse"), 1200);
  }

  // ---------------------------------------------------------------------------
  // Flags - /engine/flag pre/green helpers
  // ---------------------------------------------------------------------------
  async function postFlag(flag) {
    try {
      CCRS.setNetStatus("connecting", elNetMsg, elNetDot);
      const res = await fetch(CCRS.url("/engine/flag"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ flag })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      CCRS.setNetStatus("ok", elNetMsg, elNetDot);
    } catch (e) {
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    }
  }

  // ---------------------------------------------------------------------------
  // Utils
  // ---------------------------------------------------------------------------
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
})();
