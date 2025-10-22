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

  function paintClock(sec) {
    if (!els.clockDisplay) return;
    const neg = sec < 0;
    const s   = Math.abs(Math.floor(sec || 0));
    const h   = Math.floor(s / 3600);
    const m   = Math.floor((s % 3600) / 60);
    const r   = s % 60;

    const digits = `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(r).padStart(2,'0')}`;
    // Reserve one fixed-width character for the sign so the text never shifts.
    els.clockDisplay.innerHTML =
      `<span class="clock-sign">${neg ? '–' : '&#8201;'}</span>` +
      `<span class="clock-digits">${digits}</span>`;
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

// ----------------------------------------------------------------------
// Render the "seen" list from state
// ---------------------------------------------------------------------- 
function renderSeen(state) {
  const ul    = document.getElementById('seenList');
  const cSpan = document.getElementById('seenCount');
  const tSpan = document.getElementById('seenTotal');
  if (!ul) return;

  const seen = state?.seen || { count:0, total:0, rows:[] };
  if (cSpan) cSpan.textContent = String(seen.count ?? 0);
  if (tSpan) tSpan.textContent = String(seen.total ?? 0);

  setSeenHeader();

  // Normalize + choose a stable key
  const rows = (Array.isArray(seen.rows) ? seen.rows : []).map(r => ({
    key: r.tag ?? r.entrant_id ?? `${r.number ?? ''}:${r.name ?? ''}`,
    tag: r.tag ?? '—',
    number: r.number ?? null,
    name: r.name ?? '',
    reads: Number(r.reads ?? 0),
    enabled: !!r.enabled,
  }));

  // Desired order: enabled first, reads desc, number asc
  rows.sort((a,b) => {
    if (a.enabled !== b.enabled) return (a.enabled ? -1 : 1);
    if (a.reads !== b.reads)     return b.reads - a.reads;
    return String(a.number ?? '').localeCompare(String(b.number ?? ''));
  });

  // Index existing <li> by key
  const existing = new Map();
  ul.querySelectorAll('li.seenRow').forEach(li => {
    const key = li.getAttribute('data-key');
    if (key) existing.set(key, li);
  });

  // Helper to build or update a row
  function ensureRow(r) {
    let li = existing.get(r.key);
    if (!li) {
      li = document.createElement('li');
      li.className = 'seenRow';
      li.setAttribute('data-key', r.key);

      const cTag   = document.createElement('div'); cTag.className   = 'cell tag';
      const cNum   = document.createElement('div'); cNum.className   = 'cell num';
      const cName  = document.createElement('div'); cName.className  = 'cell name';
      const cReads = document.createElement('div'); cReads.className = 'cell reads';

      li.append(cTag, cNum, cName, cReads);
    }

    const [cTag, cNum, cName, cReads] = li.children;

    // Update cells (only if changed)
    if (cTag.textContent !== r.tag) cTag.textContent = r.tag;
    const numTxt = r.number ? `#${r.number}` : '—';
    if (cNum.textContent !== numTxt) cNum.textContent = numTxt;

    // Name + pill
    const wantPill = r.reads > 0;
    if (cName.firstChild?.nodeType === Node.TEXT_NODE) {
      if (cName.firstChild.nodeValue !== r.name) cName.firstChild.nodeValue = r.name;
    } else {
      cName.textContent = r.name;
    }
    let pill = cName.querySelector('.seen-pill');
    if (wantPill && !pill) {
      pill = document.createElement('span');
      pill.className = 'seen-pill';
      pill.textContent = 'Entrant Seen';
      cName.appendChild(pill);
    } else if (!wantPill && pill) {
      pill.remove();
    }

    const readsTxt = String(r.reads);
    if (cReads.textContent !== readsTxt) cReads.textContent = readsTxt;

    // Enabled styling
    li.classList.toggle('is-disabled', !r.enabled);
    return li;
  }

  // 1) Ensure all rows exist/updated and gather in desired order
  const desiredNodes = rows.map(r => ensureRow(r));

  // 2) Reorder DOM by appending in desired order (moves nodes cheaply)
  //    This avoids tearing down the entire list each second.
  const frag = document.createDocumentFragment();
  for (const node of desiredNodes) frag.appendChild(node);
  ul.appendChild(frag);

  // 3) Remove any stale nodes not present anymore
  existing.forEach((li, key) => {
    if (!rows.find(r => r.key === key)) li.remove();
  });
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

  function renderRCSummary(st) {
    const el = document.getElementById('rcSummaryText');
    if (!el) return;
    el.textContent = summarizeFromState(st);
}


  function summarizeFromState(st) {
    // Limit
    let limitStr = '—';
    const lim = st?.limit;

    if (lim && typeof lim === 'object') {
      const t = String(lim.type || '').toLowerCase();

      if (t === 'time') {
        // accept either value_s (old UI) or value (engine snapshot)
        const seconds = (lim.value_s != null) ? Number(lim.value_s)
                      : (lim.value    != null) ? Number(lim.value)
                      : null;
        if (Number.isFinite(seconds)) {
          limitStr = `Time ${secondsToHuman(seconds)}`;
          // optionally show soft-end if present on either source
          if (lim.soft_end === true) limitStr += ' • soft';
        }
      } else if (t === 'laps') {
        // accept either value_laps (old UI) or value (engine snapshot)
        const laps = (lim.value_laps != null) ? Number(lim.value_laps)
                  : (lim.value       != null) ? Number(lim.value)
                  : null;
        if (Number.isFinite(laps)) {
          limitStr = `Laps ${laps}`;
        }
      } else if (t) {
        limitStr = String(lim.type).toUpperCase();
      }
    }

    // Rank (kept simple for now)
    const rankStr = 'Rank: Total Laps';

    // MinLap: accept from several places
    const minLap = st?.min_lap_s ?? st?.session?.min_lap_s ?? st?.engine?.min_lap_s;
    const minLapStr = (minLap != null) ? `MinLap ${Number(minLap).toFixed(1)}s` : null;

    const parts = [limitStr, rankStr];
    if (minLapStr) parts.push(minLapStr);
    return parts.join(' • ');
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
    //btnPreRace     : $('#btnPreRace'),
    //btnStartPrep   : $('#btnStartPrep'),
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

    //Post-finish actions
    postFinish     : document.querySelector('#postFinishActions'),
    btnResults     : document.querySelector('#btnResults'),
  };

  // ----------------------------------------------------------------------
  // Allowed flag presses by phase + pad update (authoritative gate)
  // ----------------------------------------------------------------------
  // Flags permitted while in each phase.
  // Key idea: while racing (green/white), you can always go back to GREEN (and throw other colors).
  function allowedFlagsForPhase(phase) {
    switch ((phase || 'pre').toLowerCase()) {
      case 'pre':
        // No accidental colors; GREEN is allowed only to arm/start (if you support that).
        return ['pre', 'green'];
      case 'countdown':
        // UI shouldn’t force GREEN; timer flips it.
        return ['pre'];
      case 'green':
      case 'white':
      case 'yellow':
      case 'red':
      case 'blue':
        // While running / neutralized, you can always return to GREEN (and throw others).
        return ['green', 'yellow', 'red', 'blue', 'white', 'checkered'];
      case 'checkered':
        return ['checkered'];
      default:
        return ['green', 'yellow', 'red', 'blue', 'white', 'checkered'];
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
    const phase = (st?.phase || 'pre').toLowerCase();
    const allowed = new Set(allowedFlagsForPhase(phase));
    document.querySelectorAll('.flag-btn').forEach(btn => {
      const flagName = (btn.dataset.flag || '').toLowerCase();
      btn.disabled = !allowed.has(flagName);
      btn.classList.toggle('is-active', (st?.flag || 'pre').toLowerCase() === flagName);
    });
  }

  let _lastPillFlag = null;

function updateFlagPill(st){
  const pill = document.getElementById('flagPill');
  const txt  = document.getElementById('flagPillText');
  if (!pill || !txt) return;

  const flag  = (st.flag || 'PRE').toUpperCase();
  const phase = (st.phase||'pre').toLowerCase();

  const label = { PRE:'Pre-race', GREEN:'Green', YELLOW:'Yellow', RED:'Red', BLUE:'Blue', WHITE:'White', CHECKERED:'Checkered' }[flag] || flag;

  if (flag === 'GREEN')       txt.textContent = 'Green — Race in progress';
  else if (flag === 'CHECKERED') txt.textContent = 'Checkered — Race complete';
  else                         txt.textContent = label;

  // one-shot pulse you already had
  if (window._lastPillFlag !== flag) {
    pill.classList.remove('pulse');
    setTimeout(() => pill.classList.add('pulse'), 0);
    window._lastPillFlag = flag;
  }

  // NEW: continuous beacon for everything except GREEN/CHECKERED
  const shouldBeacon = !(flag === 'GREEN' || flag === 'CHECKERED');
  pill.classList.toggle('beacon', shouldBeacon);
}



// ----------------------------------------------------------------------
// Clock mode button update (remaining vs elapsed)
//  ----------------------------------------------------------------------

function updateClockModeButton(st) {
  const btn = els.btnClockMode;
  if (!btn) return;

  const ph = (st?.phase || '').toLowerCase();
  const hasRemaining =
    (st?.limit && typeof st.limit.remaining_ms === 'number') ||
    (st?.clock && typeof st.clock.remaining_s === 'number');

  // Disable during COUNTDOWN; enable during race only if we actually have Remaining.
  const disable = (ph === 'countdown') || !hasRemaining;
  btn.disabled = !!disable;

  // Label reflects current mode; if disabled, show "Elapsed" (the only sensible view).
  btn.textContent = (disable || clockMode === 'elapsed') ? 'Elapsed' : 'Remaining';
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

  // Decide phase once for this block
  const phaseLower = (st?.phase || '').toLowerCase();

  // 1) COUNTDOWN: always render the negative timer first (engine doesn't own this)
  if (phaseLower === 'countdown') {
    let dispSec;

    // Prefer explicit remaining seconds if server sent them
    const remS = (st?.clock?.countdown_remaining_s ?? st?.countdown_remaining_s);
    if (typeof remS === 'number') {
      // Show as negative HH:MM:SS (e.g., −00:00:09)
      dispSec = -Math.max(0, Math.ceil(remS));
    }
    // Or derive from negative clock_ms if present
    else if (typeof st?.clock_ms === 'number' && st.clock_ms < 0) {
      dispSec = st.clock_ms / 1000;
    } else if (typeof st?.clock?.clock_ms === 'number' && st.clock.clock_ms < 0) {
      dispSec = st.clock.clock_ms / 1000;
    }
    // Or fall back to the local anchor
    else if (countdownAnchor) {
      const now = Date.now() / 1000;
      const neg = -(Math.max(0, countdownAnchor - now));
      const maxNeg = -Number(st.countdown_from_s || 0);
      dispSec = Math.max(neg, maxNeg);
    }
    // Last resort: show the full armed countdown as a static negative
    else {
      dispSec = -Number(st.countdown_from_s || 0);
    }
    // ---- Race summary (top bar) ----
  
  paintClock(dispSec);
  }
  // 2) RACING/FINISH: engine owns time → support Elapsed/Remaining toggle
  else {
    const srvClockMs =
      (typeof st.clock_ms === 'number') ? st.clock_ms :
      (st.clock && typeof st.clock.clock_ms === 'number') ? st.clock.clock_ms :
      null;

    if (srvClockMs != null) {
      const elapsedS   = Math.max(0, srvClockMs / 1000);
      const remMs      = st?.limit?.remaining_ms;
      const remainingS = (typeof remMs === 'number') ? Math.max(0, remMs / 1000) : null;
      const showS      = (remainingS != null && clockMode === 'remaining') ? remainingS : elapsedS;
      paintClock(showS);
    } else {
      // Fallback to server-provided elapsed/remaining seconds if available
      const elapsed   = Number(st?.clock?.elapsed_s ?? 0);
      const remaining = (st?.clock?.remaining_s != null) ? Number(st.clock.remaining_s) : null;
      const show      = (remaining != null && clockMode === 'remaining') ? remaining : elapsed;
      paintClock(show);
    }
  }


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

    // Post-finish CTA: visible only at CHECKERED + breathe
    if (els.postFinish) {
      const show = (phaseLower === 'checkered');
      els.postFinish.classList.toggle('hidden', !show);
      if (els.btnResults) els.btnResults.classList.toggle('btn--breathing', show);
    }
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
    burstPoll(250, 3000);
    renderState(await api('/race/state'));
  }

  async function startRace() {
    const res = await api('/race/control/start_race', { method: 'POST' });
    if ((res?.phase || '').toLowerCase() === 'countdown') {
      const cd = Number(res?.countdown_from_s || 0);
      if (cd > 0) countdownAnchor = (Date.now() / 1000) + cd;
    }
    lastLapCounts.clear();
    burstPoll(250, 4000);
    renderState(await api('/race/state'));
  }

  async function endRace() {
    if (!confirm('End race and throw checkered?')) return;
    await api('/race/control/end_race', { method: 'POST' });
    burstPoll(250, 3000);
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
    burstPoll(250, 3000);
    renderState(await api('/race/state'));
  }

  async function setActiveFlag(flagLower) {
    const upper = String(flagLower || 'pre').toUpperCase();
    const phase = (document.body.dataset.phase || 'pre').toLowerCase();

    // Optimistic paint so it feels instant
    const prevFlag = document.body.dataset.flag || 'PRE';
    document.body.dataset.flag = upper;
    updateFlagPill({ flag: upper, phase });
    highlightActiveFlagButton(flagLower);
    burstPoll(250, 3000); // faster pulls for the next ~3s

    try {
      const r = await fetch('/engine/flag', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ flag: upper }),
      });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);

      // Reconcile with authoritative state quickly
      renderState(await api('/race/state'));
    } catch (err) {
      // Revert on failure (e.g., 409 during COUNTDOWN or illegal phase)
      document.body.dataset.flag = prevFlag;
      updateFlagPill({ flag: prevFlag, phase });
      highlightActiveFlagButton((prevFlag || 'PRE').toLowerCase());
      console.debug('Flag set rejected:', upper, String(err));
    }
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

    // Results button
    if (els.btnResults) {
      els.btnResults.addEventListener('click', () => {
        // Prefer live state; fall back to what Race Setup cached
        const rid = (window.lastState && lastState.race_id) ||
                    Number(localStorage.getItem('rc.race_id') || 0);
        // Adjust the URL if your results page path is different
        const url = `/ui/operator/results.html${rid ? `?race=${encodeURIComponent(rid)}` : ''}`;
        window.location.assign(url);
      });
    }
  }

  // ----------------------------------------------------------------------
  // Poll loop - Burs t mode support
  // ----------------------------------------------------------------------
  let _burstUntil = 0;
  let _burstMs = 250;
  let _pollTimer = null;

  function burstPoll(intervalMs = 250, durationMs = 3000) {
    _burstMs = intervalMs;
    _burstUntil = Date.now() + durationMs;
  }

  async function refreshState() {
    try { renderState(await api('/race/state')); } catch { /* ignore */ }
  }

  function startPolling() {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }

    const tick = async () => {
      await refreshState();
      const now = Date.now();
      const nextMs = (now < _burstUntil) ? _burstMs : 1000; // default 1s
      _pollTimer = setTimeout(tick, nextMs);
    };

    // prime the pump
    tick();
  }

  // ----------------------------------------------------------------------
  // Boot
  // ----------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', () => {
    bindControls();
    startPolling();
  });
})();
