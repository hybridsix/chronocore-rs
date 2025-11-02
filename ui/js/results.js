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
  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // References (optional: these may be missing in early stubs)
  const railEmpty      = $('#railEmpty');
  const heatListEl     = $('#heatList');
  const btnRefresh     = $('#btnRefreshHeats');

  const heatTitleEl    = $('#heatTitle');
  const heatWindowEl   = $('#heatWindow');
  const chipFrozenEl   = $('#chipFrozen');
  const chipLiveEl     = $('#chipLive');
  const chipGridEl     = $('#chipGrid');

  const statFastEl     = $('#statFast');
  const statCarsEl     = $('#statCars');

  const tabsEl         = $('#tabs');
  const tbodyStandings = $('#tbodyStandings');
  const standingsEmpty = $('#standingsEmpty');
  const tbodyLaps      = $('#tbodyLaps');
  const lapsEmpty      = $('#lapsEmpty');

  const btnStandingsCSV  = $('#btnStandingsCSV');
  const btnStandingsJSON = $('#btnStandingsJSON');
  const btnLapsCSV       = $('#btnLapsCSV');
  const btnLapsJSON      = $('#btnLapsJSON');
  const btnPassesCSV     = $('#btnPassesCSV'); // kept disabled unless journaling is on

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

    // Last selection
    const ls = toId(localStorage.getItem('rc.race_id'));
    return ls || null;
  }

  // ---------------------------------------------------------------------------
  // Utilities: ms → "s.mmm" text; ISO trimming
  // ---------------------------------------------------------------------------
  const fmtSec = (ms) => (ms == null ? '' : (Number(ms) / 1000).toFixed(3));

  // ---------------------------------------------------------------------------
  // Helpers for rail text formatting
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

  // ---------------------------------------------------------------------------
  // Rail rendering (click-to-load). Accepts either recent or heats payloads.
  // ---------------------------------------------------------------------------
  function renderHeats(list) {
  const listEl = heatListEl;
  if (!listEl) return;

    heats = (Array.isArray(list) ? list : [])
      .map(h => ({
        heat_id:     toId(h?.heat_id ?? h?.race_id ?? h?.id),
        name:        h?.name || h?.race_type || `Race ${h?.heat_id ?? h?.race_id ?? h?.id ?? ''}`,
        event_label: h?.event_label ?? null,
        session_label: h?.session_label ?? null,
        race_mode:   h?.race_mode ?? h?.name ?? h?.race_type ?? null,
        frozen_iso_local: h?.frozen_iso_local ?? null,
        finished_utc:     h?.finished_utc ?? h?.frozen_iso_utc ?? null,
      }))
      .filter(h => !!h.heat_id);

    listEl.innerHTML = heats.map(h => {
      const isSelected = String(h.heat_id) === String(selectedRaceId);
      const eventLabel = orDash(h.event_label);
      const sessionLabel = orDash(h.session_label);
      const raceMode = orDash(h.race_mode || h.name);
      const when = compactIso(h.frozen_iso_local || h.finished_utc);

      return `
        <button class="heat-card${isSelected ? ' heat-card--selected' : ''}" data-heat-id="${h.heat_id}" title="${raceMode}" aria-current="${isSelected ? 'true' : 'false'}">
          <div class="heat-card__top">
            <span class="heat-card__event">${eventLabel}</span>
            <span class="heat-card__session">${sessionLabel}</span>
          </div>
          <div class="heat-card__mid">
            <span class="heat-card__mode">${raceMode}</span>
          </div>
          <div class="heat-card__bottom">
            <span class="heat-card__time">${when}</span>
          </div>
        </button>
      `;
    }).join('');

    listEl.querySelectorAll('.heat-card').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = toId(btn.getAttribute('data-heat-id'));
        if (!id) return;
        const heat = heats.find(x => x.heat_id === id);
        if (heat) selectHeat(heat);
      });
    });

    if (railEmpty) railEmpty.hidden = heats.length > 0;
  }

  function selectHeat(heat) {
    const id = heat ? toId(heat.heat_id ?? heat.race_id ?? heat.id) : null;
    if (!id) return;

    selectedRaceId = id;
    try { localStorage.setItem('rc.race_id', String(id)); } catch {}

    if (heatListEl) {
      heatListEl.querySelectorAll('.heat-card').forEach(btn => {
        const btnId = toId(btn.getAttribute('data-heat-id'));
        const match = btnId === id;
        btn.classList.toggle('heat-card--selected', match);
        btn.setAttribute('aria-current', match ? 'true' : 'false');
      });
    }

    renderFinalOrLive(id).catch(err => console.warn(err));
    wireExports(id);
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
  // Right pane: Final-first, Live-fallback
  // ---------------------------------------------------------------------------
  async function renderFinalOrLive(raceId) {
    // Clear chips first
    if (chipFrozenEl) chipFrozenEl.hidden = true;
    if (chipLiveEl)   chipLiveEl.hidden   = true;

    // Try frozen results
    try {
      const finalData = await getJSON(`/results/${raceId}`);
      if (chipFrozenEl) chipFrozenEl.hidden = false;

      // Title + window info
      setTitle(finalData?.race_type || `Race ${raceId}`);
      setWindow(`Frozen ${finalData?.frozen_utc || ''} • Duration ${fmtDuration(finalData?.duration_ms)}`);

      renderStandings(finalData);
      await renderLaps(raceId); // tolerant: empty if none persisted
      calcQuickStatsFromFinal(finalData);
      return;
    } catch {
      // Fall through to live preview
    }

    // Live preview
    try {
      const live = await getJSON(`/race/state?race_id=${raceId}`);
      if (chipLiveEl) chipLiveEl.hidden = false;

      setTitle(live?.race_type || `Race ${raceId}`);
  setWindow('Live preview - not final');

      renderStandings(live);
      clearLapsTable();
      calcQuickStatsFromLive(live);
    } catch (err) {
      console.warn('[Results] Unable to render final or live state', err);
      // Clear UI softly
      setTitle(`Race ${raceId}`);
      setWindow('Unavailable');
      clearStandingsTable();
      clearLapsTable();
    }
  }

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
      const gapMs  = e.gap_ms  ?? (e.gap_s != null ? Math.round(Number(e.gap_s) * 1000) : null);

      return `
        <tr>
          <td>${e.position ?? (i + 1)}</td>
          <td>${e.number ?? ''}</td>
          <td>${e.name ?? ''}</td>
          <td>${e.laps ?? 0}</td>
          <td>${e.lap_deficit ?? 0}</td>
          <td>${fmtSec(lastMs)}</td>
          <td>${fmtSec(bestMs)}</td>
          <td>${fmtSec(null) /* Pace-5 not yet computed */}</td>
          <td>${'' /* Grid placeholder */}</td>
          <td>${'' /* Brake placeholder */}</td>
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

  // ---------------------------------------------------------------------------
  // Laps rendering (frozen only)
  // ---------------------------------------------------------------------------
  async function renderLaps(raceId) {
    if (!tbodyLaps) return;
    try {
      const data = await getJSON(`/results/${raceId}/laps`);
      const map = data?.laps || {};
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
    } catch {
      clearLapsTable();
    }
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
  function setTitle(text)   { if (heatTitleEl)  heatTitleEl.textContent  = text || '-'; }
  function setWindow(text)  { if (heatWindowEl) heatWindowEl.textContent = text || '-'; }

  function fmtDuration(ms) {
    if (!Number.isFinite(ms)) return '-';
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${String(m).padStart(2,'0')}:${String(r).padStart(2,'0')}`;
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
    const bust = () => `&_=${Date.now()}`;

    if (btnStandingsCSV) {
      btnStandingsCSV.onclick = () => {
        if (!raceId) return;
        window.location.href = `/export/results_csv?race_id=${raceId}${bust()}`;
      };
    }
    if (btnLapsCSV) {
      btnLapsCSV.onclick = () => {
        if (!raceId) return;
        window.location.href = `/export/laps_csv?race_id=${raceId}${bust()}`;
      };
    }
    if (btnStandingsJSON) {
      btnStandingsJSON.onclick = () => {
        if (!raceId) return;
        window.open(`/results/${raceId}`, '_blank');
      };
    }
    if (btnLapsJSON) {
      btnLapsJSON.onclick = () => {
        if (!raceId) return;
        window.open(`/results/${raceId}/laps`, '_blank');
      };
    }
    // btnPassesCSV remains disabled unless you wire /passes.csv?heat_id=
  }

  // ---------------------------------------------------------------------------
  // Bootstrap
  // ---------------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', async () => {
    wireTabs();

    // Pull a race id from the URL (or last selection)
    selectedRaceId = getRaceIdFromPage();

    // Fill rail (non-blocking)
    refreshHeats().catch(() => { /* rail is optional */ });

    // If we have a race id, render it immediately, even if the rail is empty.
    if (selectedRaceId) {
      try { localStorage.setItem('rc.race_id', String(selectedRaceId)); } catch {}
      await renderFinalOrLive(selectedRaceId);
    }

    // CSV/JSON buttons track the current selection
    wireExports(selectedRaceId);

    // Manual refresh button on the rail
    if (btnRefresh) btnRefresh.addEventListener('click', () => refreshHeats());
  });
})();
