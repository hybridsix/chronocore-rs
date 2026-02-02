/* ============================================================================
   CCRS Results & Exports — Robust Frontend
   ----------------------------------------------------------------------------
   What this file does:
   1) Loads a left-rail list of races (prefers /results/recent; falls back to /heats).
   2) Renders final results if available (/results/{id}); otherwise a live preview (/race/state).
   3) Wires CSV/JSON export buttons to backend endpoints.
   4) Never crashes if optional DOM nodes are missing — all selectors are guarded.

   Contract assumptions (backend):
   - /results/recent             -> {heats:[{heat_id, name, finished_utc, ...}, ...]}
   - /heats  OR /results/heats   -> {heats:[{heat_id, name, started_utc?, finished_utc?, status?}, ...]}
   - /results/{race_id}          -> { race_id, race_type, frozen_utc, duration_ms, entrants:[...] }
   - /results/{race_id}/laps     -> { race_id, laps: { "<entrant_id>": [ms,...], ... } }
   - /export/results_csv?race_id=ID
   - /export/laps_csv?race_id=ID
   ============================================================================ */

(() => {
  'use strict';

  // ---------------------------------------------------------------------------
  // Lightweight DOM helpers (all null-safe by design)
  // ---------------------------------------------------------------------------
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // References (optional: these may be missing in early stubs)
  const railEmpty = $('#railEmpty');
  const heatListEl = $('#heatList');
  const btnRefresh = $('#btnRefreshHeats');

  const heatTitleEl = $('#heatTitle');
  const heatWindowEl = $('#heatWindow');
  const chipFrozenEl = $('#chipFrozen');
  const chipLiveEl = $('#chipLive');
  const chipGridEl = $('#chipGrid');

  const statFastEl = $('#statFast');
  const statCarsEl = $('#statCars');

  const tabsEl = $('#tabs');
  const tbodyStandings = $('#tbodyStandings');
  const standingsEmpty = $('#standingsEmpty');
  const tbodyLaps = $('#tbodyLaps');
  const lapsEmpty = $('#lapsEmpty');

  const btnStandingsCSV = $('#btnStandingsCSV');
  const btnStandingsJSON = $('#btnStandingsJSON');
  const btnLapsCSV = $('#btnLapsCSV');
  const btnLapsJSON = $('#btnLapsJSON');
  const btnPassesCSV = $('#btnPassesCSV'); // kept disabled unless journaling is on

  // State
  let heats = [];                // normalized heats list for the rail
  let selectedRaceId = null;     // currently viewed race id (heat_id)

  // ---------------------------------------------------------------------------
  // Fetch JSON helper with explicit errors (no silent failures).
  // ---------------------------------------------------------------------------
  async function getJSON(url) {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Request failed ${res.status} for ${url}`);
    return res.json();
  }

  // ---------------------------------------------------------------------------
  // ID parsers
  // ---------------------------------------------------------------------------
  function toId(v) {
    if (v == null) return null;
    const n = parseInt(String(v).trim(), 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }

  function getRaceIdFromPage() {
    // Query params first (?race= / ?race_id= / ?raceId=)
    const params = new URLSearchParams(window.location.search);
    const direct =
      toId(params.get('race')) ||
      toId(params.get('race_id')) ||
      toId(params.get('raceId'));
    if (direct) return direct;

    // data-race-id on any element
    const el = document.querySelector('[data-race-id]');
    if (el) {
      const viaAttr = toId(el.getAttribute('data-race-id'));
      if (viaAttr) return viaAttr;
    }

    // Hash fallback (#race=…)
    if (location.hash && location.hash.includes('=')) {
      const hs = new URLSearchParams(location.hash.slice(1));
      const viaHash =
        toId(hs.get('race')) ||
        toId(hs.get('race_id')) ||
        toId(hs.get('raceId'));
      if (viaHash) return viaHash;
    }

    // Don't use localStorage as fallback - let user pick from heats list
    // (Old race IDs from localStorage may no longer exist)
    return null;
  }

  // ---------------------------------------------------------------------------
  // Utilities: ms → "s.mmm" text; ISO trimming
  // ---------------------------------------------------------------------------
  const fmtSec = (ms) => (ms == null ? '' : (Number(ms) / 1000).toFixed(3));

  function toLocalIso(dt) {
    const pad = (n) => String(n).padStart(2, '0');
    const offMin = -new Date(dt).getTimezoneOffset();
    const sign = offMin >= 0 ? '+' : '-';
    const hh = pad(Math.floor(Math.abs(offMin) / 60));
    const mm = pad(Math.abs(offMin) % 60);
    const d = new Date(dt);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${sign}${hh}:${mm}`;
  }

  // Pick the best time present on a heat object
  function heatDisplayTime(h) {
    // Preferred: backend-computed local ISO with offset
    const raw =
      h?.display_time ??
      h?.frozen_iso_local ??
      h?.finished_utc ??
      h?.frozen_iso_utc ??
      h?.frozen_utc ?? null;

    // compactIso() is your formatter; if missing, just pass raw through
    try {
      return raw ? (typeof compactIso === 'function' ? compactIso(raw) : String(raw)) : '';
    } catch {
      return raw ? String(raw) : '';
    }
  }

  function safeText(v, fallback = '—') {
    return (v === null || v === undefined || v === '') ? fallback : String(v);
  }

  function safeNumber(v, fallback = 0) {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  }

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------
  function orDash(v) {
    return (v === null || v === undefined || v === '') ? '-' : v;
  }

  function compactIso(iso) {
    // "2025-11-01T01:45:41-04:00" → "2025-11-01 01:45:41 -04:00"
    if (!iso) return '-';
    const str = String(iso);
    const parts = str.split('T');
    if (parts.length !== 2) return str;
    const timePart = parts[1];
    const offsetMatch = timePart.match(/^([0-9:.]+)([+-].+|Z)$/);
    if (offsetMatch) {
      const time = offsetMatch[1];
      const offset = offsetMatch[2] === 'Z' ? ' Z' : ` ${offsetMatch[2]}`;
      return `${parts[0]} ${time}${offset}`;
    }
    return `${parts[0]} ${timePart}`;
  }

  // Fallback toast so we never silently fail
  function toast(msg) { try { CCRS?.toast?.(msg); } catch { console.log(msg); } }

  // Keep admin buttons enabled only when a heat is selected
  function updateAdminEnabled() {
    const hasSel = !!selectedRaceId;
    const ids = ['btnCopyLinks', 'btnDeleteHeat'];
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = !hasSel;
    });
  }

  // ---------------------------------------------------------------------------
  // View Mode Helpers (Frozen vs Live)
  // ---------------------------------------------------------------------------
  let viewMode = 'frozen'; // 'frozen' | 'live'

  async function hasFrozenResult(raceId) {
    try {
      const res = await fetch(`/results/${raceId}`, { cache: 'no-store' });
      return res.ok;
    } catch { return false; }
  }

  async function hasLiveFor(raceId) {
    try {
      const res = await fetch(`/race/state`, { cache: 'no-store' });
      if (!res.ok) return false;
      const js = await res.json();
      return String(js?.race_id) === String(raceId);
    } catch { return false; }
  }

  function setPillState({ frozenEnabled, liveEnabled }) {
    const pFrozen = document.getElementById('pillFrozen');
    const pLive = document.getElementById('pillLive');
    const hint = document.getElementById('viewHint');

    if (pFrozen) {
      pFrozen.disabled = !frozenEnabled;
      pFrozen.classList.toggle('is-on', viewMode === 'frozen');
      pFrozen.setAttribute('aria-selected', viewMode === 'frozen' ? 'true' : 'false');
    }
    if (pLive) {
      pLive.disabled = !liveEnabled;
      pLive.classList.toggle('is-on', viewMode === 'live');
      pLive.setAttribute('aria-selected', viewMode === 'live' ? 'true' : 'false');
    }
    if (hint) {
      if (viewMode === 'frozen') {
        hint.textContent = frozenEnabled ? 'Showing frozen results (authoritative).' : 'No frozen results available.';
      } else {
        hint.textContent = liveEnabled ? 'Live preview from engine (not final).' : 'Live preview not available for this heat.';
      }
    }
  }

  async function chooseDefaultView(raceId) {
    const frozen = await hasFrozenResult(raceId);
    const live = await hasLiveFor(raceId);
    viewMode = frozen ? 'frozen' : (live ? 'live' : 'frozen');
    setPillState({ frozenEnabled: frozen, liveEnabled: live });
    return { frozen, live };
  }

  async function renderFinalOrLive(raceId) {
    // Clear chips first
    if (chipFrozenEl) chipFrozenEl.hidden = true;
    if (chipLiveEl) chipLiveEl.hidden = true;

    // Check availability
    const frozenAvail = await hasFrozenResult(raceId);
    const liveAvail = await hasLiveFor(raceId);
    setPillState({ frozenEnabled: frozenAvail, liveEnabled: liveAvail });

    if (viewMode === 'frozen' && frozenAvail) {
      if (chipFrozenEl) chipFrozenEl.hidden = false;
      try {
        const [meta, laps] = await Promise.all([
          getJSON(`/results/${raceId}`),
          getJSON(`/results/${raceId}/laps`)
        ]);

        // Title + window info
        const titleParts = [];
        if (meta?.race_type) titleParts.push(meta.race_type.charAt(0).toUpperCase() + meta.race_type.slice(1));
        if (meta?.session_label) titleParts.push(meta.session_label);
        setTitle(titleParts.length > 0 ? titleParts.join(' • ') : `Race ${raceId}`);
        setWindow(`Frozen ${meta?.frozen_utc || ''} • Duration ${fmtDuration(meta?.duration_ms)}`);

        renderStandings(meta);
        renderLapsFromMap(laps?.laps || {});
        calcQuickStatsFromFinal(meta);
        return;
      } catch (err) {
        console.error('[Results] frozen fetch failed:', err);
      }
    }

    if (viewMode === 'live' && liveAvail) {
      if (chipLiveEl) chipLiveEl.hidden = false;
      try {
        const state = await getJSON(`/race/state`);

        // Title + window info
        const titleParts = [];
        if (state?.race_type) titleParts.push(state.race_type.charAt(0).toUpperCase() + state.race_type.slice(1));
        if (state?.session_label) titleParts.push(state.session_label);
        setTitle(titleParts.length > 0 ? titleParts.join(' • ') : `Race ${raceId}`);
        setWindow('Live preview - not final');

        renderStandings(liveToStandings(state));
        renderLapsFromMap(liveToLapMap(state));
        calcQuickStatsFromLive(state);
        return;
      } catch (err) {
        console.error('[Results] live fetch failed:', err);
      }
    }

    // Fallback: clear selection and UI
    selectedRaceId = null;
    setTitle('-');
    setWindow('-');
    renderStandings([]);
    renderLapsFromMap({});
  }

  // Normalize live state to frozen standings format
  function liveToStandings(state) {
    if (!state?.standings) return [];
    return state.standings.map(s => ({
      position: s.position || 0,
      entrant_id: s.entrant_id,
      number: s.number || '',
      name: s.name || '',
      tag: s.tag || '',
      laps: s.laps || 0,
      last_ms: s.last != null ? Math.round(s.last * 1000) : null,
      best_ms: s.best != null ? Math.round(s.best * 1000) : null,
      gap_ms: s.gap_s != null ? Math.round(s.gap_s * 1000) : null,
      lap_deficit: s.lap_deficit || 0,
      status: s.status || 'ACTIVE',
      enabled: s.enabled !== false
    }));
  }

  // Normalize live state to frozen lap map format
  function liveToLapMap(state) {
    if (!state?.standings) return {};
    const lapMap = {};
    state.standings.forEach(s => {
      if (s.lap_times && Array.isArray(s.lap_times)) {
        // lap_times is already in seconds, convert to ms
        lapMap[s.entrant_id] = s.lap_times.map(t => Math.round(t * 1000));
      }
    });
    return lapMap;
  }

  function wireViewPills() {
    const pFrozen = document.getElementById('pillFrozen');
    const pLive = document.getElementById('pillLive');
    if (pFrozen) pFrozen.addEventListener('click', async () => {
      if (viewMode === 'frozen') return;
      viewMode = 'frozen';
      setPillState({ frozenEnabled: !pFrozen.disabled, liveEnabled: !pLive?.disabled });
      if (selectedRaceId) await renderFinalOrLive(selectedRaceId);
    });
    if (pLive) pLive.addEventListener('click', async () => {
      if (viewMode === 'live') return;
      viewMode = 'live';
      setPillState({ frozenEnabled: !pFrozen?.disabled, liveEnabled: !pLive.disabled });
      if (selectedRaceId) await renderFinalOrLive(selectedRaceId);
    });
  }

  // Wire admin button handlers with event delegation
  function wireAdminButtons() {
    const rail = document.getElementById('railAdmin');
    if (!rail) { console.warn('railAdmin not found'); return; }

    rail.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button');
      if (!btn) return;

      console.log('[wireAdminButtons] Button clicked:', btn.id, 'selectedRaceId:', selectedRaceId);

      // No selection? Give a friendly nudge.
      if (!selectedRaceId && (btn.id === 'btnCopyLinks' || btn.id === 'btnDeleteHeat')) {
        toast('Select a heat first.');
        return;
      }

      try {
        if (btn.id === 'btnCopyLinks') {
          await copyLinksFor(selectedRaceId);
          return;
        }

        if (btn.id === 'btnDeleteHeat') {
          console.log('[wireAdminButtons] Calling deleteHeatById with:', selectedRaceId);
          await deleteHeatById(selectedRaceId);  // shows prompt internally
          await refreshHeats();                  // rebuild rail after delete
          // Clear main view if the selected heat is gone
          if (!heats.find(h => String(h.heat_id) === String(selectedRaceId))) {
            selectedRaceId = null;
            updateAdminEnabled();
            // Clear all displayed data
            try { clearHeatHeader(); } catch { }
            try { clearStandingsTable(); } catch { }
            try { clearLapsTable(); } catch { }
          }
          return;
        }

        if (btn.id === 'btnPurgeAll') {
          await purgeAllResults();               // double prompt internally
          await refreshHeats();
          selectedRaceId = null;
          updateAdminEnabled();
          try { clearHeatHeader(); } catch { }
          try { clearStandingsTable(); } catch { }
          try { clearLapsTable(); } catch { }
          return;
        }
      } catch (e) {
        toast(e?.message || 'Action failed.');
        console.warn(e);
      }
    });

    updateAdminEnabled();
  }

  // ---------------------------------------------------------------------------
  // Admin actions: copy links, delete heat, purge all
  // ---------------------------------------------------------------------------

  async function copyLinksFor(raceId) {
    if (!raceId) return;
    const base = window.location.origin;
    const text = [
      `${base}/results/${raceId}`,
      `${base}/results/${raceId}/laps`,
      `${base}/export/results_csv?race_id=${raceId}`,
      `${base}/export/laps_csv?race_id=${raceId}`,
    ].join('\n');

    try {
      await navigator.clipboard.writeText(text);
      toast('Links copied to clipboard.');
    } catch (e) {
      console.warn('Clipboard write failed:', e);
      toast('Failed to copy links.');
    }
  }

  async function deleteHeatById(raceId) {
    console.log('[deleteHeatById] Called with raceId:', raceId);
    if (!raceId) return;

    const heat = heats.find(h => String(h.heat_id) === String(raceId));
    const label = heat?.race_mode || heat?.name || `Heat ${raceId}`;

    console.log('[deleteHeatById] About to show confirm dialog for:', label);
    const confirmed = confirm(
      `Delete frozen results for "${label}" (race_id=${raceId})?\n\n` +
      `This will permanently remove:\n` +
      `• Result metadata\n` +
      `• Standings snapshot\n` +
      `• Lap times\n\n` +
      `This does NOT affect live race data or entrants.`
    );

    if (!confirmed) return;

    const token = `heat-${raceId}`;
    const res = await fetch(`/results/${raceId}?confirm=${encodeURIComponent(token)}`, {
      method: 'DELETE',
    });

    if (!res.ok) {
      const err = await res.text().catch(() => `HTTP ${res.status}`);
      throw new Error(`Delete failed: ${err}`);
    }

    const data = await res.json();
    toast(`Deleted: ${data.meta || 0} meta, ${data.standings || 0} standings, ${data.laps || 0} laps.`);
  }

  async function purgeAllResults() {
    const firstConfirm = confirm(
      `⚠️ DELETE ALL FROZEN RESULTS?\n\n` +
      `This will permanently erase ALL frozen heats from the database.\n\n` +
      `Live race data and entrants are NOT affected.\n\n` +
      `Click OK to proceed to final confirmation.`
    );

    if (!firstConfirm) return;

    const secondConfirm = confirm(
      `🚨 FINAL WARNING 🚨\n\n` +
      `Type the word "PURGE-ALL-RESULTS" in the next prompt to confirm.\n\n` +
      `This action CANNOT be undone.`
    );

    if (!secondConfirm) return;

    const token = prompt('Type exactly: PURGE-ALL-RESULTS');
    if (token !== 'PURGE-ALL-RESULTS') {
      toast('Purge cancelled (incorrect confirmation).');
      return;
    }

    const res = await fetch(`/results/?confirm=${encodeURIComponent(token)}`, {
      method: 'DELETE',
    });

    if (!res.ok) {
      const err = await res.text().catch(() => `HTTP ${res.status}`);
      throw new Error(`Purge failed: ${err}`);
    }

    const data = await res.json();
    toast(`Purged all results: ${data.meta || 0} meta, ${data.standings || 0} standings, ${data.laps || 0} laps.`);
  }


  // ---------------------------------------------------------------------------
  // Rail rendering (click-to-load). Accepts either recent or heats payloads.
  // ---------------------------------------------------------------------------
  function renderHeats(list) {
    const listEl = heatListEl;
    if (!listEl) return;

    heats = (Array.isArray(list) ? list : [])
      .map(h => {
        const id = toId(h?.heat_id ?? h?.race_id ?? h?.id);
        return {
          heat_id: id,
          name: h?.name || h?.race_type || (id ? `Race ${id}` : 'Race'),
          event_label: h?.event_label ?? null,
          session_label: h?.session_label ?? null,
          race_mode: h?.race_mode ?? h?.name ?? h?.race_type ?? null,
          display_time: h?.display_time ?? null,
          frozen_iso_local: h?.frozen_iso_local ?? null,
          finished_utc: h?.finished_utc ?? h?.frozen_iso_utc ?? null,
          frozen_utc: h?.frozen_utc ?? null,
        };
      })
      .filter(h => !!h.heat_id);

    const html = heats.map(h => {
      const isSelected = String(h.heat_id) === String(selectedRaceId);
      const eventLabel = safeText(h.event_label, '—');
      const sessionLabel = safeText(h.session_label, '—');
      const raceMode = safeText(h.race_mode, '—');
      const raceModeCapitalized = raceMode.charAt(0).toUpperCase() + raceMode.slice(1);
      const when = heatDisplayTime(h);

      // list-group-item + action gives you the clickable card feel
      // active provides the selected highlight (Bootstrap theme-aware)
      return `
    <button
      type="button"
      class="list-group-item list-group-item-action py-3 ${isSelected ? 'active' : ''}"
      data-heat-id="${h.heat_id}"
      aria-current="${isSelected ? 'true' : 'false'}"
      title="${raceModeCapitalized}"
    >
      <div class="d-flex justify-content-between align-items-start gap-2">
        <div class="fw-semibold text-truncate">${eventLabel}</div>
        <span class="badge ${isSelected ? 'text-bg-light' : 'text-bg-secondary'} flex-shrink-0">
          ${raceModeCapitalized}
        </span>
      </div>

      <div class="small ${isSelected ? 'text-white-50' : 'text-body-secondary'} text-truncate mt-1">
        ${sessionLabel}
      </div>

      <div class="mt-2 small ${isSelected ? 'text-white-50' : 'text-body-secondary'}">
        ${when}
      </div>
    </button>
  `;
    }).join('');


    listEl.innerHTML = html;

    // Delegated click
    listEl.onclick = (ev) => {
      const btn = ev.target.closest('.heat-card');
      if (!btn) return;
      const id = toId(btn.getAttribute('data-heat-id'));
      if (!id) return;
      const heat = heats.find(x => x.heat_id === id); // from full list, not sliced
      if (!heat) return;

      // Let selectHeat handle selectedRaceId and rendering
      selectHeat(heat);
    };

    railEmpty && (railEmpty.hidden = heats.length > 0);

    // Auto-select first if nothing selected
    if (!selectedRaceId && heats[0]) {
      selectedRaceId = heats[0].heat_id;
      selectHeat(heats[0]);
      const first = listEl.querySelector('.heat-card');
      if (first) {
        first.classList.add('heat-card--selected');
        first.setAttribute('aria-current', 'true');
      }
    }
  }


  // Select a heat (from object or id), update UI + exports, and remember it.
  function selectHeat(heatOrId) {
    // Accept either a heat object or a raw id
    const id = typeof heatOrId === 'number'
      ? heatOrId
      : toId(heatOrId?.heat_id ?? heatOrId?.race_id ?? heatOrId?.id);

    if (!id) return;
    // Allow re-selection to force render (removed early return)
    // if (id === selectedRaceId) return; // no-op if already selected

    selectedRaceId = id;

    // Persist selection for next visit/session
    try { localStorage.setItem('rc.race_id', String(id)); } catch { }

    // Reflect selection in the URL without reloading (supports ?race or ?race_id)
    try {
      const u = new URL(window.location.href);
      if (u.searchParams.has('race_id')) {
        u.searchParams.set('race_id', String(id));
      } else {
        u.searchParams.set('race', String(id));
      }
      history.replaceState(null, '', u.toString());
    } catch { }

    // Update the left-rail visual selection if that card exists
    if (heatListEl) {
      heatListEl.querySelectorAll('.heat-card').forEach(btn => {
        const btnId = toId(btn.getAttribute('data-heat-id'));
        const match = btnId === id;
        btn.classList.toggle('heat-card--selected', match);
        btn.setAttribute('aria-current', match ? 'true' : 'false');
        if (match) {
          // keep the selected card in view
          try { btn.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch { }
        }
      });
    }

    // Enable/disable rail admin buttons based on selection
    updateAdminEnabled();

    // Choose default view mode and render
    chooseDefaultView(id)
      .then(() => renderFinalOrLive(id))
      .then(() => wireExports(id))
      .catch(err => {
        console.warn(err);
        toast?.(err?.message || 'Failed to load results for selected heat.');
      });
  }


  // ---------------------------------------------------------------------------
  // Heats loader preference order: /results/recent → /heats → /results/heats
  // ---------------------------------------------------------------------------
  async function refreshHeats() {
    let payload;
    try {
      payload = await getJSON('/results/recent');
    } catch (eRecent) {
      try {
        payload = await getJSON('/heats');
      } catch (eHeats) {
        try {
          payload = await getJSON('/results/heats');
        } catch (ePrefixed) {
          if (railEmpty) railEmpty.hidden = false;
          if (heatListEl) heatListEl.innerHTML = '';
          console.warn('[Results] heats unavailable', eRecent, eHeats, ePrefixed);
          return;
        }
      }
    }

    const list = Array.isArray(payload) ? payload
      : (Array.isArray(payload?.heats) ? payload.heats : []);
    renderHeats(list);

    // Auto-select newest if nothing is selected
    if (!selectedRaceId && heats[0]) {
      selectHeat(heats[0]);
    }
  }

  // ---------------------------------------------------------------------------
  // Right pane: Final-first, Live-fallback (REPLACED BY NEW VIEW MODE SYSTEM)
  // ---------------------------------------------------------------------------
  // The old renderFinalOrLive function has been replaced by the new one above
  // that uses viewMode state and pills. Keeping this comment as a marker.

  // ---------------------------------------------------------------------------
  // Standings rendering (accepts frozen {entrants:[...]} or live {standings:[...]})
  // ---------------------------------------------------------------------------
  function renderStandings(payload) {
    if (!tbodyStandings) return;

    const rows = payload?.entrants || payload?.standings || [];
    if (standingsEmpty) standingsEmpty.hidden = rows.length > 0;

    const html = rows.map((e, i) => {
      // Normalize ms fields: prefer *_ms; else convert seconds.
      const bestMs = e.best_ms ?? (e.best != null ? Math.round(Number(e.best) * 1000) : null);
      const lastMs = e.last_ms ?? (e.last != null ? Math.round(Number(e.last) * 1000) : null);
      const gapMs = e.gap_ms ?? (e.gap_s != null ? Math.round(Number(e.gap_s) * 1000) : null);
      const paceMs = e.pace_5 != null ? Math.round(Number(e.pace_5) * 1000) : null;

      // Format brake status
      let brakeHtml = '-';
      if (e.brake_valid === true) {
        brakeHtml = '<span class="badge text-bg-success">Pass</span>';
      } else if (e.brake_valid === false) {
        brakeHtml = '<span class="badge text-bg-danger">Fail</span>';
      }

      return `
        <tr>
          <td>${e.position ?? (i + 1)}</td>
          <td>${e.number ?? ''}</td>
          <td>${e.name ?? ''}</td>
          <td>${e.laps ?? 0}</td>
          <td>${e.lap_deficit ?? 0}</td>
          <td>${fmtSec(lastMs)}</td>
          <td>${fmtSec(bestMs)}</td>
          <td>${e.grid_index ?? '-'}</td>
          <td>${brakeHtml}</td>
          <td>${e.pit_count ?? 0}</td>
        </tr>
      `;
    }).join('');

    tbodyStandings.innerHTML = html;
  }

  function clearStandingsTable() {
    if (tbodyStandings) tbodyStandings.innerHTML = '';
    if (standingsEmpty) standingsEmpty.hidden = false;
  }

  function clearHeatHeader() {
    setTitle('');
    setWindow('');
    if (chipFrozenEl) chipFrozenEl.textContent = '';
    if (chipLiveEl) chipLiveEl.textContent = '';
    if (chipGridEl) chipGridEl.textContent = '';
    if (statFastEl) statFastEl.textContent = '-';
    if (statCarsEl) statCarsEl.textContent = '-';
  }

  // ---------------------------------------------------------------------------
  // Laps rendering (frozen only)
  // ---------------------------------------------------------------------------
  async function renderLaps(raceId) {
    if (!tbodyLaps) return;
    try {
      const data = await getJSON(`/results/${raceId}/laps`);
      const map = data?.laps || {};
      renderLapsFromMap(map);
    } catch {
      clearLapsTable();
    }
  }

  function renderLapsFromMap(map) {
    if (!tbodyLaps) return;
    const rows = [];

    // Render by entrant, in ascending lap order, with simple cumulative.
    Object.entries(map).forEach(([entrantId, arr]) => {
      let cumul = 0;
      (arr || []).forEach((lapMs, idx) => {
        cumul += Number(lapMs) || 0;
        rows.push(`
          <tr>
            <td>${entrantId}</td>
            <td>${'' /* Name not present in this endpoint */}</td>
            <td>${idx + 1}</td>
            <td>${lapMs}</td>
            <td>${fmtSec(lapMs)}</td>
            <td>${cumul}</td>
            <td>${fmtSec(cumul)}</td>
            <td>${'' /* ts_ms not in frozen laps */}</td>
            <td>${'' /* UTC not in frozen laps */}</td>
            <td>${'' /* Flag */}</td>
            <td>${'' /* Src */}</td>
            <td>${'' /* Loc ID */}</td>
            <td>${'' /* Loc */}</td>
            <td>${'' /* Inf */}</td>
          </tr>
        `);
      });
    });

    tbodyLaps.innerHTML = rows.join('');
    if (lapsEmpty) lapsEmpty.hidden = rows.length > 0;
  }

  function clearLapsTable() {
    if (tbodyLaps) tbodyLaps.innerHTML = '';
    if (lapsEmpty) lapsEmpty.hidden = false;
  }

  // ---------------------------------------------------------------------------
  // Quick stats
  // ---------------------------------------------------------------------------
  function calcQuickStatsFromFinal(finalData) {
    const rows = finalData?.entrants || [];
    // Fast lap: min best_ms > 0
    const bests = rows.map(r => r.best_ms).filter(v => v != null && Number(v) > 0);
    const minBest = bests.length ? Math.min(...bests) : null;
    if (statFastEl) statFastEl.textContent = minBest != null ? `${fmtSec(minBest)}s` : '-';

    // Classified: entrants with laps > 0
    const total = rows.length;
    const classified = rows.filter(r => Number(r.laps) > 0).length;
    if (statCarsEl) statCarsEl.textContent = total ? `${classified}/${total}` : '-';
  }

  function calcQuickStatsFromLive(liveData) {
    const rows = liveData?.standings || [];
    const total = rows.length;
    const classified = rows.filter(r => Number(r.laps) > 0).length;
    if (statCarsEl) statCarsEl.textContent = total ? `${classified}/${total}` : '-';

    // Fast lap (live): best or best_ms
    const bests = rows.map(r => (r.best_ms ?? (r.best != null ? Math.round(Number(r.best) * 1000) : null)))
      .filter(v => v != null && Number(v) > 0);
    const minBest = bests.length ? Math.min(...bests) : null;
    if (statFastEl) statFastEl.textContent = minBest != null ? `${fmtSec(minBest)}s` : '-';
  }

  // ---------------------------------------------------------------------------
  // Header helpers
  // ---------------------------------------------------------------------------
  function setTitle(text) { if (heatTitleEl) heatTitleEl.textContent = text || '-'; }
  function setWindow(text) { if (heatWindowEl) heatWindowEl.textContent = text || '-'; }

  function fmtDuration(ms) {
    if (!Number.isFinite(ms)) return '-';
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
  }

  // ---------------------------------------------------------------------------
  // Tabs (simple toggle)
  // ---------------------------------------------------------------------------
  function wireTabs() {
    if (!tabsEl) return;
    tabsEl.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.tab');
      if (!btn) return;
      const tab = btn.getAttribute('data-tab');
      if (!tab) return;

      // Toggle active class on buttons
      $$('.tab', tabsEl).forEach(b => b.classList.toggle('is-active', b === btn));

      // Show/hide panels by ID
      $('#panel-standings')?.classList.toggle('is-hidden', tab !== 'standings');
      $('#panel-laps')?.classList.toggle('is-hidden', tab !== 'laps');
    });
  }

  // ---------------------------------------------------------------------------
  // Exports
  // ---------------------------------------------------------------------------
  function wireExports(raceId) {
    const bust = () => `?_=${Date.now()}`;

    if (btnStandingsCSV) {
      btnStandingsCSV.onclick = () => {
        if (!raceId) return;
        window.location.href = `/results/${raceId}/standings.csv${bust()}`;
      };
    }
    if (btnLapsCSV) {
      btnLapsCSV.onclick = () => {
        if (!raceId) return;
        window.location.href = `/results/${raceId}/laps.csv${bust()}`;
      };
    }
    if (btnStandingsJSON) {
      btnStandingsJSON.onclick = () => {
        if (!raceId) return;
        window.location.href = `/results/${raceId}/standings.json${bust()}`;
      };
    }
    if (btnLapsJSON) {
      btnLapsJSON.onclick = () => {
        if (!raceId) return;
        window.location.href = `/results/${raceId}/laps.json${bust()}`;
      };
    }
    // btnPassesCSV remains disabled unless you wire /passes.csv?heat_id=
  }

  // Wire the "Export All Heats" button
  function wireExportAll() {
    const btnExportAll = $('#btnExportAll');
    if (!btnExportAll) return;

    btnExportAll.onclick = () => {
      const bust = `?_=${Date.now()}`;
      window.location.href = `/results/export/all${bust}`;
    };
  }

  // ---------------------------------------------------------------------------
  // Health check for DB status indicator
  // ---------------------------------------------------------------------------
  async function checkBackendHealth() {
    try {
      const res = await fetch('/healthz', { cache: 'no-store' });
      if (res.ok && window.CCRS?.setNetStatus) {
        window.CCRS.setNetStatus(true, 'DB: Online');
      }
    } catch (err) {
      if (window.CCRS?.setNetStatus) {
        window.CCRS.setNetStatus(false, 'DB: Offline');
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Bootstrap
  // ---------------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', async () => {
    checkBackendHealth(); // Update DB status pill

    wireTabs();
    wireAdminButtons();
    wireExportAll();  // Wire the bulk export button

    // Pull a race id from the URL (or last selection)
    selectedRaceId = getRaceIdFromPage();

    // Fill rail (non-blocking)
    refreshHeats().catch(() => { /* rail is optional */ });

    // Wire view pills and set up view mode
    wireViewPills();
    if (selectedRaceId) {
      try {
        localStorage.setItem('rc.race_id', String(selectedRaceId));
      } catch { }

      // Enable admin buttons since we have a selection
      updateAdminEnabled();

      try {
        await chooseDefaultView(selectedRaceId);
        await renderFinalOrLive(selectedRaceId);
      } catch (err) {
        console.warn('[Results] Failed to load race, showing heats only:', err);
        // Clear the bad race_id from URL so user can select from list
        selectedRaceId = null;
        updateAdminEnabled(); // Disable buttons again
      }
    }

    // CSV/JSON buttons track the current selection
    wireExports(selectedRaceId);

    // Manual refresh button on the rail
    if (btnRefresh) btnRefresh.addEventListener('click', () => refreshHeats());
  });
})();
