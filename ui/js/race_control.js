/* --------------------------------------------------------------------------
   CCRS Race Control — unified controller
   --------------------------------------------------------------------------
   - Polls /race/state once per second and renders clock/phase/flag.
   - Wires action buttons to backend (/race/control/*, /engine/flag).
   - Shows a live "Last lap feed" by diffing standings and appending entries.
   - Keeps flag pad visually synced and enforces legal-press policy.
   - Uses server-provided clock_ms when present; falls back to local calc.
  -------------------------------------------------------------------------- */

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
    if (sec == null) return '-';
    // m:ss.mmm where m can grow; .mmm is 3 digits
    const ms = Math.round((sec - Math.floor(sec)) * 1000);
    const s  = Math.floor(sec) % 60;
    const m  = Math.floor(sec / 60);
    return `${m}:${String(s).padStart(2,'0')}.${String(ms).padStart(3,'0')}`;
  }



// ----------------------------------------------------------------------
// Unified viewport sizing + Seen & Standings renderers
// ----------------------------------------------------------------------

const DEFAULT_VISIBLE_STANDINGS_ROWS = 16;
let cachedPlannedEntrantCount;

/** Read planned entrant count from localStorage (cached). */
function getPlannedEntrantCount() {
  if (cachedPlannedEntrantCount !== undefined) return cachedPlannedEntrantCount;
  try {
    const raw = localStorage.getItem('rc.entrants');
    if (!raw) return (cachedPlannedEntrantCount = null);
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return (cachedPlannedEntrantCount = null);
    cachedPlannedEntrantCount = parsed.filter(e => e && e.enabled !== false).length;
  } catch {
    cachedPlannedEntrantCount = null;
  }
  return cachedPlannedEntrantCount;
}

/** Fixed 16-row viewport; scroll activates beyond this. */
function getStandingsViewportRows() {
  return DEFAULT_VISIBLE_STANDINGS_ROWS;
}

/**
 * Compute an exact scroll viewport height from header + first N rows.
 * Optional tableEl lets callers use a different table (e.g. Seen).
 * Backward compatible if omitted: falls back to '#rcStandings'.
 */
function updateStandingsScrollState(scrollEl, visibleRows, actualRows, tableEl /* optional */) {
  if (!scrollEl) return;

  const rosterSize = getPlannedEntrantCount();
  if (typeof rosterSize === 'number' && rosterSize >= 0) {
    scrollEl.dataset.roster = String(rosterSize);
  } else {
    scrollEl.removeAttribute('data-roster');
  }

  const table = tableEl || scrollEl.querySelector('#rcStandings');
  const thead = table?.tHead;
  const tbody = table?.tBodies?.[0];

  let headH = 0, rowsH = 0;
  try {
    if (thead) headH = Math.ceil(thead.getBoundingClientRect().height);
    const rows = tbody ? Array.from(tbody.rows) : [];
    const limit = Math.min(visibleRows, rows.length);
    for (let i = 0; i < limit; i += 1) {
      rowsH += Math.ceil(rows[i].getBoundingClientRect().height);
    }
  } catch {}

  let padT = 0, padB = 0;
  try {
    const cs = scrollEl ? getComputedStyle(scrollEl) : null;
    padT = cs ? parseFloat(cs.paddingTop) || 0 : 0;
    padB = cs ? parseFloat(cs.paddingBottom) || 0 : 0;
  } catch {}

  // +2px for container border +1px safety for subpixel rounding
  const px = Math.max(0, headH + rowsH + padT + padB + 2 + 1);
  if (px > 0) {
    scrollEl.style.height = `${px}px`;
    scrollEl.style.maxHeight = `${px}px`;
    scrollEl.style.minHeight = `${px}px`;
  }

  const needsScroll = actualRows > visibleRows;
  scrollEl.style.overflowY = needsScroll ? 'auto' : 'hidden';
  scrollEl.classList.toggle('has-scroll', needsScroll);
  if (!needsScroll) scrollEl.scrollTop = 0;
}

