/* ==========================================================================
   CCRS Race Control — unified controller
   --------------------------------------------------------------------------
   - Polls /race/state once per second and renders clock/phase/flag.
   - Wires action buttons to backend (/race/control/*, /engine/flag).
   - Keeps flag pad visually synced and now enforces legal-press policy
     (buttons disabled unless allowed for the current phase).
   - Preserves existing behaviors: clock mode toggle, countdown handling,
     keyboard shortcuts, and last-lap feed hooks.
   ========================================================================== */

(() => {
  'use strict';

  // ----------------------------------------------------------------------
  // Tiny DOM + fetch helpers
  // ----------------------------------------------------------------------
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  // Minimal fetch helper that returns JSON or empty object on 204
  async function api(url, opts = {}) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    try { return await r.json(); } catch { return {}; }
  }

  // HH:MM:SS with optional leading minus (en-dash)
  function fmtClockHMS(sec) {
    const neg = sec < 0;
    const s = Math.abs(Math.floor(sec || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const sign = neg ? '–' : '';
    return `${sign}${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(r).padStart(2,'0')}`;
  }

  // ----------------------------------------------------------------------
  // Local state
  // ----------------------------------------------------------------------
  let countdownAnchor = null; // epoch seconds when 0 should occur (client-only)
  let lastState = null;

  // Persisted clock mode: 'remaining' | 'elapsed'
  let clockMode = (() => {
    const v = (localStorage.getItem('rc.clockMode') || 'remaining').toLowerCase();
    return (v === 'elapsed') ? 'elapsed' : 'remaining';
  })();

  // ----------------------------------------------------------------------
  // Element map (coexists with your current markup)
  // ----------------------------------------------------------------------
  const els = {
    // Action buttons (top bar)
    btnPreRace     : $('#btnPreRace'),
    btnStartPrep   : $('#btnStartPrep'),   // alias, if present
    btnStartRace   : $('#btnStartRace'),
    btnGoGreen     : $('#btnGoGreen'),
    btnEndRace     : $('#btnEndRace'),
    btnAbortReset  : $('#btnAbortReset'),
    btnAbortList   : $$('#btnAbortReset, .btnAbortReset'),

    // Clock display + mode toggle
    clockDisplay   : $('#raceClock'),
    btnClockMode   : $('#btnClockMode'),

    // Panels
    panelSeen      : $('#panelSeen'),
    panelFeed      : $('#panelFeed'),

    // Flags pad (buttons contain data-flag="...")
    flagPad        : $('#flagPad'),
    preFlagRow     : $('#preFlagRow'),
  };

  // ----------------------------------------------------------------------
  // Flag logic: policy + UI enforcement
  // ----------------------------------------------------------------------
  // NOTE: We keep exactly one definition of these. They’re referenced from renderState().
  function allowedFlagsForPhase(phase) {
    // phase: 'pre' | 'countdown' | 'green' | 'white' | 'checkered'
    switch ((phase || 'pre').toLowerCase()) {
      case 'pre':
      case 'countdown':
        // Before the race is actually running, only PRE is meaningful.
        return ['pre'];
      case 'green':
        // While racing, allow management flags (no jumping to PRE here).
        return ['yellow', 'red', 'blue', 'white', 'checkered'];
      case 'white':
        // Near the end; you can escalate or finish.
        return ['yellow', 'red', 'checkered', 'blue'];
      case 'checkered':
      default:
        // Race is done — flag pad is inert (use “Abort & reset” to go back).
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

  // Keep the active flag button visually highlighted
  function highlightActiveFlagButton(flagLower) {
    const allBtns = $$('#flagPad .flag, #preFlagRow .flag');
    allBtns.forEach(btn => {
      btn.classList.toggle('is-active', (btn.dataset.flag || '').toLowerCase() === flagLower);
    });
  }

  // Bind per-flag button actions once (delegated)
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

  // Update clock mode button affordance based on state
  function updateClockModeButton(st) {
    const btn = els.btnClockMode;
    if (!btn) return;

    const ph = st?.phase;
    const remaining = st?.clock?.remaining_s;

    // Disable during countdown or when no remaining exists (open-ended)
    const disable = (ph === 'countdown') || (remaining == null);
    btn.disabled = !!disable;

    // Label reflects what we’re currently showing
    const label = (disable && remaining == null)
      ? 'Elapsed'
      : (clockMode === 'elapsed' ? 'Elapsed' : 'Remaining');
    btn.textContent = label;
  }

  function renderState(st) {
    if (!st) return;
    lastState = st;

    // ---- Clock ----
    const elapsed   = st?.clock?.elapsed_s ?? 0;          // optional (engine-supplied)
    const remaining = st?.clock?.remaining_s;             // may be null (open-ended)

    updateClockModeButton(st);

    // Initialize the client-side countdown anchor from server state if needed.
    // This allows the UI to tick T-minus smoothly even if we only poll once/sec.
    const phaseLower = (st.phase || 'pre').toLowerCase();
    if (phaseLower === 'countdown' && !countdownAnchor) {
      const rem = Number(st.countdown_remaining_s ?? st.clock?.countdown_remaining_s ?? 0);
      if (rem > 0) {
        countdownAnchor = (Date.now() / 1000) + rem;
      }
    }

    // Prefer authoritative server clock if provided (top-level or nested).
    // This removes guesswork and ensures the UI matches backend truth:
    //   - negative ms during COUNTDOWN
    //   - positive ms during GREEN
    const srvClockMs = (typeof st.clock_ms === 'number') ? st.clock_ms : (
      (st.clock && typeof st.clock.clock_ms === 'number') ? st.clock.clock_ms : null
    );

    if (srvClockMs != null && els.clockDisplay) {
      // Convert ms → seconds (can be negative for T-minus)
      els.clockDisplay.textContent = fmtClockHMS(srvClockMs / 1000);
    } else if ((st.phase === 'countdown') && countdownAnchor) {
      // Fallback: compute T-minus from the local anchor if server ms is absent
      const now = Date.now() / 1000;
      const neg = Math.max(
        -(countdownAnchor - now),
        -Number(st.countdown_from_s || 0)
      );
      if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(neg);
    } else if (remaining == null) {
      // Open-ended race → always show elapsed
      if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(elapsed);
    } else {
      // Time-limited race → show either elapsed or remaining per user mode
      const show = (clockMode === 'elapsed') ? elapsed : remaining;
      if (els.clockDisplay) els.clockDisplay.textContent = fmtClockHMS(show);
    }

    // ---- Phase/flag → dataset for CSS and visual highlight ----
    document.body.dataset.phase = st.phase || 'pre';
    document.body.dataset.flag  = (st.flag || 'PRE').toUpperCase();

    // ---- Enable/disable action buttons (business rules) ----
    const ph = st.phase;
    if (els.btnPreRace)      els.btnPreRace.disabled      = (ph !== 'pre');
    if (els.btnStartPrep)    els.btnStartPrep.disabled    = (ph !== 'pre');
    if (els.btnStartRace)    els.btnStartRace.disabled    = !(ph === 'pre' || ph === 'countdown');
    if (els.btnGoGreen)      els.btnGoGreen.disabled      = !(ph === 'pre' || ph === 'countdown'); // legacy/alias
    if (els.btnEndRace)      els.btnEndRace.disabled      = !(ph === 'green' || ph === 'white');
    if (els.btnAbortReset)   els.btnAbortReset.disabled   = false;
    if (els.btnAbortList?.length) els.btnAbortList.forEach(b => b.disabled = false);

    // ---- Panels: show feed only when racing or checkered ----
    const showFeed = (ph === 'green' || ph === 'white' || ph === 'checkered');
    if (els.panelSeen) els.panelSeen.classList.toggle('hidden', showFeed);
    if (els.panelFeed) els.panelFeed.classList.toggle('hidden', !showFeed);

    // ---- Flag pad rules + highlight ----
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
    // Ensure PRE, but don’t nuke roster
    await api('/race/control/start_prep', { method: 'POST' });
    countdownAnchor = null;            // clear any stale client anchor
    renderState(await api('/race/state'));
  }

  async function startRace() {
    // Backend will either enter COUNTDOWN (if countdown_from_s > 0) or GREEN immediately.
    const res = await api('/race/control/start_race', { method: 'POST' });
    // If we entered countdown, the server will report countdown_remaining_s on next poll.
    // We still pre-warm the anchor for zero-latency UI feedback.
    if ((res?.phase || '').toLowerCase() === 'countdown') {
      const cd = Number(res?.countdown_from_s || 0);
      if (cd > 0) countdownAnchor = (Date.now() / 1000) + cd;
    }
    // Refresh from /race/state so we render from authoritative server values.
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
      // legacy fallback
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
      body: JSON.stringify({ flag: upper }),
    });
    // Re-render to reflect any engine-side changes; phase rules still apply.
    renderState(await api('/race/state'));
  }

  function bindControls() {
    if (els.btnPreRace)    els.btnPreRace.addEventListener('click', startPrep);
    if (els.btnStartPrep)  els.btnStartPrep.addEventListener('click', startPrep);
    if (els.btnStartRace)  els.btnStartRace.addEventListener('click', startRace);
    if (els.btnGoGreen)    els.btnGoGreen.addEventListener('click', startRace); // legacy/alias
    if (els.btnEndRace)    els.btnEndRace.addEventListener('click', endRace);
    if (els.btnAbortReset) els.btnAbortReset.addEventListener('click', abortReset);
    if (els.btnAbortList?.length) els.btnAbortList.forEach(b => b.addEventListener('click', abortReset));
    if (els.btnClockMode)  els.btnClockMode.addEventListener('click', toggleClockMode);
    bindFlags();

    // Keyboard shortcuts: P,G,Y,R,B,W,C → set flag (still subject to policy)
    document.addEventListener('keydown', (e) => {
      const map = {
        KeyP: 'pre',
        KeyG: 'green',
        KeyY: 'yellow',
        KeyR: 'red',
        KeyB: 'blue',
        KeyW: 'white',
        KeyC: 'checkered'
      };
      const f = map[e.code];
      if (!f) return;
      e.preventDefault();

      // Enforce the same policy as the pad (don’t allow illegal transitions)
      const allowed = new Set(allowedFlagsForPhase(document.body.dataset.phase || 'pre'));
      if (!allowed.has(f)) return;

      setActiveFlag(f);
    });
  }

  // ----------------------------------------------------------------------
  // Heartbeat (poll /race/state every second)
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
