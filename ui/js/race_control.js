/* ==========================================================================
   CCRS Race Control — unified controller
   --------------------------------------------------------------------------
   - Polls /race/state once per second and renders clock/phase/flag.
   - Wires action buttons to backend (/race/control/*, /engine/flag).
   - Shows a live "Last lap feed" by diffing standings and appending entries.
   - Keeps flag pad visually synced and enforces legal-press policy.
   - Uses server-provided clock_ms when present; falls back to local calc.
   ========================================================================== */

(() => {
  'use strict';

  // ----------------------------------------------------------------------
  // Tiny DOM + fetch helpers
  // ----------------------------------------------------------------------
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  async function api(url, opts = {}) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    try { return await r.json(); } catch { return {}; }
  }

  function fmtClockHMS(sec) {
    const neg = sec < 0;
    const s = Math.abs(Math.floor(sec || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const sign = neg ? '–' : '';
    return `${sign}${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(r).padStart(2,'0')}`;
  }

  function fmtLapTime(sec) {
    if (sec == null) return '—';
    // m:ss.mmm where m can grow; .mmm is 3 digits
    const ms = Math.round((sec - Math.floor(sec)) * 1000);
    const s  = Math.floor(sec) % 60;
    const m  = Math.floor(sec / 60);
    return `${m}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`;
  }


// ----------------------------------------------------------------------
// "Seen" panel rendering
// ----------------------------------------------------------------------
function seenCell(cls, text, style='') {
  const d = document.createElement('div');
  d.className = `cell ${cls}`;
  if (style) d.setAttribute('style', style);
  d.textContent = text;
  return d;
}

// Render the header labels into .liveHead (replaces the title during PRE/COUNTDOWN)
function setSeenHeader() {
  const head = document.querySelector('#panelSeen .liveHead');
  if (!head) return;
  head.replaceChildren(
    document.createTextNode('TAG'),
    seenCell('hdr num', 'Number'),
    seenCell('hdr name', 'Name'),
    seenCell('hdr reads', 'Reads', 'text-align:right;')
  );
}

// Build the rows from state.seen.rows
function renderSeen(state) {
  const ul    = document.getElementById('seenList');
  const cSpan = document.getElementById('seenCount');
  const tSpan = document.getElementById('seenTotal');
  if (!ul) return;

  const seen = state?.seen || { count:0, total:0, rows:[] };
  if (cSpan) cSpan.textContent = String(seen.count ?? 0);
  if (tSpan) tSpan.textContent = String(seen.total ?? 0);

  setSeenHeader();

  // Sort: enabled first, reads desc, then car number (stable)
  const rows = Array.isArray(seen.rows) ? [...seen.rows] : [];
  rows.sort((a,b) => {
    if (!!b.enabled !== !!a.enabled) return (b.enabled ? 1 : 0) - (a.enabled ? 1 : 0);
    const rb = Number(b.reads||0), ra = Number(a.reads||0);
    if (rb !== ra) return rb - ra;
    return String(a.car_number||'').localeCompare(String(b.car_number||''));
  });

  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const li = document.createElement('li');
    li.className = 'seenRow';

    const cTag   = seenCell('tag',   r.tag ?? '—');
    const cNum   = seenCell('num',   r.car_number ? `#${r.car_number}` : '—');
    const cName  = seenCell('name',  r.name ?? '');
    const cReads = seenCell('reads', String(r.reads ?? 0), 'text-align:right;');

    li.append(cTag, cNum, cName, cReads);
    frag.appendChild(li);
  }
  ul.replaceChildren(frag);
}


  // ----------------------------------------------------------------------
  // Local state
  // ----------------------------------------------------------------------
  let countdownAnchor = null; // epoch seconds when countdown hits 0 (client-side convenience)
  let lastState = null;

  // Track last known lap counts by entrant_id to emit feed lines only on increment
  const lastLapCounts = new Map(); // entrant_id -> laps

  // Persisted clock mode: 'remaining' | 'elapsed'
  let clockMode = (() => {
    const v = (localStorage.getItem('rc.clockMode') || 'remaining').toLowerCase();
    return (v === 'elapsed') ? 'elapsed' : 'remaining';
  })();

  // ----------------------------------------------------------------------
  // Element map
  // ----------------------------------------------------------------------
  const els = {
    // Action buttons
    btnPreRace     : $('#btnPreRace'),
    btnStartPrep   : $('#btnStartPrep'),
    btnStartRace   : $('#btnStartRace'),
    btnGoGreen     : $('#btnGoGreen'),
    btnEndRace     : $('#btnEndRace'),
    btnAbortReset  : $('#btnAbortReset'),
    btnAbortList   : $$('#btnAbort'), // some pages duplicate this id

    // Clock
    btnClockMode   : $('#btnClockMode'),
    clockDisplay   : $('#raceClock'),

    // Panels
    panelSeen      : $('#panelSeen'),
    panelFeed      : $('#panelFeed'),
    lapFeed        : $('#lapFeed'),       // <ul> for last-lap feed
    // seenList     : $('#seenList'),     // exists, not used in this step

    // Flag pad
    flagPad        : $('#flagPad'),
    preFlagRow     : $('#preFlagRow'),
  };

  // ----------------------------------------------------------------------
  // Allowed flag presses by phase + pad update (authoritative gate)
  // ----------------------------------------------------------------------
  function allowedFlagsForPhase(phase) {
    switch ((phase || 'pre').toLowerCase()) {
      case 'pre':
      case 'countdown':
        return ['pre'];
      case 'green':
        return ['yellow', 'red', 'blue', 'white', 'checkered'];
      case 'white':
        return ['yellow', 'red', 'checkered', 'blue'];
      case 'checkered':
      default:
        return [];
    }
  }

  function updateFlagPad(phase) {
    const allowed = new Set(allowedFlagsForPhase(phase));
    const allBtns = $$('#flagPad .flag, #preFlagRow .flag');
    allBtns.forEach(btn => {
      const name = (btn.dataset.flag || '').toLowerCase();
      const isAllowed = allowed.has(name);
      btn.disabled = !isAllowed;
      btn.setAttribute('aria-disabled', String(!isAllowed));
      btn.classList.toggle('is-disabled', !isAllowed);
    });
  }

  function highlightActiveFlagButton(flagLower) {
    const allBtns = $$('#flagPad .flag, #preFlagRow .flag');
    allBtns.forEach(btn => {
      btn.classList.toggle('is-active', (btn.dataset.flag || '').toLowerCase() === flagLower);
    });
  }

  function bindFlags() {
    const container = els.flagPad || document;
    container.addEventListener('click', (e) => {
      const btn = e.target.closest('.flag');
      if (!btn) return;
      if (btn.disabled || btn.classList.contains('is-disabled')) return;
      const f = (btn.dataset.flag || 'pre').toLowerCase();
      setActiveFlag(f);
    });
  }

  function updateClockModeButton(st) {
    const btn = els.btnClockMode;
    if (!btn) return;
    const ph = st?.phase;
    const remaining = st?.clock?.remaining_s;
    const disable = (ph === 'countdown') || (remaining == null);
    btn.disabled = !!disable;
    const label = (disable && remaining == null)
      ? 'Elapsed'
      : (clockMode === 'elapsed' ? 'Elapsed' : 'Remaining');
    btn.textContent = label;
  }

  // ----------------------------------------------------------------------
  // Lap feed: append on lap increments (GREEN/WHITE only)
  // ----------------------------------------------------------------------
  function appendLapFeedItem(row) {
    const ul = document.getElementById('lapFeed');
    if (!ul) return;

    // Build the styled row to match race_control.css
    const li   = document.createElement('li');
    li.className = 'liveRow';

    const num  = document.createElement('div');
    num.className = 'liveCell liveNum';
    num.textContent = row.car_number ? `#${row.car_number}` : (row.entrant_id ?? '');

    const name = document.createElement('div');
    name.className = 'liveCell liveName';
    name.textContent = row.name || '';

    const laps = document.createElement('div');
    laps.className = 'liveCell liveLaps';
    laps.textContent = `Lap ${Number(row.laps || 0)}`;

    const last = document.createElement('div');
    last.className = 'liveCell liveLast';
    last.textContent = `Last ${fmtLapTime(row.last)}`;

    const best = document.createElement('div');
    best.className = 'liveCell liveBest';
    best.textContent = `Best ${fmtLapTime(row.best)}`;

    li.append(num, name, laps, last, best);

    // Put newest on top
    ul.prepend(li);

    // Keep list from growing unbounded
    const maxItems = 25;
    while (ul.children.length > maxItems) {
      ul.removeChild(ul.lastElementChild);
    }
  }


  function updateLapFeed(st) {
    const ph = (st?.phase || 'pre').toLowerCase();
    if (!Array.isArray(st?.standings)) return;

    // Only emit feed during active racing phases
    const feedActive = (ph === 'green' || ph === 'white');
    if (!feedActive) return;

    for (const row of st.standings) {
      const id = row.entrant_id ?? row.tag ?? row.car_number ?? row.name;
      if (id == null) continue;
      const prev = lastLapCounts.get(id) ?? 0;
      const cur  = Number(row.laps || 0);
      if (cur > prev) {
        appendLapFeedItem(row);
        lastLapCounts.set(id, cur);
      } else if (!lastLapCounts.has(id)) {
        // Initialize the map on first sight so we don’t backfill old laps
        lastLapCounts.set(id, cur);
      }
    }
  }

  // ----------------------------------------------------------------------
  // Main state render
  // ----------------------------------------------------------------------
  function renderState(st) {
    if (!st) return;
    lastState = st;

    // ---- Clock ----
    updateClockModeButton(st);

    // Prefer authoritative server clock_ms if present (top-level or nested).
    const srvClockMs = (typeof st.clock_ms === 'number') ? st.clock_ms
                      : (st.clock && typeof st.clock.clock_ms === 'number') ? st.clock.clock_ms
                      : null;

    if (srvClockMs != null && els.clockDisplay) {
      els.clockDisplay.textContent = fmtClockHMS(srvClockMs / 1000);
    } else if ((st.phase === 'countdown') && countdownAnchor) {
      const now = Date.now() / 1000;
      const neg = Math.max(-(countdownAnchor - now), -Number(st.countdown_from_s || 0));
      if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(neg);
    } else {
      // Fallback to server-provided elapsed/remaining seconds if available
      const elapsed   = st?.clock?.elapsed_s ?? 0;
      const remaining = st?.clock?.remaining_s;
      if (remaining == null) {
        if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(elapsed);
      } else {
        const show = (clockMode === 'elapsed') ? elapsed : remaining;
        if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(show);
      }
    }

    // Initialize countdown anchor on first entry to COUNTDOWN
    const phaseLower = (st.phase || 'pre').toLowerCase();
    if (phaseLower === 'countdown' && !countdownAnchor) {
      const rem = Number(st.countdown_remaining_s ?? st.clock?.countdown_remaining_s ?? 0);
      if (rem > 0) {
        countdownAnchor = (Date.now() / 1000) + rem;
      }
    }
    // Clear anchor when we leave countdown
    if (phaseLower !== 'countdown') countdownAnchor = null;

    // ---- Phase/flag dataset + pad highlight ----
    document.body.dataset.phase = st.phase || 'pre';
    document.body.dataset.flag  = (st.flag || 'PRE').toUpperCase();

    // ---- Enable/disable action buttons ----
    const ph = st.phase;
    if (els.btnPreRace)      els.btnPreRace.disabled      = (ph !== 'pre');
    if (els.btnStartPrep)    els.btnStartPrep.disabled    = (ph !== 'pre');
    if (els.btnStartRace)    els.btnStartRace.disabled    = !(ph === 'pre' || ph === 'countdown');
    if (els.btnGoGreen)      els.btnGoGreen.disabled      = !(ph === 'pre' || ph === 'countdown');
    if (els.btnEndRace)      els.btnEndRace.disabled      = !(ph === 'green' || ph === 'white');
    if (els.btnAbortReset)   els.btnAbortReset.disabled   = false;
    if (els.btnAbortList?.length) els.btnAbortList.forEach(b => b.disabled = false);

    // ---- Panels: show feed only when racing or checkered ----
    const showFeed = (phaseLower === 'green' || phaseLower === 'white' || phaseLower === 'checkered');
    const seenPane = els.panelSeen || document.getElementById('panelSeen');
    const feedPane = els.panelFeed || document.getElementById('panelFeed');
    if (seenPane) seenPane.classList.toggle('hidden', showFeed);
    if (feedPane) feedPane.classList.toggle('hidden', !showFeed);

    if (showFeed) {
      updateLapFeed(st);
    } else {
      renderSeen(st);
    }

    // ---- Flags ----
    updateFlagPad(ph);
    highlightActiveFlagButton((st.flag || 'PRE').toLowerCase());
  }

  // ----------------------------------------------------------------------
  // Clock mode switch
  // ----------------------------------------------------------------------
  function setClockMode(mode) {
    clockMode = (mode === 'elapsed') ? 'elapsed' : 'remaining';
    localStorage.setItem('rc.clockMode', clockMode);
    if (lastState) renderState(lastState);
  }
  function toggleClockMode() {
    setClockMode(clockMode === 'elapsed' ? 'remaining' : 'elapsed');
  }

  // ----------------------------------------------------------------------
  // Control actions
  // ----------------------------------------------------------------------
  async function startPrep() {
    await api('/race/control/start_prep', { method: 'POST' });
    countdownAnchor = null;
    lastLapCounts.clear();
    renderState(await api('/race/state'));
  }

  async function startRace() {
    const res = await api('/race/control/start_race', { method: 'POST' });
    if ((res?.phase || '').toLowerCase() === 'countdown') {
      const cd = Number(res?.countdown_from_s || 0);
      if (cd > 0) countdownAnchor = (Date.now() / 1000) + cd;
    }
    lastLapCounts.clear(); // fresh feed for a new start
    renderState(await api('/race/state'));
  }

  async function endRace() {
    if (!confirm('End race and throw checkered?')) return;
    await api('/race/control/end_race', { method: 'POST' });
    renderState(await api('/race/state'));
  }

  async function abortReset() {
    if (!confirm('Abort & reset to PRE? Laps/seen will be cleared.')) return;
    try {
      await api('/race/control/abort_reset', { method: 'POST' });
    } catch (_) {
      await api('/race/reset_session', { method: 'POST' });
    }
    countdownAnchor = null;
    lastLapCounts.clear();
    renderState(await api('/race/state'));
  }

  async function setActiveFlag(flagLower) {
    const upper = String(flagLower || 'pre').toUpperCase();
    await api('/engine/flag', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flag: upper }),
    });
    renderState(await api('/race/state'));
  }

  function bindControls() {
    if (els.btnPreRace)    els.btnPreRace.addEventListener('click', startPrep);
    if (els.btnStartPrep)  els.btnStartPrep.addEventListener('click', startPrep);
    if (els.btnStartRace)  els.btnStartRace.addEventListener('click', startRace);
    if (els.btnGoGreen)    els.btnGoGreen.addEventListener('click', startRace);
    if (els.btnEndRace)    els.btnEndRace.addEventListener('click', endRace);
    if (els.btnAbortReset) els.btnAbortReset.addEventListener('click', abortReset);
    if (els.btnAbortList?.length) els.btnAbortList.forEach(b => b.addEventListener('click', abortReset));
    if (els.btnClockMode)  els.btnClockMode.addEventListener('click', toggleClockMode);

    bindFlags();

    // Keyboard: P,G,Y,R,B,W,C → set flag (policy enforced)
    document.addEventListener('keydown', (e) => {
      const map = { KeyP:'pre', KeyG:'green', KeyY:'yellow', KeyR:'red', KeyB:'blue', KeyW:'white', KeyC:'checkered' };
      const f = map[e.code];
      if (!f) return;
      e.preventDefault();
      const allowed = new Set(allowedFlagsForPhase(document.body.dataset.phase || 'pre'));
      if (!allowed.has(f)) return;
      setActiveFlag(f);
    });
  }

  // ----------------------------------------------------------------------
  // Poll loop
  // ----------------------------------------------------------------------
  let tick = null;
  function refreshState() {
    api('/race/state').then(renderState).catch(() => {/* ignore */});
  }
  function startPolling() {
    if (tick) clearInterval(tick);
    refreshState();
    tick = setInterval(refreshState, 1000);
  }

  // ----------------------------------------------------------------------
  // Boot
  // ----------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    bindControls();
    startPolling();
  });
})();
