/* ==========================================================================
   CCRS Race Control — merged: API-driven clock/phase + existing flags UI
   --------------------------------------------------------------------------
   - Polls /race/state once per second and renders the clock + phase/flag.
   - Wires existing buttons to the real backend endpoints.
   - Keeps the visual flag pad highlighting in sync with server flag.
   ========================================================================== */

(() => {
  'use strict';

  // --- tiny utils ---
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  // Minimal fetch helper that returns JSON or empty object
  const api = async (url, opts = {}) => {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    try { return await r.json(); } catch { return {}; }
  };

  //HH:MM:SS with optional leading minus
function fmtClockHMS(sec) {
  const neg = sec < 0;
  const s = Math.abs(Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const sign = neg ? "–" : "";
  return `${sign}${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(r).padStart(2,"0")}`;
}


  // --- local countdown anchor (client-side only) ---
  let countdownAnchor = null; // epoch seconds when 0 should occur
  // clock mode: 'remaining' | 'elapsed' (persisted)
  let clockMode = (() => {
    const v = (localStorage.getItem('rc.clockMode') || 'remaining').toLowerCase();
    return (v === 'elapsed') ? 'elapsed' : 'remaining';
  })();
  let lastState = null;

  // -------------------------- Elements ---------------------------------
  const els = {
    // Toolbar/clock
    btnPreRace    : $('#btnPreRace'),
    btnCountdown10: $('#btnCountdown10'),
    btnGoGreen    : $('#btnGoGreen'),
    btnStartRace  : $('#btnStartRace'),
    btnEndRace    : $('#btnEndRace'),
    btnAbortList  : $$('#btnAbort'),    // note: duplicate IDs in DOM; collect both
  btnStartPrep  : $('#btnStartPrep'),
  btnAbortReset : $('#btnAbortReset'),
  btnClockMode  : $('#btnClockMode'),
    clockDisplay  : document.getElementById("raceClock"),

    // Panels
    panelSeen     : $('#panelSeen'),
    panelFeed     : $('#panelFeed'),

    // Flags
    flagPad       : $('#flagPad'),
    preFlagRow    : $('#preFlagRow'),
  };

  // -------------------------- State render ------------------------------
  async function refreshState() {
    try {
      const st = await api('/race/state');
      renderState(st);
    } catch (e) {
      // optional: net status UI could be updated here
    }
  }

  function renderState(st) {
    if (!st) return;
    lastState = st;

    // Clock
    const elapsed   = st?.clock?.elapsed_s ?? 0;
    const remaining = st?.clock?.remaining_s; // may be null

    // Update clock mode button availability and label first
    updateClockModeButton(st);

    if ((st.phase === 'countdown') && countdownAnchor) {
      const now = Date.now() / 1000;
      const neg = Math.max(-(countdownAnchor - now), -Number(st.countdown_from_s || 0));
      if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(neg);
    } else if (remaining == null) {
      // open-ended
      if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(elapsed);
    } else {
      // time-limited → show based on selected mode
      const show = (clockMode === 'elapsed') ? elapsed : remaining;
      if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(show);
    }

    // phase/flag to body dataset for CSS and highlighting
    document.body.dataset.phase = st.phase || 'pre';
    document.body.dataset.flag  = (st.flag || 'PRE').toUpperCase();

    // Enable/disable buttons
    const ph = st.phase;
    if (els.btnPreRace)   els.btnPreRace.disabled   = (ph !== 'pre');
    if (els.btnStartRace) els.btnStartRace.disabled = !(ph === 'pre' || ph === 'countdown');
    if (els.btnEndRace)   els.btnEndRace.disabled   = !(ph === 'green' || ph === 'white');
    if (els.btnGoGreen)   els.btnGoGreen.disabled   = !(ph === 'pre' || ph === 'countdown');
    if (els.btnCountdown10) els.btnCountdown10.disabled = !(ph === 'countdown');
    if (els.btnAbortList && els.btnAbortList.length) els.btnAbortList.forEach(b => b.disabled = false);

    // Panels: show feed only when racing or checkered
    const showFeed = (ph === 'green' || ph === 'white' || ph === 'checkered');
    if (els.panelSeen) els.panelSeen.classList.toggle('hidden', showFeed);
    if (els.panelFeed) els.panelFeed.classList.toggle('hidden', !showFeed);

    // Flag highlighting
    highlightActiveFlagButton((st.flag || 'PRE').toLowerCase());
  }

  function setClockMode(mode) {
    const next = (mode === 'elapsed') ? 'elapsed' : 'remaining';
    clockMode = next;
    try { localStorage.setItem('rc.clockMode', next); } catch (_) {}
    updateClockModeButton(lastState);
    if (lastState) renderState(lastState);
  }

  function toggleClockMode() {
    setClockMode(clockMode === 'elapsed' ? 'remaining' : 'elapsed');
  }

  function updateClockModeButton(st) {
    const btn = els.btnClockMode;
    if (!btn) return;
    const ph = st?.phase;
    const remaining = st?.clock?.remaining_s;
    // Disable during countdown or when no remaining exists (open-ended)
    const disable = (ph === 'countdown') || (remaining == null);
    btn.disabled = !!disable;
    // Label indicates current display mode; if disabled due to open-ended, force 'Elapsed'
    const label = (disable && remaining == null) ? 'Elapsed' : (clockMode === 'elapsed' ? 'Elapsed' : 'Remaining');
    btn.textContent = label;
  }

  function highlightActiveFlagButton(flagLower) {
    const want = String(flagLower || '').toLowerCase();
    $$('#flagPad .flag, #preFlagRow .flag').forEach(btn => {
      btn.classList.toggle('is-active', (btn.dataset.flag || '').toLowerCase() === want);
    });
  }

  // -------------------------- Controls ---------------------------------
  async function startPrep() {
    await api('/race/control/start_prep', { method: 'POST' });
    const st = await api('/race/state');
    if ((st.countdown_from_s || 0) > 0) {
      countdownAnchor = Date.now() / 1000 + (st.countdown_from_s || 0);
    } else {
      countdownAnchor = null;
    }
    renderState(st);
  }

  async function startRace() {
    await api('/race/control/start_race', { method: 'POST' });
    countdownAnchor = null;
    renderState(await api('/race/state'));
  }

  async function endRace() {
    if (!confirm('End race and throw checkered?')) return;
    await api('/race/control/end_race', { method: 'POST' });
    renderState(await api('/race/state'));
  }

  async function abortReset() {
    if (!confirm('Abort & reset to PRE? Laps/seen will be cleared.')) return;
    // Prefer dedicated control endpoint; UI also supports legacy reset route
    try {
      await api('/race/control/abort_reset', { method: 'POST' });
    } catch (_) {
      await api('/race/reset_session', { method: 'POST' });
    }
    countdownAnchor = null;
    renderState(await api('/race/state'));
  }

  async function setActiveFlag(flagLower) {
    const upper = String(flagLower || 'pre').toUpperCase();
    await api('/engine/flag', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flag: upper })
    });
    refreshState();
  }

  // Hold-to-confirm helper (for red/checkered).
  function bindHoldToConfirm(btn, ms = 850) {
    let t = null;
    const arm = () => {
      btn.dataset.hold = 'arming';
      t = setTimeout(() => { btn.dataset.hold=''; btn.click(); }, ms);
    };
    const disarm = () => { btn.dataset.hold=''; if (t) { clearTimeout(t); t=null; } };
    btn.addEventListener('mousedown', arm);
    btn.addEventListener('touchstart', arm);
    ['mouseup','mouseleave','touchend','touchcancel'].forEach(ev => btn.addEventListener(ev, disarm));
  }

  function bindFlags() {
    $$('#flagPad .flag, #preFlagRow .flag').forEach(btn => {
      const flag = btn.dataset.flag;
      if (!flag) return;
      if (btn.classList.contains('hold')) bindHoldToConfirm(btn, 850);
      btn.addEventListener('click', () => setActiveFlag(flag));
    });
  }

  // --- wire buttons once ---
  function bindControls() {
    if (els.btnPreRace) els.btnPreRace.addEventListener('click', startPrep);
    if (els.btnGoGreen) els.btnGoGreen.addEventListener('click', startRace);
    if (els.btnStartRace) els.btnStartRace.addEventListener('click', startRace);
    if (els.btnEndRace) els.btnEndRace.addEventListener('click', endRace);
    if (els.btnAbortList && els.btnAbortList.length) els.btnAbortList.forEach(b => b.addEventListener('click', abortReset));
    if (els.btnStartPrep) els.btnStartPrep.addEventListener('click', startPrep);
    if (els.btnAbortReset) els.btnAbortReset.addEventListener('click', abortReset);
    if (els.btnClockMode) els.btnClockMode.addEventListener('click', toggleClockMode);
    bindFlags();

    // Keyboard shortcuts: P,G,Y,R,B,W,C
    document.addEventListener('keydown', (e) => {
      const map = {
        KeyP: 'pre',
        KeyG: 'green',
        KeyY: 'yellow',
        KeyR: 'red',
        KeyB: 'blue',
        KeyW: 'white',
        KeyC: 'checkered',
      };
      const f = map[e.code];
      if (f) { e.preventDefault(); setActiveFlag(f); }
    });
  }

  // --- heartbeat ---
  let tick = null;
  function startPolling() {
    if (tick) clearInterval(tick);
    refreshState();
    tick = setInterval(refreshState, 1000);
  }

  // init
  document.addEventListener('DOMContentLoaded', () => {
    bindControls();
    startPolling();
  });
})();