// ----------------------------------------------------------------------
// "Seen" panel (PRE/COUNTDOWN) — table-rendered to match Standings
// ----------------------------------------------------------------------

/** No-op placeholder to keep older callsites happy. */
function setSeenHeader() { /* unified table header now; nothing to do */ }

/** Build padding rows for the Seen table to keep a full 16-row viewport. */
function padSeenRows(tbody, minRows = DEFAULT_VISIBLE_STANDINGS_ROWS) {
  if (!tbody) return;
  const dataCount = tbody.querySelectorAll('tr[data-key]').length;
  const target = Math.max(minRows, dataCount);

  // Remove stale padding before rebuilding cleanly
  tbody.querySelectorAll('tr.pad').forEach(tr => tr.remove());

  for (let i = dataCount; i < target; i += 1) {
    const tr = document.createElement('tr');
    tr.className = 'pad';
    tr.innerHTML = `
      <td class="tag"></td>
      <td class="num"></td>
      <td class="name"></td>
      <td class="reads"></td>`;
    tbody.appendChild(tr);
  }
}

/**
 * Render the PRE/COUNTDOWN Seen table using the same structure/spacing
 * as live Standings. Sources:
 *  - Prefer state.seen.rows if provided by server (already normalized)
 *  - Else derive from state.entrants and merge state.seen.counts
 */
function renderSeen(state) {
  const table   = document.getElementById('rcSeen');
  const tbody   = document.getElementById('seenTbody');
  const scroll  = document.querySelector('.seenScroll');
  const cSpan   = document.getElementById('seenCount');
  const tSpan   = document.getElementById('seenTotal');
  if (!table || !tbody || !scroll) return;

  const seen    = state?.seen || {};
  const counts  = seen?.counts || Object.create(null);
  const rowsSrc = Array.isArray(seen?.rows) ? seen.rows : null;

  // Update header counts if provided
  if (cSpan) cSpan.textContent = String(seen.count ?? 0);
  if (tSpan) tSpan.textContent = String(seen.total ?? 0);

  // Build normalized row list
  let base = [];
  if (rowsSrc && rowsSrc.length) {
    base = rowsSrc.map((r, idx) => ({
      key: r.tag ?? r.entrant_id ?? `${r.number ?? ''}:${r.name ?? ''}:${idx}`,
      tag: (r.tag ?? '').toString(),
      number: r.number != null && r.number !== '' ? String(r.number) : '',
      name: r.name || '',
      reads: Number(r.reads ?? (r.tag ? counts[r.tag] : 0) ?? 0),
      enabled: r.enabled !== false
    }));
  } else {
    const entrants = Array.isArray(state?.entrants) ? state.entrants : [];
    base = entrants.map((e, idx) => {
      const tag = (e.tag ?? '').toString();
      return {
        key: tag || e.entrant_id || `${e.number ?? ''}:${e.name ?? ''}:${idx}`,
        tag,
        number: e.number != null && e.number !== '' ? String(e.number) : '',
        name: e.name || '',
        reads: Number(tag ? counts[tag] : 0) || 0,
        enabled: e.enabled !== false
      };
    });
  }

  // Sort: enabled first, reads desc, then number asc (numeric-aware)
  base.sort((a, b) => {
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
    if (a.reads   !== b.reads)   return b.reads - a.reads;
    return String(a.number || '').localeCompare(String(b.number || ''), undefined, { numeric: true });
  });

  // Diff + patch rows
  const existing = new Map();
  tbody.querySelectorAll('tr[data-key]').forEach(tr => {
    const k = tr.getAttribute('data-key');
    if (k) existing.set(k, tr);
  });

  const frag = document.createDocumentFragment();
  for (const r of base) {
    let tr = existing.get(r.key);
    if (!tr) {
      tr = document.createElement('tr');
      tr.className = 'data';
      tr.setAttribute('data-key', r.key);
      tr.innerHTML = `
        <td class="tag"></td>
        <td class="num"></td>
        <td class="name"></td>
        <td class="reads"></td>`;
    }

    const [cTag, cNum, cName, cReads] = tr.children;
  const tagTxt = r.tag || '-';
    if (cTag.textContent !== tagTxt) cTag.textContent = tagTxt;

  const numTxt = r.number ? `#${r.number}` : '-';
    if (cNum.textContent !== numTxt) cNum.textContent = numTxt;

    if (cName.textContent !== (r.name || '')) cName.textContent = r.name || '';

    const readsTxt = String(r.reads);
    if (cReads.textContent !== readsTxt) cReads.textContent = readsTxt;

    tr.classList.toggle('is-disabled', !r.enabled);
    frag.appendChild(tr);
    existing.delete(r.key);
  }
  existing.forEach(tr => tr.remove());
  tbody.appendChild(frag);

  // Pad to fixed viewport and size/scroll exactly like standings
  const visible = getStandingsViewportRows();
  padSeenRows(tbody, visible);
  updateStandingsScrollState(scroll, visible, base.length, table);
}

