/* ============================================================================
 * Operator — Entrants Admin
 * ----------------------------------------------------------------------------
 * Responsibilities:
 *  - List entrants (GET /admin/entrants)
 *  - Create/update/delete entrants (POST /admin/entrants, PUT/DELETE /admin/entrants/{id})
 *  - (Optional) Capture a transponder tag from the decoder and assign to current entrant
 *  - Show deterministic net-status copy and the effective engine host
 *
 * Dependencies:
 *  - base.js (window.CCRS: $, $$, fetchJSON, url, setNetStatus, startWallClock, etc.)
 *  - CSS: base.css + entrants.css (+ operator.css for shared form styles)
 *
 * Notes:
 *  - All API calls are routed through CCRS.url()/CCRS.fetchJSON(), so host is YAML-driven.
 *  - The capture helper tries a couple of common endpoints; if none exist, it no-ops with a
 *    friendly message so field ops aren’t blocked.
 * ==========================================================================*/

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Safe references + micro-helpers (don’t fight existing base.js helpers)
  // ---------------------------------------------------------------------------
  const CCRS = (window.CCRS = window.CCRS || {});
  const $ = CCRS.$ || ((sel, root) => (root || document).querySelector(sel));
  const $$ = CCRS.$$ || ((sel, root) => Array.from((root || document).querySelectorAll(sel)));

  // DOM cache (populated on DOMContentLoaded)
  let elRows, elTbody;
  let elId, elNumber, elName, elTag, elEnabled;
  let btnCreate, btnUpdate, btnDelete, btnCapture, btnRefresh;
  let chipAll, chipEnabled, chipDisabled;
  let elNetMsg, elNetDot, elEngineHost, elToast;

  // Local state
  let entrants = [];           // last fetched list (array of entrants)
  let selectedId = null;       // currently selected entrant id
  let filterMode = "all";      // all | enabled | disabled
  let captureTimer = null;     // capture polling handle

  // ---------------------------------------------------------------------------
  // DOM Ready
  // ---------------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", () => {
    // Cache elements
    elRows       = $("#entrantsTable") || document;
    elTbody      = $("#rows", elRows) || $("#rows");
    elId         = $("#id");
    elNumber     = $("#number");
    elName       = $("#name");
    elTag        = $("#tag");
    elEnabled    = $("#enabled");
    btnCreate    = $("#btnCreate");
    btnUpdate    = $("#btnUpdate");
    btnDelete    = $("#btnDelete");
    btnCapture   = $("#btnCapture");
    btnRefresh   = $("#btnRefresh");
    chipAll      = $("#filterAll");
    chipEnabled  = $("#filterEnabled");
    chipDisabled = $("#filterDisabled");
    elNetMsg     = $("#netMsg");
    elNetDot     = $("#netDot");
    elEngineHost = $("#engineHost");
    elToast      = $("#toast");

    // Footer wall clock (base.js)
    if (typeof CCRS.startWallClock === "function") CCRS.startWallClock();

    // Show effective engine host
    if (elEngineHost && typeof CCRS.effectiveEngineLabel === "function") {
      elEngineHost.textContent = CCRS.effectiveEngineLabel();
      elEngineHost.setAttribute("title", "Effective Engine Host");
    }

    // Initial net status
    CCRS.setNetStatus("connecting", elNetMsg, elNetDot);

    // Wire controls
    bindControls();

    // Initial load
    refreshEntrants();

    // Background refresh every ~10s (use makePoller if present to be polite)
    if (typeof CCRS.makePoller === "function") {
      CCRS.makePoller(refreshEntrants, 10000);
    } else {
      setInterval(refreshEntrants, 10000);
    }
  });

  // ---------------------------------------------------------------------------
  // UI Wiring
  // ---------------------------------------------------------------------------
  function bindControls() {
    // Table row selection (event delegation)
    if (elTbody) {
      elTbody.addEventListener("click", (ev) => {
        const tr = ev.target.closest("tr[data-id]");
        if (!tr) return;
        const id = tr.getAttribute("data-id");
        selectEntrant(id);
      });
    }

    // Create
    if (btnCreate) {
      btnCreate.addEventListener("click", async () => {
        const payload = readForm(false);
        if (!payload) return;
        await createEntrant(payload);
      });
    }

    // Update
    if (btnUpdate) {
      btnUpdate.addEventListener("click", async () => {
        const payload = readForm(true);
        if (!payload || !payload.id) return;
        await updateEntrant(payload.id, payload);
      });
    }

    // Delete
    if (btnDelete) {
      btnDelete.addEventListener("click", async () => {
        if (!selectedId) {
          toast("Select a row to delete.");
          return;
        }
        if (!confirm("Delete this entrant?")) return;
        await deleteEntrant(selectedId);
      });
    }

    // Capture loop
    if (btnCapture) {
      btnCapture.addEventListener("click", toggleCapture);
    }

    // Manual refresh
    if (btnRefresh) {
      btnRefresh.addEventListener("click", refreshEntrants);
    }

    // Filter chips
    if (chipAll)      chipAll.addEventListener("click", () => { setFilter("all");      renderEntrants(); });
    if (chipEnabled)  chipEnabled.addEventListener("click", () => { setFilter("enabled");  renderEntrants(); });
    if (chipDisabled) chipDisabled.addEventListener("click", () => { setFilter("disabled"); renderEntrants(); });
  }

  // ---------------------------------------------------------------------------
  // Filters
  // ---------------------------------------------------------------------------
  function setFilter(mode) {
    filterMode = mode;
    // update visual chip selection if present
    [chipAll, chipEnabled, chipDisabled].forEach(chip => chip && chip.classList.remove("active"));
    if (mode === "enabled" && chipEnabled) chipEnabled.classList.add("active");
    else if (mode === "disabled" && chipDisabled) chipDisabled.classList.add("active");
    else if (chipAll) chipAll.classList.add("active");
  }

  function applyFilter(list) {
    if (!Array.isArray(list)) return [];
    if (filterMode === "enabled")  return list.filter(e => e.enabled !== false);
    if (filterMode === "disabled") return list.filter(e => e.enabled === false);
    return list;
  }

  // ---------------------------------------------------------------------------
  // CRUD — /admin/entrants
  // ---------------------------------------------------------------------------
  async function refreshEntrants() {
    try {
      CCRS.setNetStatus("connecting", elNetMsg, elNetDot);
      const list = await CCRS.fetchJSON("/admin/entrants");
      entrants = Array.isArray(list) ? list : [];
      renderEntrants();
      CCRS.setNetStatus("ok", elNetMsg, elNetDot);
    } catch (e) {
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    }
  }

  function renderEntrants() {
    if (!elTbody) return;
    const rows = applyFilter(entrants);

    let html = "";
    for (let i = 0; i < rows.length; i++) {
      const e = rows[i] || {};
      const id = e.id ?? e.entrant_id ?? "";
      const number = e.number ?? e.car ?? e.car_num ?? "";
      const name = e.name ?? e.team ?? e.driver ?? "";
      const tag = e.tag ?? e.transponder ?? "";
      const enabled = e.enabled !== false;

      html += `
        <tr data-id="${escapeHtml(String(id))}" class="${enabled ? "" : "disabled"}">
          <td class="num">${escapeHtml(String(number))}</td>
          <td class="name">${escapeHtml(String(name))}</td>
          <td class="tag">${escapeHtml(String(tag))}</td>
          <td class="enabled">${enabled ? "Yes" : "No"}</td>
        </tr>`;
    }
    elTbody.innerHTML = html;

    // maintain selection highlight if present
    if (selectedId) {
      const tr = elTbody.querySelector(`tr[data-id="${cssEscape(selectedId)}"]`);
      if (tr) tr.classList.add("selected");
    }
  }

  function selectEntrant(id) {
    selectedId = id;

    // Highlight selection
    if (elTbody) {
      elTbody.querySelectorAll("tr.selected").forEach(tr => tr.classList.remove("selected"));
      const tr = elTbody.querySelector(`tr[data-id="${cssEscape(id)}"]`);
      if (tr) tr.classList.add("selected");
    }

    // Populate form
    const e = entrants.find(x => String(x.id ?? x.entrant_id) === String(id));
    if (!e) return;
    if (elId)      elId.value = String(e.id ?? e.entrant_id ?? "");
    if (elNumber)  elNumber.value = String(e.number ?? e.car ?? e.car_num ?? "");
    if (elName)    elName.value = String(e.name ?? e.team ?? e.driver ?? "");
    if (elTag)     elTag.value = String(e.tag ?? e.transponder ?? "");
    if (elEnabled) elEnabled.checked = e.enabled !== false;
  }

  async function createEntrant(e) {
    try {
      CCRS.setNetStatus("connecting", elNetMsg, elNetDot);
      const res = await fetch(CCRS.url("/admin/entrants"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(slimEntrant(e))
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast("Entrant created.");
      clearForm();
      await refreshEntrants();
    } catch (err) {
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    }
  }

  async function updateEntrant(id, e) {
    try {
      CCRS.setNetStatus("connecting", elNetMsg, elNetDot);
      const res = await fetch(CCRS.url(`/admin/entrants/${encodeURIComponent(id)}`), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(slimEntrant(e))
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast("Entrant updated.");
      await refreshEntrants();
      selectEntrant(id);
    } catch (err) {
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    }
  }

  async function deleteEntrant(id) {
    try {
      CCRS.setNetStatus("connecting", elNetMsg, elNetDot);
      const res = await fetch(CCRS.url(`/admin/entrants/${encodeURIComponent(id)}`), {
        method: "DELETE"
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast("Entrant deleted.");
      clearForm();
      selectedId = null;
      await refreshEntrants();
    } catch (err) {
      CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
    }
  }

  function slimEntrant(e) {
    return {
      number:  numOrNull(e.number),
      name:    (e.name || "").trim(),
      tag:     (e.tag || "").trim(),
      enabled: e.enabled !== false
    };
  }

  // ---------------------------------------------------------------------------
  // Capture — assign the next scanned tag to the currently selected entrant
  // ---------------------------------------------------------------------------
  function toggleCapture() {
    if (!selectedId) {
      toast("Select an entrant row first.");
      return;
    }
    if (captureTimer) {
      stopCapture("Capture stopped.");
      return;
    }
    startCapture();
  }

  function startCapture() {
    // Visual affordance
    if (btnCapture) {
      btnCapture.classList.add("active");
      btnCapture.textContent = "Stop Capture";
    }
    // Poll every ~750ms for a scanned tag. We try common endpoints:
    //   1) /admin/capture           -> { tag: "1234567" }
    //   2) /engine/scan             -> { tag: "1234567" }
    //   3) /engine/last_pass        -> { tag: "1234567", ts: ... } (we’ll dedupe by ts)
    // If none exist, we stop and inform the operator politely.
    let lastSeen = "";
    const tick = async () => {
      try {
        let tag = "";
        // Prefer an admin capture endpoint if present
        try {
          const a = await CCRS.fetchJSON("/admin/capture");
          tag = (a && a.tag) ? String(a.tag) : "";
        } catch {}
        if (!tag) {
          try {
            const b = await CCRS.fetchJSON("/engine/scan");
            tag = (b && b.tag) ? String(b.tag) : "";
          } catch {}
        }
        if (!tag) {
          try {
            const c = await CCRS.fetchJSON("/engine/last_pass");
            tag = (c && c.tag) ? String(c.tag) : "";
            // If the endpoint includes ts and repeats the same tag endlessly, guard:
            if (c && c.ts && String(c.tag) === lastSeen) tag = "";
          } catch {}
        }

        if (tag && tag !== lastSeen) {
          lastSeen = tag;
          // Blink the tag field and fill it
          if (elTag) {
            elTag.value = tag;
            elTag.classList.add("blip");
            setTimeout(() => elTag && elTag.classList.remove("blip"), 600);
          }
          // Assign immediately to backend if such a route exists
          // (optional — if not present, operator can press "Update")
          try {
            await fetch(CCRS.url(`/engine/entrant/assign_tag`), {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ id: selectedId, tag })
            });
          } catch {}
        }

        CCRS.setNetStatus("ok", elNetMsg, elNetDot);
      } catch {
        CCRS.setNetStatus("disconnected", elNetMsg, elNetDot);
      }
    };

    // Use base.js poller if provided (handles jitter/backoff)
    if (typeof CCRS.makePoller === "function") {
      captureTimer = CCRS.makePoller(tick, 750);
    } else {
      captureTimer = setInterval(tick, 750);
    }
    toast("Capture started — scan a transponder.");
  }

  function stopCapture(msg) {
    if (captureTimer) {
      if (typeof captureTimer === "number") clearInterval(captureTimer);
      // CCRS.makePoller may return undefined; in that case tick stops when page unloads
      captureTimer = null;
    }
    if (btnCapture) {
      btnCapture.classList.remove("active");
      btnCapture.textContent = "Capture Tag";
    }
    if (msg) toast(msg);
  }

  // ---------------------------------------------------------------------------
  // Form helpers
  // ---------------------------------------------------------------------------
  function readForm(expectId) {
    const id = (elId && elId.value) ? elId.value.trim() : "";
    const number = (elNumber && elNumber.value) ? elNumber.value.trim() : "";
    const name = (elName && elName.value) ? elName.value.trim() : "";
    const tag = (elTag && elTag.value) ? elTag.value.trim() : "";
    const enabled = !!(elEnabled && elEnabled.checked);

    if (expectId && !id) {
      toast("No ID selected — click a row first.");
      return null;
    }
    if (!name && !number && !tag) {
      toast("Enter at least a name, number, or tag.");
      return null;
    }

    return {
      id: id || undefined,
      number: number ? Number(number) : null,
      name,
      tag,
      enabled
    };
  }

  function clearForm() {
    if (elId) elId.value = "";
    if (elNumber) elNumber.value = "";
    if (elName) elName.value = "";
    if (elTag) elTag.value = "";
    if (elEnabled) elEnabled.checked = true;
  }

  // ---------------------------------------------------------------------------
  // Small UI utilities
  // ---------------------------------------------------------------------------
  function toast(msg) {
    if (!elToast) {
      // fallback: title on netMsg
      if (elNetMsg) elNetMsg.title = msg;
      return;
    }
    elToast.textContent = msg || "";
    elToast.classList.add("show");
    setTimeout(() => elToast && elToast.classList.remove("show"), 1500);
  }

  function numOrNull(x) {
    const n = Number(x);
    return Number.isFinite(n) ? n : null;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // tiny CSS.escape fallback for attribute selectors
  function cssEscape(s) {
    try { return CSS && CSS.escape ? CSS.escape(String(s)) : String(s).replace(/"/g, '\\"'); }
    catch { return String(s).replace(/"/g, '\\"'); }
  }
})();
