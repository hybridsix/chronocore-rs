/* ============================================================================
   Results & Exports — Frontend logic (no framework, no ARIA)
   ============================================================================ */
(() => {
  'use strict';

  // ----- DOM helpers ---------------------------------------------------------
  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const API = {
    heats:               '/results/heats',
    summary:   (hid) =>  `/results/${hid}/summary`,
    laps:      (hid) =>  `/results/${hid}/laps`,
    export: {
      standingsCSV: (hid) => `/export/standings.csv?heat_id=${hid}`,
      standingsJSON:(hid) => `/export/standings.json?heat_id=${hid}`,
      lapsCSV:      (hid) => `/export/laps.csv?heat_id=${hid}`,
      lapsJSON:     (hid) => `/export/laps.json?heat_id=${hid}`,
      passesCSV:    (hid) => `/export/passes.csv?heat_id=${hid}`,
    },
    grid: {
      get:   (eid) => `/event/${eid}/qual`,
      freeze:(eid) => `/event/${eid}/qual/freeze`,
    },
    runtime: '/setup/runtime',
  };

  const heatListEl = $('#heatList');
  const railEmpty  = $('#railEmpty');

  const heatTitle  = $('#heatTitle');
  const heatWindow = $('#heatWindow');

  const chipFrozen = $('#chipFrozen');
  const chipLive   = $('#chipLive');
  const chipGrid   = $('#chipGrid');

  const statFast   = $('#statFast');
  const statCars   = $('#statCars');

  const freezePolicy = $('#freezePolicy');
  const btnFreeze    = $('#btnFreeze');

  const tabsButtons  = $$('.tab');
  const panelStand   = $('#panel-standings');
  const panelLaps    = $('#panel-laps');

  const tbodyStand   = $('#tbodyStandings');
  const tbodyLaps    = $('#tbodyLaps');
  const standingsEmpty = $('#standingsEmpty');
  const lapsEmpty      = $('#lapsEmpty');

  let heats = [];
  let selectedHeat = null;
  let journaling = false;

  // Buttons
  $('#btnStandingsCSV').addEventListener('click', () => openExport('standingsCSV'));
  $('#btnStandingsJSON').addEventListener('click', () => openExport('standingsJSON'));
  $('#btnLapsCSV').addEventListener('click', () => openExport('lapsCSV'));
  $('#btnLapsJSON').addEventListener('click', () => openExport('lapsJSON'));
  $('#btnPassesCSV').addEventListener('click', () => openExport('passesCSV'));
  $('#btnRefreshHeats').addEventListener('click', refreshHeats);

  // Tabs
  tabsButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      tabsButtons.forEach(b => b.classList.toggle('is-active', b === btn));
      const tab = btn.dataset.tab;
      panelStand.classList.toggle('is-hidden', tab !== 'standings');
      panelLaps.classList.toggle('is-hidden', tab !== 'laps');
      if (tab === 'laps' && selectedHeat) ensureLapsLoaded(selectedHeat.heat_id);
    });
  });

  // Freeze action
  btnFreeze.addEventListener('click', async () => {
    if (!selectedHeat) return;
    try {
      const body = JSON.stringify({
        source_heat_id: selectedHeat.heat_id,
        policy: freezePolicy.value || 'demote',
      });
      const res = await fetch(API.grid.freeze(selectedHeat.event_id), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      if (!res.ok) throw new Error(`Freeze failed ${res.status}`);
      await loadSummary(selectedHeat);
      toast('Grid frozen.');
    } catch (err) {
      toast(err.message || 'Freeze failed.');
    }
  });

  // Init
  init();
  async function init() {
    try {
      const rt = await getJSON(API.runtime);
      journaling = !!(rt && (rt.journaling_enabled || rt.journal_enabled));
      $('#btnPassesCSV').disabled = !journaling;
    } catch {
      journaling = false;
      $('#btnPassesCSV').disabled = true;
    }
    await refreshHeats();
  }

  // Heats
  async function refreshHeats() {
    try {
      heats = await getJSON(API.heats);
      renderHeats(heats);
      railEmpty.hidden = heats.length > 0;
      if (!selectedHeat || !heats.find(h => h.heat_id === selectedHeat.heat_id)) {
        if (heats[0]) selectHeat(heats[0]);
      }
    } catch (err) {
      railEmpty.hidden = false;
      heatListEl.innerHTML = '';
      toast(err.message || 'Failed to load heats.');
    }
  }

  function renderHeats(list) {
    heatListEl.innerHTML = '';
    for (const h of list) {
      const item = document.createElement('button');
      item.className = 'heat';
      if (selectedHeat && selectedHeat.heat_id === h.heat_id) item.classList.add('is-selected');
      item.innerHTML = `
        <div class="heat__name">${escapeHtml(h.name || '—')}</div>
        <div class="heat__count">${(h.laps_count ?? 0)} laps • ${(h.entrant_count ?? 0)} cars</div>
        <div class="heat__meta">${escapeHtml(h.status || '')}</div>
      `;
      item.addEventListener('click', () => selectHeat(h));
      heatListEl.appendChild(item);
    }
  }

  function selectHeat(h) {
    selectedHeat = h;
    $$('.heat', heatListEl).forEach(btn => btn.classList.remove('is-selected'));
    const idx = heats.findIndex(x => x.heat_id === h.heat_id);
    if (idx >= 0) {
      const btn = $$('.heat', heatListEl)[idx];
      if (btn) btn.classList.add('is-selected');
    }

    heatTitle.textContent = `${h.name || '—'}  (ID ${h.heat_id})`;
    heatWindow.textContent = formatWindow(h.started_utc, h.finished_utc);

    tabsButtons.forEach(b => b.classList.toggle('is-active', b.dataset.tab === 'standings'));
    panelStand.classList.remove('is-hidden');
    panelLaps.classList.add('is-hidden');

    loadSummary(h);
    tbodyLaps.innerHTML = '';
    lapsEmpty.hidden = true;
  }

  // Summary
  async function loadSummary(h) {
    try {
      const summary = await getJSON(API.summary(h.heat_id));
      let grid = null;
      try { grid = await getJSON(API.grid.get(h.event_id)); } catch {}

      chipFrozen.hidden = !summary.frozen;
      chipLive.hidden   = !!summary.frozen;

      if (grid && grid.frozen && grid.policy) {
        chipGrid.hidden = false;
        chipGrid.textContent = `Grid: Frozen (${grid.policy})`;
        btnFreeze.textContent = 'Re-freeze Grid';
        if (['demote','use_next_valid','exclude'].includes(grid.policy)) {
          freezePolicy.value = grid.policy;
        }
      } else {
        chipGrid.hidden = true;
        btnFreeze.textContent = 'Freeze Grid';
      }

      statFast.textContent = msToStr(summary.totals?.fastest_ms);
      statCars.textContent = String(summary.totals?.cars_classified ?? '—');

      renderStandings(summary.standings || []);
      standingsEmpty.hidden = (summary.standings || []).length > 0;
    } catch (err) {
      renderStandings([]);
      standingsEmpty.hidden = false;
      toast(err.message || 'Failed to load summary.');
    }
  }

  function renderStandings(rows) {
    let html = '';
    for (const r of rows) {
      html += `
        <tr>
          <td>${r.position ?? ''}</td>
          <td>${escapeHtml(r.number ?? '')}</td>
          <td>${escapeHtml(r.name ?? '')}</td>
          <td>${r.laps ?? 0}</td>
          <td>${r.lap_deficit ?? 0}</td>
          <td>${msToStr(r.last_ms)}</td>
          <td>${msToStr(r.best_ms)}</td>
          <td>${msToStr(r.pace_5_ms ?? r.best_ms)}</td>
          <td>${r.grid_index ?? ''}</td>
          <td>${r.brake_valid === false ? '✗' : '✓'}</td>
          <td>${r.pit_count ?? 0}</td>
        </tr>
      `;
    }
    tbodyStand.innerHTML = html;
  }

  // Laps
  let lapsLoadedFor = null;
  async function ensureLapsLoaded(heatId) {
    if (lapsLoadedFor === heatId) return;
    try {
      const rows = await getJSON(API.laps(heatId));
      lapsEmpty.hidden = rows.length > 0;
      tbodyLaps.innerHTML = '';

      const CHUNK = 400;
      let i = 0;
      (function appendChunk() {
        const end = Math.min(i + CHUNK, rows.length);
        let html = '';
        for (; i < end; i++) {
          const r = rows[i];
          html += `
            <tr>
              <td>${escapeHtml(r.number ?? '')}</td>
              <td>${escapeHtml(r.name ?? '')}</td>
              <td>${r.lap_num ?? ''}</td>
              <td>${r.lap_ms ?? ''}</td>
              <td>${msToStr(r.lap_ms)}</td>
              <td>${r.cumulative_ms ?? ''}</td>
              <td>${msToStr(r.cumulative_ms ?? null)}</td>
              <td>${r.ts_ms ?? ''}</td>
              <td>${r.ts_utc ? escapeHtml(r.ts_utc) : ''}</td>
              <td>${r.flag ?? ''}</td>
              <td>${r.source_id ?? ''}</td>
              <td>${escapeHtml(r.location_id ?? '')}</td>
              <td>${escapeHtml(r.location_label ?? '')}</td>
              <td>${r.inferred ?? 0}</td>
            </tr>
          `;
        }
        tbodyLaps.insertAdjacentHTML('beforeend', html);
        if (i < rows.length) requestAnimationFrame(appendChunk);
      })();

      lapsLoadedFor = heatId;
    } catch (err) {
      tbodyLaps.innerHTML = '';
      lapsEmpty.hidden = false;
      toast(err.message || 'Failed to load laps.');
    }
  }

  // Utilities
  async function getJSON(url, opts = {}) {
    const res = await fetch(url, { credentials: 'same-origin', ...opts });
    if (!res.ok) throw new Error(`Request failed ${res.status}`);
    return res.json();
  }

function msToStr(ms) {
  if (ms == null || isNaN(ms)) return '—';
  const m = Math.floor(ms / 60000);
  const s = ((ms % 60000) / 1000).toFixed(3).padStart(6, '0');
  return `${String(m).padStart(2, '0')}:${s}`;
}


  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function formatWindow(startIso, endIso) {
    const s = startIso ? new Date(startIso) : null;
    const e = endIso ? new Date(endIso) : null;
    if (!s && !e) return '—';
    const fmt = (d) => d.toLocaleString();
    return e ? `${fmt(s)} → ${fmt(e)}` : `${fmt(s)} → (running)`;
  }

  function openExport(kind) {
    if (!selectedHeat) return;
    window.open(API.export[kind](selectedHeat.heat_id), '_blank', 'noopener');
  }

  function toast(msg) {
    console.log('[Results]', msg);
    alert(msg); // minimal MVP toast; replace with your toast util when ready
  }
})();