// ----------------------------------------------------------------------
// Standings panel (GREEN/WHITE/CHECKERED) — unchanged structure
// ----------------------------------------------------------------------

function ensureStandingsDom() {
  let panel = document.getElementById('panelStandings');
  if (!panel) {
    const host = document.querySelector('#paneLive .rcLiveBody');
    if (!host) return null;

    panel = document.createElement('div');
    panel.id = 'panelStandings';
    panel.classList.add('hidden');

    const head = document.createElement('div');
    head.className = 'liveHead';
    head.textContent = 'Standings';

    const scroll = document.createElement('div');
    scroll.className = 'standingsScroll';

    const table = document.createElement('table');
    table.id = 'rcStandings';
    table.innerHTML = `
      <thead>
        <tr>
          <th class="pos">Pos</th>
          <th class="num">Number</th>
          <th class="name">Name</th>
          <th class="laps">Laps</th>
          <th class="last">Last</th>
          <th class="pace">Pace</th>
          <th class="best">Best</th>
        </tr>
      </thead>
      <tbody></tbody>`;

    scroll.appendChild(table);
    panel.append(head, scroll);
    host.appendChild(panel);
  }

  const table = panel.querySelector('#rcStandings');
  if (!table) return null;

  let tbody = table.querySelector('tbody');
  if (!tbody) {
    tbody = document.createElement('tbody');
    table.appendChild(tbody);
  }

  els.panelStandings = panel;
  els.standingsTable = table;
  els.standingsTbody = tbody;

  const scroll = panel.querySelector('.standingsScroll');
  if (scroll) els.standingsScroll = scroll;

  return { panel, table, tbody, scroll };
}

function fmtLapCell(raw) {
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return '-';
  return fmtLapTime(n);
}

