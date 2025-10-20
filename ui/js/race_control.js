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

    const rows = Array.isArray(seen.rows) ? [...seen.rows] : [];
    rows.sort((a,b) => {
      if (!!b.enabled !== !!a.enabled) return (b.enabled ? 1 : 0) - (a.enabled ? 1 : 0);
      const rb = Number(b.reads||0), ra = Number(a.reads||0);
      if (rb !== ra) return rb - ra;
      return String(a.number||'').localeCompare(String(b.number||''));
    });

    const frag = document.createDocumentFragment();
    for (const r of rows) {
      const li = document.createElement('li');
      li.className = 'seenRow';

      const cTag   = seenCell('tag',   r.tag ?? '—');
      const cNum   = seenCell('num',   r.number ? `#${r.number}` : '—');
      const cName  = seenCell('name',  r.name ?? '');
      const cReads = seenCell('reads', String(r.reads ?? 0), 'text-align:right;');

      // Add “Entrant Seen” pill when reads > 0
      if ((r.reads ?? 0) > 0) {
        const pill = document.createElement('span');
        pill.className = 'seen-pill';
        pill.textContent = 'Entrant Seen';
        cName.appendChild(pill);
      }

      li.append(cTag, cNum, cName, cReads);
      frag.appendChild(li);
    }
    ul.replaceChildren(frag);
  }

  // ----------------------------------------------------------------------
  // Race summary rendering
  // ----------------------------------------------------------------------

  function secondsToHuman(s) {
    const n = Number(s);
    if (!Number.isFinite(n) || n < 0) return '—';
    if (n === 0) return 'Unlimited';
    const m = Math.floor(n / 60);
    const ss = n % 60;
    return `${m}m ${ss}s`;
  }

  function summarizeFromState(st) {
    // Limit
    let limitStr = '—';
    const lim = st?.limit;
    if (lim?.type === 'time') limitStr = `Time ${secondsToHuman(lim.value_s)}`;
    else if (lim?.type === 'laps') limitStr = `Laps ${lim.value_laps}`;
    else if (lim?.type) limitStr = String(lim.type).toUpperCase();

    // Rank (for now mirror Setup’s “Total Laps”; change here if you add modes)
    const rankStr = 'Rank: Total Laps';

    // MinLap
    const minLap = st?.min_lap_s ?? st?.session?.min_lap_s ?? st?.engine?.min_lap_s;
    const minLapStr = (minLap != null) ? `MinLap ${Number(minLap).toFixed(1)}s` : null;

    const parts = [limitStr, rankStr];
    if (minLapStr) parts.push(minLapStr);
    return parts.join(' • ');
  }

  function renderRCSummary(st) {
    const el = document.getElementById('rcSummaryText');
    if (!el) return;
    el.textContent = summarizeFromState(st);
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



  // ----------------------------------------------------------------------
  // Flag banner update + pulse on change
  // ----------------------------------------------------------------------

  let lastFlagForPulse = null;

  function updateFlagBanner(st) {
    const banner = document.getElementById('flagBanner');
    if (!banner) return;

    const labelEl = document.getElementById('flagLabel');
    const subEl   = document.getElementById('flagSublabel');

    const flag = (st.flag || 'PRE').toUpperCase();
    const phase = (st.phase || 'pre').toLowerCase();

    // Human label + sublabel
    const labelMap = {
      PRE: 'Pre-race',
      GREEN: 'Green',
      YELLOW: 'Yellow',
      RED: 'Red',
      BLUE: 'Blue',
      WHITE: 'White',
      CHECKERED: 'Checkered'
    };
    labelEl.textContent = labelMap[flag] || flag;

    if (phase === 'countdown') {
      const rem = st?.clock?.countdown_remaining_s ?? st?.countdown_remaining_s;
      subEl.textContent = (rem != null) ? `Start in ${Math.max(0, Math.ceil(rem))}s` : 'Start armed';
    } else if (flag === 'GREEN') {
      subEl.textContent = 'Race in progress';
    } else if (flag === 'CHECKERED') {
      subEl.textContent = 'Race complete';
    } else {
      subEl.textContent = '';
    }

    // Pulse on change
    if (lastFlagForPulse !== flag) {
      banner.classList.remove('flag-pulse');
      // allow reflow then add (ensures animation retriggers)
      setTimeout(() => banner.classList.add('flag-pulse'), 0);
      lastFlagForPulse = flag;
    }
  }

  function lockFlagButtonsByPhase(st) {
    // Wire to your existing allowedFlagsForPhase() if present
    const phase = (st.phase || 'pre').toLowerCase();
    const allowed = (typeof allowedFlagsForPhase === 'function')
        ? new Set(allowedFlagsForPhase(phase))
        : new Set(['pre','green','yellow','red','blue','white','checkered']); // fallback

    document.querySelectorAll('.flag-btn').forEach(btn => {
      const flagName = (btn.dataset.flag || '').toLowerCase();
      const enable = allowed.has(flagName);
      btn.disabled = !enable;
      btn.classList.toggle('is-active',
        (st.flag || 'PRE').toLowerCase() === flagName);
    });
  }

  let _lastPillFlag = null;

function updateFlagPill(st){
  const pill = document.getElementById('flagPill');
  const txt  = document.getElementById('flagPillText');
  if (!pill || !txt) return;

  const flag  = (st.flag || 'PRE').toUpperCase();   // trust flag, ignore countdown
  const label = {
    PRE:'Pre-race', GREEN:'Green', YELLOW:'Yellow',
    RED:'Red', BLUE:'Blue', WHITE:'White', CHECKERED:'Checkered'
  }[flag] || flag;

  // Only special-case GREEN/CHECKERED; countdown shows PRE
  if (flag === 'GREEN')       txt.textContent = 'Green — Race in progress';
  else if (flag === 'CHECKERED') txt.textContent = 'Checkered — Race complete';
  else                        txt.textContent = label;

  if (_lastPillFlag !== flag) {
    pill.classList.remove('pulse');
    setTimeout(() => pill.classList.add('pulse'), 0);
    _lastPillFlag = flag;
  }
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
    num.textContent = row.number ? `#${row.number}` : (row.entrant_id ?? '');

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
      const id = row.entrant_id ?? row.tag ?? row.number ?? row.name;
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
    updateFlagBanner(st);
    lockFlagButtonsByPhase(st);
    updateFlagPill(st);

    //Race summary
    renderRCSummary(st);
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
