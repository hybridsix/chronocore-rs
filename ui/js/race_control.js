/* =====================================================================
   Race Control — page logic
   - Exclusive flags
   - Pre-race parade/validation flow
   - Basic timers (count during GREEN)
   No backend calls yet. Wire fetch/WS later.
   ===================================================================== */
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const els = {
    // toolbar
    startStanding: $('#startStanding'),
    startRolling:  $('#startRolling'),
    btnStartParade: $('#btnStartParade'),
    btnStartValidation: $('#btnStartValidation'),
    btnStopPrep: $('#btnStopPrep'),
    btnCountdown10: $('#btnCountdown10'),
    btnGoGreen: $('#btnGoGreen'),
    btnAbort: $('#btnAbort'),
    beepOnRead: $('#beepOnRead'),

    // live
    tElapsed: $('#tElapsed'), tRemaining: $('#tRemaining'),
    seenCount: $('#seenCount'), seenTotal: $('#seenTotal'), seenList: $('#seenList'),
    lapFeed: $('#lapFeed'),

    // flags
    flagPad: $('#flagPad'),
    flagChip: $('#flagChip'),
  };

  const state = {
    phase: 'IDLE',             // IDLE | PREP | ARMED | GREEN | CHECKERED
    startMethod: 'standing',
    activeFlag: 'off',
    seen: new Set(), seenTotal: 0,
    elapsed_s: 0, remaining_s: 15*60,
  };

  // --- helpers ---
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  const fmtMMSS = (t) => { t=Math.max(0, t|0); const m=(t/60)|0, s=t%60; return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0'); };

  async function playSound(name) {
    for (const p of [`/config/sounds/${name}`, `/assets/sounds/${name}`]) {
      try { const a=new Audio(p); await a.play(); return true; } catch(e) {}
    } return false;
  }

  function paintFlagChip() {
    els.flagChip.dataset.flag = state.activeFlag || 'off';
    els.flagChip.textContent = (state.activeFlag || 'off').toUpperCase();
  }

  function paintPhase() {
    const p = state.phase;
    // enable/disable buttons by phase and start method
    els.btnStartParade.disabled     = !((p==='IDLE'||p==='PREP') && state.startMethod==='standing');
    els.btnStartValidation.disabled = !((p==='IDLE'||p==='PREP') && state.startMethod==='rolling');
    els.btnStopPrep.disabled        = !(p==='PREP');
    els.btnCountdown10.disabled     = !(p==='ARMED');
    els.btnGoGreen.disabled         = !(p==='ARMED');
    els.btnAbort.disabled           = (p==='IDLE'||p==='CHECKERED');

    // flags only during GREEN (except allow checkered in CHECKERED)
    els.flagPad.querySelectorAll('.flag').forEach(btn => {
      const f = btn.dataset.flag;
      btn.disabled = !(p==='GREEN') && !(p==='CHECKERED' && f==='checkered');
    });
  }

  // timers
  let tick = null;
  function startTick() {
    stopTick();
    tick = setInterval(() => {
      if (state.phase === 'GREEN') {
        state.elapsed_s += 1;
        state.remaining_s = clamp(state.remaining_s - 1, 0, 1e9);
      }
      els.tElapsed.textContent = fmtMMSS(state.elapsed_s);
      els.tRemaining.textContent = fmtMMSS(state.remaining_s);
    }, 1000);
  }
  function stopTick() { if (tick) { clearInterval(tick); tick = null; } }

  // phase changes
  function setPhase(next) { state.phase = next; paintPhase(); }
  function setActiveFlag(flag) { state.activeFlag = flag; paintFlagChip(); }

  // pre-race demo
  function enterPrep(kind) {
    setPhase('PREP');
    state.seen.clear();
    state.seenTotal = state.seenTotal || 12; // placeholder until wired to entrants
    let i=0, total=state.seenTotal;
    const runner = setInterval(() => {
      if (state.phase !== 'PREP' || i>=total) return clearInterval(runner);
      const id = `car-${i+1}`;
      state.seen.add(id); i++;
      paintSeen();
      if (els.beepOnRead.checked) playSound('lap_beep.wav');
    }, 600);
  }
  function exitPrepToArmed(){ if (state.phase === 'PREP') setPhase('ARMED'); }
  function goGreen(){
    setPhase('GREEN');
    setActiveFlag('green');
    playSound('start_horn.wav');
    startTick();
  }
  function abortSession(){
    setPhase('IDLE');
    setActiveFlag('off');
    stopTick();
    state.elapsed_s = 0;
    state.remaining_s = 15*60;
    els.tElapsed.textContent = fmtMMSS(0);
    els.tRemaining.textContent = fmtMMSS(state.remaining_s);
  }

  function paintSeen(){
    els.seenCount.textContent = String(state.seen.size);
    els.seenTotal.textContent = String(state.seenTotal);
    els.seenList.innerHTML = '';
    for (const id of state.seen) {
      const li = document.createElement('li');
      li.textContent = `${id} — last seen just now`;
      els.seenList.appendChild(li);
    }
  }

  // hold-to-confirm for Red/Checkered
  function bindHoldToConfirm(btn, ms=850){
    let t=null;
    const arm=()=>{ btn.dataset.hold='arming'; t=setTimeout(()=>{ btn.dataset.hold=''; btn.click(); }, ms); };
    const disarm=()=>{ btn.dataset.hold=''; if(t) clearTimeout(t), t=null; };
    btn.addEventListener('mousedown',arm); btn.addEventListener('touchstart',arm);
    ['mouseup','mouseleave','touchend','touchcancel'].forEach(ev=>btn.addEventListener(ev,disarm));
  }

  function bindFlags(){
    els.flagPad.querySelectorAll('.flag').forEach(btn => {
      const flag = btn.dataset.flag;
      if (btn.classList.contains('hold')) bindHoldToConfirm(btn, 850);
      btn.addEventListener('click', () => setActiveFlag(flag));
    });
  }

  function bindUi(){
    els.startStanding.addEventListener('change', ()=>{ if (els.startStanding.checked){ state.startMethod='standing'; paintPhase(); }});
    els.startRolling.addEventListener('change',  ()=>{ if (els.startRolling.checked){  state.startMethod='rolling';  paintPhase(); }});
    els.btnStartParade.addEventListener('click',     ()=> enterPrep('parade'));
    els.btnStartValidation.addEventListener('click', ()=> enterPrep('validation'));
    els.btnStopPrep.addEventListener('click',        exitPrepToArmed);
    els.btnCountdown10.addEventListener('click', async ()=>{
      if (state.phase!=='ARMED') return;
      for(let s=10;s>=1;s--){ await playSound('countdown_beep.wav'); await wait(1000); }
      await playSound('start_horn.wav');
    });
    els.btnGoGreen.addEventListener('click', goGreen);
    els.btnAbort.addEventListener('click',  abortSession);
    bindFlags();
  }

  function boot(){ paintFlagChip(); paintPhase(); paintSeen(); startTick(); }

  bindUi(); boot();
})();