function renderStandings(state) {
  const dom = ensureStandingsDom();
  if (!dom?.panel || !dom?.tbody) return;

  const phaseLower = (state?.phase || '').toLowerCase();
  const visibleRows = getStandingsViewportRows();
  const isRacingPhase =
    phaseLower === 'green' || phaseLower === 'white' || phaseLower === 'checkered';

  // Only show the Standings panel during racing phases
  dom.panel.classList.toggle('hidden', !isRacingPhase);

  if (!isRacingPhase) {
    dom.tbody.replaceChildren();
    padStandingsRows(dom.tbody, visibleRows);
    updateStandingsScrollState(dom.scroll || els.standingsScroll, visibleRows, 0);
    return;
  }

  // Normalize incoming rows
  const srcRows = Array.isArray(state?.standings) ? state.standings : [];
  const normalized = srcRows.map((src, idx) => {
    const entrantId = Number(src?.entrant_id ?? NaN);
    const keyBase =
      src?.entrant_id ?? src?.tag ?? src?.number ?? src?.name ?? idx;

    const laps = Number(src?.laps ?? src?.total_laps ?? 0) || 0;
    const lapDeficit = Number(src?.lap_deficit ?? 0) || 0;

    // Prefer pace in seconds if provided; else convert ms to seconds; else null
    const paceSeconds = (() => {
      const core = src?.pace_5 ?? src?.pace ?? src?.pace_s;
      if (core != null) {
        const v = Number(core);
        if (Number.isFinite(v) && v > 0) return v;
      }
      const paceMs = src?.pace_ms;
      if (paceMs != null) {
        const v = Number(paceMs) / 1000;
        if (Number.isFinite(v) && v > 0) return v;
      }
      return null;
    })();

    return {
      entrantId,
      key: String(keyBase),
      position: Number(src?.position ?? idx + 1) || (idx + 1),
      number: src?.number ?? src?.car ?? src?.car_num ?? '',
      name: src?.name ?? src?.team ?? src?.driver ?? '',
      laps,
      lapDeficit,
      last: src?.last ?? src?.last_s ?? src?.last_ms ?? null,
      pace: paceSeconds,
      best: src?.best ?? src?.best_s ?? src?.best_ms ?? null,
      gridIndex: Number(src?.grid_index ?? NaN),
      enabled: src?.enabled !== false
    };
  });

  // Clear padding rows before update
  const tbody = dom.tbody;
  tbody.querySelectorAll('tr.pad').forEach(tr => tr.remove());

  // Index existing rows by key
  const existing = new Map();
  tbody.querySelectorAll('tr[data-key]').forEach(tr => {
    const key = tr.getAttribute('data-key');
    if (key) existing.set(key, tr);
  });

  // Build/patch rows
  const frag = document.createDocumentFragment();
  for (const row of normalized) {
    let tr = existing.get(row.key);
    if (!tr) {
      tr = document.createElement('tr');
      tr.className = 'data';
      tr.setAttribute('data-key', row.key);
      if (Number.isFinite(row.entrantId)) tr.dataset.entrantId = String(row.entrantId);

      // Column order MUST match your <thead>:
      // 0: Pos, 1: Number, 2: Name, 3: Brake, 4: Laps, 5: Last, 6: Pace, 7: Best
      tr.innerHTML = `
        <td class="pos"></td>
        <td class="num"></td>
        <td class="name"></td>
        <td class="brake col-brake">
        <button class="btn btn--sm brake-toggle" data-ok="">—</button></td>
        <td class="laps"></td>
        <td class="last"></td>
        <td class="pace"></td>
        <td class="best"></td>
      `;

      // Initialize the brake button (no-op unless body.is-qualifying)
      if (typeof window.CCRS?.initBrakeCell === 'function') {
        window.CCRS.initBrakeCell(tr);
      }
    } else {
      // Ensure entrant id is stamped for existing rows
      if (Number.isFinite(row.entrantId)) tr.dataset.entrantId = String(row.entrantId);

      // If this row predates the Brake column, insert it at index 3
      if (!tr.querySelector('.brake-toggle')) {
        const td = document.createElement('td');
        td.className = 'brake col-brake';
        td.innerHTML = `<button class="btn btn--sm brake-toggle" data-ok="">—</button>`;
        // Insert before current index 3 (Laps) to keep order aligned
        const insertBefore = tr.children[3] || null;
        tr.insertBefore(td, insertBefore);
        if (typeof window.CCRS?.initBrakeCell === 'function') {
          window.CCRS.initBrakeCell(tr);
        }
      }
    }

    // Update cell contents (keep indices aligned with header order)
    const cells = tr.children;

    const posTxt = String(row.position);
    if (cells[0].textContent !== posTxt) cells[0].textContent = posTxt;

    const numTxt = row.number ? `#${row.number}` : '-';
    if (cells[1].textContent !== numTxt) cells[1].textContent = numTxt;

    if (cells[2].textContent !== row.name) cells[2].textContent = row.name;

    let lapsTxt = String(row.laps);
    if (row.lapDeficit > 0) {
      const suffix = row.lapDeficit === 1 ? '−1L' : `−${row.lapDeficit}L`;
      lapsTxt += ` (${suffix})`;
    }
    if (cells[4].textContent !== lapsTxt) cells[4].textContent = lapsTxt;

    const lastTxt = fmtLapCell(row.last);
    if (cells[5].textContent !== lastTxt) cells[5].textContent = lastTxt;

    const paceTxt = fmtLapCell(row.pace);
    if (cells[6].textContent !== paceTxt) cells[6].textContent = paceTxt;

    const bestTxt = fmtLapCell(row.best);
    if (cells[7].textContent !== bestTxt) cells[7].textContent = bestTxt;

    tr.classList.toggle('is-disabled', !row.enabled);
    frag.appendChild(tr);
    existing.delete(row.key);
  }

  // Remove rows not in the new snapshot
  existing.forEach(tr => tr.remove());

  // Commit DOM, pad to viewport, update scroll state
  tbody.appendChild(frag);
  padStandingsRows(tbody, visibleRows);
  updateStandingsScrollState(dom.scroll || els.standingsScroll, visibleRows, normalized.length);
}


