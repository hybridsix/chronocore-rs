/* ==========================================================================
   CCRS Race Control — UI-first controller (patched)
   --------------------------------------------------------------------------
   This version simplifies the top bar and clock behavior, and adds clear,
   glanceable flag state directly on the flag buttons.

   WHAT'S NEW (compared to earlier drafts):
   1) Unified "Pre‑race" button (auto: Standing→parade, Rolling→validation).
   2) Right pane swaps automatically:
        - PREP/IDLE  -> "Seen" panel
        - GREEN/CHK  -> "Lap feed" panel
   3) Single clock field with a small toggle pill:
        - Time-limited: "Remaining" <-> "Elapsed" (hh:mm:ss)
        - Lap-limited : "Laps rem." <-> "Elapsed"
   4) Limit chip shows the active limit ("15:00" or "30 laps").
   5) Flag buttons themselves indicate active state:
        - Non-green pulse + glow, Green bright, Pre flag supported.
   6) Keyboard shortcuts: P,G,Y,R,B,W,C to select flags quickly.

   SAFE TO MERGE:
   - IDs that are missing in your current HTML safely no-op.
   - Server calls are not included here; wire them where indicated.
   - If you still show an "Active Flag" pill elsewhere, you can delete it.
   ========================================================================== */

(() => {
  'use strict';

  // -------------------------- DOM helpers -------------------------------
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  // -------------------------- Elements ---------------------------------
  const els = {
    // Top toolbar
    startStanding : $('#startStanding'),
    startRolling  : $('#startRolling'),
    btnPreRace    : $('#btnPreRace'),
    btnCountdown10: $('#btnCountdown10'),
    btnGoGreen    : $('#btnGoGreen'),
    btnAbort      : $('#btnAbort'),
    btnClockMode  : $('#btnClockMode'),
    clockDisplay  : $('#clockDisplay'),
    limitChip     : $('#limitChip'),

    // Right pane panels
    panelSeen     : $('#panelSeen'),
    panelFeed     : $('#panelFeed'),
    seenCount     : $('#seenCount'),
    seenTotal     : $('#seenTotal'),
    seenList      : $('#seenList'),
    lapFeed       : $('#lapFeed'),

    // Flags
    flagPad       : $('#flagPad'),
    preFlagRow    : $('#preFlagRow'),
  };

  // -------------------------- State ------------------------------------
  const state = {
    // Session flow
    phase         : 'IDLE',         // IDLE | PREP | ARMED | GREEN | CHECKERED
    startMethod   : 'standing',     // 'standing' | 'rolling'
    activeFlag    : 'off',          // pre | green | yellow | red | blue | white | checkered | off

    // Timing
    elapsed_s     : 0,              // monotonically increasing while GREEN
    // limit: set this from the selected mode when wiring backend:
    //   { type: 'time', value_s: 900 }
    //   { type: 'laps', value_laps: 30 }
    limit         : { type: 'time', value_s: 15*60, value_laps: null },
    completed_laps: 0,              // wire this from engine if you show laps remaining
    clockMode     : 'remaining',    // 'remaining' | 'elapsed' | 'lapsRemaining'

    // "Seen" accounting during PREP
    seen          : new Set(),
    seenTotalNum  : 12,             // placeholder until wired to entrants count
  };

  // -------------------------- Utilities --------------------------------
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  // Format 00:00:00
  function toHHMMSS(totalSeconds) {
    const n = Math.max(0, Math.floor(totalSeconds));
    const h = Math.floor(n / 3600);
    const m = Math.floor((n % 3600) / 60);
    const s = n % 60;
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  }

  // Simple audio with fallback path. Replace with your engine-verified sounds later.
  async function playSound(name) {
    const tryPaths = [`/config/sounds/${name}`, `/assets/sounds/${name}`];
    for (const p of tryPaths) {
      try { const a = new Audio(p); await a.play(); return true; } catch (_) {}
    }
    return false;
  }

  // -------------------------- Painting ---------------------------------
  function paintActiveFlagButton() {
    // Mark the active flag button (includes #preFlagRow if present)
    $$('#flagPad .flag, #preFlagRow .flag').forEach(btn => {
      btn.classList.toggle('is-active', btn.dataset.flag === state.activeFlag);
    });
  }

  function paintPanelsByPhase() {
    const showFeed = (state.phase === 'GREEN' || state.phase === 'CHECKERED');
    if (els.panelSeen) els.panelSeen.classList.toggle('hidden', showFeed);
    if (els.panelFeed) els.panelFeed.classList.toggle('hidden', !showFeed);
  }

  function paintClock() {
    if (!els.clockDisplay || !els.btnClockMode) return;

    let label = 'Remaining';
    let text  = '00:00:00';

    if (state.limit.type === 'time') {
      const elapsed   = state.elapsed_s;
      const remaining = Math.max(0, (state.limit.value_s || 0) - elapsed);

      if (state.clockMode === 'elapsed') {
        label = 'Elapsed';
        text  = toHHMMSS(elapsed);
      } else {
        label = 'Remaining';
        text  = toHHMMSS(remaining);
      }
    } else { // laps-limited
      if (state.clockMode === 'elapsed') {
        label = 'Elapsed';
        text  = toHHMMSS(state.elapsed_s);
      } else {
        label = 'Laps rem.';
        const total = Number(state.limit.value_laps || 0);
        const done  = Number(state.completed_laps || 0);
        const rem   = clamp(total - done, 0, total);
        text  = String(rem).padStart(2, '0');
      }
    }

    els.btnClockMode.textContent = label;
    els.clockDisplay.textContent = text;
  }

  function paintLimitChip() {
    if (!els.limitChip) return;
    if (state.limit.type === 'time') {
      els.limitChip.textContent = 'Limit: ' + toHHMMSS(state.limit.value_s || 0);
    } else {
      const laps = Number(state.limit.value_laps || 0);
      els.limitChip.textContent = 'Limit: ' + laps + ' laps';
    }
  }

  // Button enable/disable per phase
  function paintPhase() {
    const p = state.phase;

    if (els.btnPreRace) {
      // Pre-race button shows "Stop pre‑race" while active
      els.btnPreRace.textContent = (p === 'PREP') ? 'Stop pre‑race' : 'Pre‑race';
    }

    if (els.btnCountdown10) els.btnCountdown10.disabled = !(p === 'ARMED');
    if (els.btnGoGreen)     els.btnGoGreen.disabled     = !(p === 'ARMED');
    if (els.btnAbort)       els.btnAbort.disabled       = (p === 'IDLE' || p === 'CHECKERED');

    // Flags enabled only during GREEN (except allow checkered during CHECKERED).
    if (els.flagPad) {
      els.flagPad.querySelectorAll('.flag').forEach(btn => {
        const f = btn.dataset.flag;
        btn.disabled = !(p === 'GREEN') && !(p === 'CHECKERED' && f === 'checkered');
      });
    }
    // Pre-race button in the stack is always clickable
    const preBtn = $('#preFlagRow .flag');
    if (preBtn) preBtn.disabled = false;

    paintPanelsByPhase();
    paintActiveFlagButton();
    paintClock();
  }

  // -------------------------- Phase + flow -----------------------------
  function setPhase(next) {
    state.phase = next;
    paintPhase();
  }

  // Pre-race start (auto-kind: standing→parade, rolling→validation)
  function startPreRaceAuto() {
    const kind = (state.startMethod === 'rolling') ? 'validation' : 'parade';
    // back-end: POST /race/prep/start { kind }
    enterPrep(kind);
    if (els.btnPreRace) els.btnPreRace.textContent = 'Stop pre‑race';
  }

  // Pre-race stop → ARMED
  function stopPreRace() {
    // back-end: POST /race/prep/stop
    exitPrepToArmed();
    if (els.btnPreRace) els.btnPreRace.textContent = 'Pre‑race';
  }

  function enterPrep(kind) {
    setPhase('PREP');
    state.activeFlag = 'pre';         // reflect on UI
    state.seen.clear();

    // Simulate "seen" filling in (UI-only demo)
    let i = 0, total = Number(state.seenTotalNum || 0);
    const runner = setInterval(() => {
      if (state.phase !== 'PREP' || i >= total) return clearInterval(runner);
      const id = `car-${i+1}`;
      state.seen.add(id); i++;
      if (els.seenCount) els.seenCount.textContent = String(state.seen.size);
      if (els.seenTotal) els.seenTotal.textContent = String(total);
      if (els.seenList) {
        const li = document.createElement('li');
        li.textContent = `${id} — last seen just now`;
        els.seenList.appendChild(li);
      }
      paintActiveFlagButton();
    }, 600);
  }

  function exitPrepToArmed() {
    if (state.phase !== 'PREP') return;
    setPhase('ARMED');
  }

  function goGreen() {
    setPhase('GREEN');
    setActiveFlag('green');
    playSound('start_horn.wav');
    startTick();
  }

  function abortSession() {
    // hard reset to IDLE (UI-level)
    setActiveFlag('off');
    setPhase('IDLE');
    stopTick();
    state.elapsed_s = 0;
    // Repaint clock/panels
    paintClock();
    paintPanelsByPhase();
  }

  // -------------------------- Timing ----------------------------------
  let tickHandle = null;
  function startTick() {
    stopTick();
    tickHandle = setInterval(() => {
      if (state.phase === 'GREEN') {
        state.elapsed_s += 1;
      }
      // If you still update other legacy fields elsewhere, do that here.
      paintClock();
    }, 1000);
  }
  function stopTick() { if (tickHandle) { clearInterval(tickHandle); tickHandle = null; } }

  // -------------------------- Flags -----------------------------------
  function setActiveFlag(flag) {
    state.activeFlag = flag;
    // back-end: POST /race/flag { active: flag }
    paintActiveFlagButton();
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

  // -------------------------- Bindings --------------------------------
  function bindUi() {
    if (els.startStanding) els.startStanding.addEventListener('change', () => {
      if (els.startStanding.checked) { state.startMethod = 'standing'; paintPhase(); }
    });
    if (els.startRolling) els.startRolling.addEventListener('change', () => {
      if (els.startRolling.checked)  { state.startMethod = 'rolling';  paintPhase(); }
    });

    if (els.btnPreRace) els.btnPreRace.addEventListener('click', () => {
      if (state.phase === 'PREP') stopPreRace(); else startPreRaceAuto();
    });

    if (els.btnCountdown10) els.btnCountdown10.addEventListener('click', async () => {
      if (state.phase !== 'ARMED') return;
      for (let s=10; s>=1; s--) {
        await playSound('countdown_beep.wav');
        await sleep(1000);
      }
      await playSound('start_horn.wav');
    });

    if (els.btnGoGreen) els.btnGoGreen.addEventListener('click', goGreen);
    if (els.btnAbort)   els.btnAbort.addEventListener('click', abortSession);

    if (els.btnClockMode) els.btnClockMode.addEventListener('click', () => {
      if (state.limit.type === 'time') {
        state.clockMode = (state.clockMode === 'remaining') ? 'elapsed' : 'remaining';
      } else {
        state.clockMode = (state.clockMode === 'elapsed') ? 'lapsRemaining' : 'elapsed';
      }
      paintClock();
    });

    // Flags
    bindFlags();

    // Optional: keyboard shortcuts for flags
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

  // -------------------------- Boot ------------------------------------
  function boot() {
    paintLimitChip();
    paintPanelsByPhase();
    paintActiveFlagButton();
    paintClock();
    startTick();  // keeps the clock repainting; only counts up during GREEN
  }

  // Init
  bindUi();
  paintPhase();
  boot();
})();
