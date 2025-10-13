/* ==========================================================================
   Race Setup — controller (v1.1)
   - Single "Mode" select (built-ins from race_modes.yaml) + trailing "Custom"
   - Limit = time | laps (time=0 => unlimited/free-play)
   - Soft end default true for Qualifying; hidden when time==0
   - Timeout padding defaults to 0
   - "Save as Mode" posts to backend to update race_modes.yaml
   - Header/nav behavior matches Diagnostics (handled inline in HTML)
   ========================================================================== */

(function () {
  'use strict';

  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const els = {
    form: $('#raceForm'),
    eventLabel: $('#eventLabel'),
    sessionLabel: $('#sessionLabel'),

    // Mode / custom
    modeSelect: $('#modeSelect'),
    customSave: $('#customSave'),
    customLabel: $('#customLabel'),
    btnSaveMode: $('#btnSaveMode'),

    // Limit
    optTime: $('#limitTime'),
    optLaps: $('#limitLaps'),
    rowTimeValue: $('#rowTimeValue'),
    rowSoftEnd: $('#rowSoftEnd'),
    rowLapsValue: $('#rowLapsValue'),
    limitValueS: $('#limitValueS'),
    limitValueLaps: $('#limitValueLaps'),
    limitSoftEnd: $('#limitSoftEnd'),

    // Rank + filter
    rankMethod: $('#rankMethod'),
    minLapS: $('#minLapS'),

    // Countdowns
    startEnabled: $('#startEnabled'),
    startFromS: $('#startFromS'),
    endEnabled: $('#endEnabled'),
    endFromS: $('#endFromS'),
    timeoutS: $('#timeoutS'),

    // Announcements
    lapIndication: $('#lapIndication'),
    rankEnabled: $('#rankEnabled'),
    rankInterval: $('#rankInterval'),
    rankChangeSpeech: $('#rankChangeSpeech'),
    bestLapSpeech: $('#bestLapSpeech'),

    // Misc
    entrantsCount: $('#entrantsCount'),
    decoderBypass: $('#decoderBypass'),

    // Readiness + actions
    chipDB: $('#chipDB'),
    chipDecoder: $('#chipDecoder'),
    chipEntrants: $('#chipEntrants'),
    summaryText: $('#summaryText'),
    btnSaveConfig: $('#btnSaveConfig'),
    btnStartRace: $('#btnStartRace'),

    // Tests
    btnTestBeep: $('#btnTestBeep'),
    btnTestHorn: $('#btnTestHorn'),
  };

  let MODES = {};
  let currentModeId = null;
  let config = {};

  const CUSTOM_ID = '__custom__';

  const clamp = (n, lo, hi) => Math.max(lo, Math.min(n, hi));

  function setChip(el, status, msg) {
    if (!el) return;
    el.classList.remove('ok', 'warn', 'err');
    if (status) el.classList.add(status);
    el.textContent = msg;
  }

  function secondsToHuman(s) {
    if (!Number.isFinite(s) || s < 0) return '—';
    if (s === 0) return 'Unlimited';
    const m = Math.floor(s / 60);
    const ss = s % 60;
    return `${m}m ${ss}s`;
  }

  function summarize(cfg) {
    const parts = [];
    if (cfg.limit?.type === 'time') {
      parts.push(`Time ${secondsToHuman(cfg.limit.value_s)}`);
      if (cfg.limit.value_s > 0 && cfg.limit.soft_end) parts.push('soft');
    } else if (cfg.limit?.type === 'laps') {
      parts.push(`${cfg.limit.value_laps} laps`);
    }
    parts.push(cfg.rank_method === 'best_lap' ? 'Rank: Best Lap' : 'Rank: Total Laps');
    parts.push(`MinLap ${cfg.min_lap_s?.toFixed ? cfg.min_lap_s.toFixed(1) : cfg.min_lap_s}s`);
    if (cfg.relay?.driver_change_interval_s) {
      parts.push(`Driver change ${Math.round(cfg.relay.driver_change_interval_s / 60)}m`);
    }
    return parts.join(' • ');
  }

  function setCustomEnabled(on) {
  // Grey whole row + disable its controls
  els.customSave.classList.toggle('is-disabled', !on);
  els.customLabel.disabled = !on;
  els.btnSaveMode.disabled = !on;
}

  // ------------------------------------------------------------------------
  // Load built-in modes from backend; add trailing Custom option
  // ------------------------------------------------------------------------
  async function loadModes() {
    const endpoints = [
      '/setup/race_modes',     // preferred: server returns {modes:{...}}
      '/api/race/modes',
      '../config/race_modes.json',
    ];
    for (const url of endpoints) {
      try {
        const r = await fetch(url, { cache: 'no-cache' });
        if (r.ok) {
          const data = await r.json();
          if (data && data.modes) return data.modes;
        }
      } catch (_) { /* try next */ }
    }
    console.warn('[RaceSetup] Using minimal fallback modes.');
    return {
      sprint: {
        label: 'Sprint (15 min)',
        limit: { type: 'time', value_s: 900, soft_end: false },
        rank_method: 'total_laps',
        min_lap_s: 4.1,
        countdown: { start_enabled: true, start_from_s: 10, end_enabled: false, timeout_s: 0 },
        announcements: { lap_indication: 'beep', rank_enabled: false, rank_interval_laps: 0, rank_change_speech: false, best_lap_speech: false },
      },
      practice: {
        label: 'Practice (Free Play)',
        limit: { type: 'time', value_s: 0, soft_end: true },
        rank_method: 'best_lap',
        min_lap_s: 4.1,
        countdown: { start_enabled: false, end_enabled: false, timeout_s: 0 },
        announcements: { lap_indication: 'beep', rank_enabled: false, rank_interval_laps: 0, rank_change_speech: false, best_lap_speech: false },
      },
    };
  }

  function paintModes(modes) {
    const sel = els.modeSelect;
    if (!sel) return;
    sel.innerHTML = '';
    Object.entries(modes).forEach(([id, m]) => {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = m.label || id;
      sel.appendChild(opt);
    });
    // Add trailing 'Custom' option
    const optC = document.createElement('option');
    optC.value = CUSTOM_ID;
    optC.textContent = 'Custom…';
    sel.appendChild(optC);
  }

  function slugifyLabel(label) {
  const slug = (label || '')
    .toLowerCase()
    .normalize('NFKD').replace(/[\u0300-\u036f]/g,'')
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
  return slug || 'custom-mode';
}



  // ------------------------------------------------------------------------
  // Config <-> Form binding
  // ------------------------------------------------------------------------
  function applyModeToForm(modeId) {
    const m = MODES[modeId];
    currentModeId = modeId;

    const isCustom = (modeId === CUSTOM_ID);
    // Always visible; just toggle enabled state
    setCustomEnabled(isCustom);
    if (isCustom) {
      els.customLabel.value = '';
    }

    // If Custom, start from a sensible baseline (Sprint) if available
    const base = isCustom ? (MODES['sprint'] || Object.values(MODES)[0] || {}) : m || {};

    // Limit
    if (base.limit?.type === 'laps') {
      els.optLaps.checked = true;
      els.rowLapsValue.hidden = false;
      els.rowTimeValue.hidden = true;
      els.rowSoftEnd.hidden  = true;
      els.limitValueLaps.value = base.limit.value_laps ?? 10;
    } else {
      // time (including time=0 for free play)
      els.optTime.checked = true;
      els.rowLapsValue.hidden = true;
      els.rowTimeValue.hidden = false;
      const val = base.limit?.value_s ?? 900;
      els.limitValueS.value = Math.round(val / 60);
      // Show soft_end only when time > 0, and default it per preset; otherwise hide
      const showSoft = (val > 0);
      els.rowSoftEnd.hidden = !showSoft;
      els.limitSoftEnd.checked = !!base.limit?.soft_end && showSoft;
    }

    // Rank + filter
    els.rankMethod.value = base.rank_method || 'total_laps';
    els.minLapS.value = base.min_lap_s ?? 4.1;

    // Countdowns (timeout default 0)
    els.startEnabled.checked = !!base.countdown?.start_enabled;
    els.startFromS.value = base.countdown?.start_from_s ?? 10;
    els.endEnabled.checked = !!base.countdown?.end_enabled;
    els.endFromS.value = base.countdown?.end_from_s ?? 10;
    els.timeoutS.value = base.countdown?.timeout_s ?? 0;

    // Announcements
    els.lapIndication.value = base.announcements?.lap_indication ?? 'beep';
    els.rankEnabled.checked = !!base.announcements?.rank_enabled;
    els.rankInterval.value = base.announcements?.rank_interval_laps ?? 4;
    els.rankChangeSpeech.checked = !!base.announcements?.rank_change_speech;
    els.bestLapSpeech.checked = !!base.announcements?.best_lap_speech;

    // Relay passthrough
    config = formToConfig();
    paintSummary();
    updateLimitUi(els);
  }


  function updateLimitUi(els) {
    const isTime = els.optTime.checked;

    // Mutually exclusive entry fields
    els.limitValueS.disabled = !isTime;
    els.limitValueLaps.disabled = isTime;

    // Grey entire rows accordingly
    els.rowTimeValue.classList.toggle('is-disabled', !isTime);
    els.rowLapsValue.classList.toggle('is-disabled', isTime);

    // Soft end applies only when time > 0
    const t = parseInt(els.limitValueS.value || '0', 10);
    const softEnabled = isTime && t > 0;
    els.limitSoftEnd.disabled = !softEnabled;
    els.rowSoftEnd.classList.toggle('is-disabled', !softEnabled);
  }



  function formToConfig() {
    const isTime = els.optTime.checked;
    const timeMin = clamp(parseInt(els.limitValueS.value, 10) || 0, 0, 6 * 60);
    const timeVal = timeMin * 60;
    const lapsVal = clamp(parseInt(els.limitValueLaps.value, 10) || 1, 1, 2000);

    const cfg = {
      event_label: els.eventLabel?.value?.trim() || '',
      session_label: els.sessionLabel?.value?.trim() || '',
      decoder_bypass: !!els.decoderBypass?.checked,

      mode_id: currentModeId, // informational

      limit: isTime ? {
        type: 'time',
        value_s: timeVal,
        soft_end: (timeVal > 0) ? !!els.limitSoftEnd.checked : true /* irrelevant at 0 */
      } : {
        type: 'laps',
        value_laps: lapsVal
      },

      rank_method: els.rankMethod.value,
      min_lap_s: parseFloat(els.minLapS.value) || 0,

      countdown: {
        start_enabled: !!els.startEnabled.checked,
        start_from_s: Math.max(1, parseInt(els.startFromS.value, 10) || 10),
        end_enabled: !!els.endEnabled.checked,
        end_from_s: Math.max(1, parseInt(els.endFromS.value, 10) || 10),
        timeout_s: Math.max(0, parseInt(els.timeoutS.value, 10) || 0),
      },

      announcements: {
        lap_indication: els.lapIndication.value,
        rank_enabled: !!els.rankEnabled.checked,
        rank_interval_laps: Math.max(0, parseInt(els.rankInterval.value, 10) || 0),
        rank_change_speech: !!els.rankChangeSpeech.checked,
        best_lap_speech: !!els.bestLapSpeech.checked,
      },

      relay: MODES[currentModeId]?.relay || null,
    };

    // When time==0, hide soft-end concerns (treated as free play)
    if (cfg.limit.type === 'time' && cfg.limit.value_s === 0) {
      cfg.countdown.start_enabled = false;
      cfg.countdown.end_enabled = false;
      cfg.countdown.timeout_s = 0;
    }

    return cfg;
  }

  function paintSummary() {
    if (!els.summaryText) return;
    els.summaryText.textContent = summarize(config);
  }

  function bindLiveUpdates() {
    els.form?.addEventListener('input', () => {

      // Toggle visibility of time/laps rows
      const isTime = els.optTime.checked;
      els.rowTimeValue.hidden = !isTime;
      els.rowLapsValue.hidden = isTime;

      // Soft end shows only when time > 0
      if (isTime) {
        const timeVal = (parseInt(els.limitValueS.value || '0', 10) || 0) * 60;
        els.rowSoftEnd.hidden = !(timeVal > 0);
      } else {
        els.rowSoftEnd.hidden = true;
      }

      updateLimitUi(els);
      formToConfig();
      paintSummary();
    });

    els.modeSelect?.addEventListener('change', (e) => {
      applyModeToForm(e.target.value);
    });
    
  }

  // ------------------------------------------------------------------------
  // Custom Mode save (writes back to race_modes.yaml via backend)
  // ------------------------------------------------------------------------
  function bindCustomSave() {
    const doSave = async () => {
      // Ignore if not in Custom mode (row is disabled)
      if (els.btnSaveMode.disabled) return;

      const label = (els.customLabel.value || '').trim();
      if (!label) { alert('Provide a mode name.'); return; }

      const id = slugifyLabel(label);        // you already have this helper
      const cfg = formToConfig();
      const mode = {
        label,
        limit: cfg.limit,
        rank_method: cfg.rank_method,
        min_lap_s: cfg.min_lap_s,
        countdown: cfg.countdown,
        announcements: cfg.announcements,
        ...(cfg.relay ? { relay: cfg.relay } : {}),
      };

      if (MODES[id] && !confirm(`Mode “${label}” already exists. Overwrite it?`)) return;

      try {
        const r = await fetch('/setup/race_modes/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, mode }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);

        MODES[id] = mode;                 // update local list
        paintModes(MODES);
        els.modeSelect.value = id;        // switch to saved mode
        applyModeToForm(id);
        alert('Mode saved.');
      } catch (err) {
        console.error(err);
        alert('Could not save mode. Check /setup/race_modes/save on the backend.');
      }
    };
    els.btnSaveMode?.addEventListener('click', doSave);
    els.customLabel?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); doSave(); }
    });

  
}





  // ------------------------------------------------------------------------
  // Readiness probes (DB, decoder, entrants)
  // ------------------------------------------------------------------------
  async function refreshReadiness() {
    try {
      const r = await fetch('/healthz', { cache: 'no-cache' });
      setChip(els.chipDB, r.ok ? 'ok' : 'warn', r.ok ? 'DB: Ready' : 'DB: Unknown');
    } catch {
      setChip(els.chipDB, 'warn', 'DB: Unknown');
    }

    if (els.decoderBypass?.checked) {
      setChip(els.chipDecoder, 'warn', 'Decoder: Bypass');
    } else {
      try {
        const r = await fetch('/decoders/status', { cache: 'no-cache' });
        if (r.ok) {
          const js = await r.json();
          const n = js?.online ?? 0;
          setChip(els.chipDecoder, n > 0 ? 'ok' : 'err', `Decoder: ${n > 0 ? 'Online' : 'Offline'}`);
        } else {
          setChip(els.chipDecoder, 'err', 'Decoder: Offline');
        }
      } catch {
        setChip(els.chipDecoder, 'err', 'Decoder: Offline');
      }
    }

    try {
      const r = await fetch('/admin/entrants/enabled_count', { cache: 'no-cache' });
      if (r.ok) {
        const js = await r.json();
        const n = js?.count ?? 0;
        if (els.entrantsCount) els.entrantsCount.textContent = String(n);
        setChip(els.chipEntrants, n > 0 ? 'ok' : 'warn', `Entrants: ${n}`);
      } else {
        setChip(els.chipEntrants, 'warn', 'Entrants: Unknown');
      }
    } catch {
      setChip(els.chipEntrants, 'warn', 'Entrants: Unknown');
    }
  }

  function bindReadiness() {
    els.decoderBypass?.addEventListener('change', refreshReadiness);
    setInterval(refreshReadiness, 5000);
    refreshReadiness();
  }

  // ------------------------------------------------------------------------
  // Save / Start
  // ------------------------------------------------------------------------
  async function saveConfig() {
    const cfg = formToConfig();
    try {
      const r = await fetch('/race/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      alert('Race configuration saved.');
    } catch (err) {
      console.error(err);
      alert('Could not save race configuration. Check backend.');
    }
  }

  async function startRace() {
    const cfg = formToConfig();
    const decoderOk = els.decoderBypass?.checked || els.chipDecoder?.classList.contains('ok');
    const entrantsOk = els.chipEntrants?.classList.contains('ok') || els.chipEntrants?.classList.contains('warn');
    if (!decoderOk || !entrantsOk) {
      alert('Not ready: need entrants and (decoder or bypass).');
      return;
    }
    try {
      const r1 = await fetch('/race/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!r1.ok) throw new Error(`Save config failed: HTTP ${r1.status}`);

      let ok = false;
      try { const rStart = await fetch('/race/start', { method: 'POST' }); ok = rStart.ok; } catch {}
      if (!ok) {
        const rFlag = await fetch('/engine/flag', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ flag: 'green' }),
        });
        ok = rFlag.ok;
      }
      if (!ok) throw new Error('Start endpoint not available');

      $$('input, select, button', els.form).forEach(el => el.disabled = true);
      alert('Race started.');
    } catch (err) {
      console.error(err);
      alert('Could not start the race. Check backend.');
    }
  }

  function bindActions() {
    els.btnSaveConfig?.addEventListener('click', saveConfig);
    els.btnStartRace?.addEventListener('click', startRace);

    // small audio tests
    els.btnTestBeep?.addEventListener('click', () => {
      try {
        const actx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = actx.createOscillator();
        const gain = actx.createGain();
        osc.type = 'square'; osc.frequency.value = 880;
        gain.gain.value = 0.05;
        osc.connect(gain).connect(actx.destination);
        osc.start();
        setTimeout(() => { osc.stop(); actx.close(); }, 180);
      } catch {}
    });
    els.btnTestHorn?.addEventListener('click', () => {
      try {
        const actx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = actx.createOscillator();
        const gain = actx.createGain();
        osc.type = 'sawtooth'; osc.frequency.value = 220;
        gain.gain.value = 0.08;
        osc.connect(gain).connect(actx.destination);
        osc.start();
        setTimeout(() => { osc.frequency.value = 180; }, 150);
        setTimeout(() => { osc.stop(); actx.close(); }, 450);
      } catch {}
    });
  }

  // ------------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------------
  async function boot() {
    MODES = await loadModes();
    paintModes(MODES);

    setCustomEnabled(els.modeSelect.value === CUSTOM_ID);
    // Default to Sprint if present; else first mode; else Custom
    let defaultId = 'sprint';
    if (!MODES[defaultId]) defaultId = Object.keys(MODES)[0] || CUSTOM_ID;
    els.modeSelect.value = defaultId;
    applyModeToForm(defaultId);

    bindLiveUpdates();
    bindCustomSave();
    bindReadiness();
    bindActions();
  }

  if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