/** Pad rows for standings to keep a full 16-row viewport. */
function padStandingsRows(tbody, minRows = DEFAULT_VISIBLE_STANDINGS_ROWS) {
  if (!tbody) return;

  const existingData = tbody.querySelectorAll('tr[data-key]').length;
  const target = Math.max(minRows, existingData);

  tbody.querySelectorAll('tr.pad').forEach(tr => tr.remove());

  for (let i = existingData; i < target; i += 1) {
    const tr = document.createElement('tr');
    tr.className = 'pad';
    tr.innerHTML = `
      <td class="pos"></td>
      <td class="num"></td>
      <td class="name"></td>
      <td class="laps"></td>
      <td class="last"></td>
      <td class="pace"></td>
      <td class="best"></td>`;
    tbody.appendChild(tr);
  }
}



  // ----------------------------------------------------------------------
  // Race summary rendering
  // ----------------------------------------------------------------------

  function secondsToHuman(s) {
    const n = Number(s);
  if (!Number.isFinite(n) || n < 0) return '-';
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
  let limitStr = '-';
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
    panelStandings : $('#panelStandings'),
    standingsTable : document.querySelector('#rcStandings'),
    standingsTbody : document.querySelector('#rcStandings tbody'),
    standingsScroll: document.querySelector('#panelStandings .standingsScroll'),
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

  function restartAnimation(el, className) {
  if (!el) return;
  if (className) {
    el.classList.remove(className);
    void el.offsetWidth;     // reflow to reset animation clock
    el.classList.add(className);
  } else {
    // style-based fallback if needed
    el.style.animation = 'none';
    void el.offsetWidth;
    el.style.animation = '';
  }
}

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

function updateFlagPill(st) {
  const pill = document.getElementById('flagPill');
  const txt  = document.getElementById('flagPillText');
  if (!pill || !txt) return;

  const flag  = String(st?.flag || 'PRE').toUpperCase();
  const label = {
    PRE:'Pre-race',
    GREEN:'Green — Race in progress',
    YELLOW:'Yellow',
    RED:'Red',
    BLUE:'Blue',
    WHITE:'White',
    CHECKERED:'Checkered — Race complete'
  }[flag] || flag;
  txt.textContent = label;

  // Beacon for everything except GREEN/CHECKERED
  const shouldBeacon = !(flag === 'GREEN' || flag === 'CHECKERED');

  // Only do the sync dance when the flag actually changes
  if (window._lastPillFlag !== flag) {
    // 1) Brief brightness flash (CSS transition-based, no animation keyframes)
    pill.classList.add('flash');
    setTimeout(() => pill.classList.remove('flash'), 200);

    // 2) Restart both the pill beacon AND the active button at the exact same instant
    //    This ensures they're perfectly in sync from the moment of flag change
    if (shouldBeacon) {
      restartAnimation(pill, 'beacon');
    } else {
      pill.classList.remove('beacon');
    }

    // 3) Restart the left pad's active button pulse at the exact same instant
    const activeBtn = document.querySelector('#flagPad .flag.is-active, #preFlagRow .flag.is-active');
    if (activeBtn && String(activeBtn.dataset.flag || '').toUpperCase() === flag) {
      restartAnimation(activeBtn, 'is-active');
    }

    window._lastPillFlag = flag;
  } else {
    // Steady-state: just enforce beacon on/off
    pill.classList.toggle('beacon', shouldBeacon);
  }
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

    // NEW: standings render (visible when showFeed)
    renderStandings(st);

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
        const url = `/ui/operator/results_exports.html${rid ? `?race=${encodeURIComponent(rid)}` : ''}`;
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

    // initial tick
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

/* ======================================================================
   QUALIFYING — Brake Test toggles (Race Control)
   ----------------------------------------------------------------------
   Purpose
   - Provide a per-row Brake toggle shown only during qualifying.
   - Cycle order: null (—) → Pass → Fail → null.
   - Persist immediately to /qual/heat/{heat_id}/brake as { entrant_id, brake_ok }.
   - Restore saved state on load from GET /qual/heat/{heat_id}/brake (e.g. {"17":true}).

   DOM contract
   - <thead> contains: <th class="brake col-brake">Brake</th>
   - Each standings row: <tr data-entrant-id> (set via tr.dataset.entrantId)
   - Cell markup (inserted by renderStandings):
       <td class="brake col-brake">
         <button class="btn btn--sm brake-toggle" data-ok="">—</button>
       </td>
   - CSS: .col-brake is hidden unless body.is-qualifying is present.

   Behavior
   - No-op outside qualifying.
   - Optimistic UI update; POST errors are logged and state re-syncs on refresh.
   - CCRS.initBrakeCell(tr) initializes the button when a row is created.

   Storage
   - In-memory Map: entrant_id -> true | false | null
   - Server stores flags in heats.config_json.qual_brake_flags
   ====================================================================== */
(function () {
  'use strict';

  // Namespace + tiny query helper
  const CCRS = (window.CCRS = window.CCRS || {});
  const $ = (sel, root) => (root || document).querySelector(sel);

  // Cache of verdicts: entrant_id -> true | false | null
  const brakeVerdicts = new Map();

  // Current session (set on boot)
  let qualHeatId = null;
  let isQualifying = false;

  // -------------------------------------------------------------------
  // Runtime discovery: prefer a global CCRS.runtime if you set one;
  // otherwise pull from /setup/runtime (heat_id, session_type, etc.).
  // -------------------------------------------------------------------
  async function getRuntime() {
    const rtv = (window.CCRS && window.CCRS.runtime) || null;
    if (rtv && (rtv.heat_id != null) && typeof rtv.session_type === 'string') {
      return rtv;
    }
    try {
      const r = await fetch('/setup/runtime');
      if (!r.ok) throw new Error('runtime fetch failed');
      return await r.json();
    } catch {
      return { heat_id: null, session_type: null };
    }
  }

  function setQualMode(on) {
    isQualifying = !!on;
    document.body.classList.toggle('is-qualifying', isQualifying);
  }

  // -------------------------------------------------------------------
  // Server I/O for verdicts (stored in heats.config_json.qual_brake_flags)
  // GET returns { "17": true, "44": false, ... }
  // POST body: { entrant_id, brake_ok }
  // -------------------------------------------------------------------
  async function loadBrakeVerdicts(heat_id) {
    brakeVerdicts.clear();
    if (!heat_id) return;
    try {
      const r = await fetch(`/qual/heat/${heat_id}/brake`);
      if (!r.ok) return;
      const data = await r.json();
      for (const [k, v] of Object.entries(data)) {
        brakeVerdicts.set(Number(k), v === true);
      }
    } catch {
      /* swallow; remain empty */
    }
  }

  async function persistBrake(entrantId, nextVal) {
    if (!qualHeatId) return;
    try {
      const r = await fetch(`/qual/heat/${qualHeatId}/brake`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entrant_id: entrantId, brake_ok: nextVal }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (e) {
      console.warn('Brake verdict update failed:', e);
    }
  }

  // -------------------------------------------------------------------
  // UI helpers
  // -------------------------------------------------------------------
  function renderBrakeButton(btn, entrantId) {
    const val = brakeVerdicts.has(entrantId) ? brakeVerdicts.get(entrantId) : null;
    btn.dataset.ok = (val === null ? '' : String(val));
    btn.classList.remove('is-yes', 'is-no', 'is-unk');
    if (val === true) {
      btn.classList.add('is-yes'); btn.textContent = 'Pass';
    } else if (val === false) {
      btn.classList.add('is-no');  btn.textContent = 'Fail';
    } else {
      btn.classList.add('is-unk'); btn.textContent = '—';
    }
  }

  // Public hook: call this after you build a TR to stamp initial state
  CCRS.initBrakeCell = function initBrakeCell(tr) {
    if (!isQualifying) return; // hidden anyway
    const btn = tr.querySelector('button.brake-toggle');
    const entrantId = Number(tr?.dataset?.entrantId);
    if (!btn || !Number.isFinite(entrantId)) return;
    renderBrakeButton(btn, entrantId);
  };

  // One delegated click listener for the standings table
  function wireClicks() {
    const table = $('#rcStandings');
    if (!table) return;
    table.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button.brake-toggle');
      if (!btn || !isQualifying) return; // ignore outside qualifying

      const tr = btn.closest('tr');
      const entrantId = Number(tr?.dataset?.entrantId);
      if (!Number.isFinite(entrantId)) return;

      // Current → next (cycle: — → Pass → Fail → —)
      const cur = (btn.dataset.ok === 'true') ? true :
                  (btn.dataset.ok === 'false') ? false : null;
      const next = (cur === null) ? true : (cur === true ? false : null);

      // Optimistic cache + UI, then persist
      brakeVerdicts.set(entrantId, next);
      renderBrakeButton(btn, entrantId);
      await persistBrake(entrantId, next);
      // Re-render from cache (in case of class/text drift)
      renderBrakeButton(btn, entrantId);
    });
  }

  // Observe <tbody> so rows added later get initialized automatically
  function observeStandingsBody() {
    const tbody = $('#rcStandings tbody');
    if (!tbody) return;
    // Initialize any existing rows
    tbody.querySelectorAll('tr').forEach((tr) => CCRS.initBrakeCell(tr));
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        m.addedNodes.forEach((n) => {
          if (n.nodeType === 1 && n.tagName === 'TR') CCRS.initBrakeCell(n);
        });
      }
    });
    mo.observe(tbody, { childList: true });
  }

  // -------------------------------------------------------------------
  // Boot sequence (runs once per page load)
  // -------------------------------------------------------------------
  async function bootQualBrake() {
    const rt = await getRuntime();
    const isQual = (rt.session_type || '').toLowerCase() === 'qualifying';
    qualHeatId = isQual ? rt.heat_id : null;
    setQualMode(isQual);

    if (!isQual) return; // nothing else to do in non-qual sessions

    await loadBrakeVerdicts(qualHeatId);
    wireClicks();
    observeStandingsBody();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootQualBrake);
  } else {
    bootQualBrake();
  }
})();

