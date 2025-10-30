/* ==========================================================================
  Race Setup - controller (v1.2 merged)
   --------------------------------------------------------------------------
   Summary of notable changes relative to v1.1:
   • Preserves the existing form logic & UI behavior (modes, custom save,
     live summary, readiness chips, audio tests).
   • Updates the "Start Race" flow to call POST /race/setup with
       { race_id, entrants, session_config }
     then persists to localStorage and redirects to Race Control.
   • Adds helpers to fetch entrants and to map the form config to the
     backend's expected 'session_config' shape (including the 'bypass' block).
   • Removes the duplicate bindCustomSave definition that previously existed twice.

   Contract (server.py):
     /setup/race_modes           → { modes: {...} }
     /setup/race_modes/save      ← { id, mode }
     /admin/entrants             → [ { id, number, name, tag, enabled, ... } ]
     /admin/entrants/enabled_count → { count }
     /race/setup                 ← { race_id, entrants, session_config }

   LocalStorage (for Race Control bootstrap or offline UI hints):
     rc.session   ← JSON session_config
     rc.race_id   ← number (epoch seconds)
     rc.entrants  ← JSON entrants array
   ========================================================================== */

(function () {
  'use strict';

  const DECODER_BYPASS_KEY = 'ccrs.decoderBypass';

  // Shorthand DOM helpers
  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // Cache handles to inputs (IDs match template markup)
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
    limitValueS: $('#limitValueS'),       // MINUTES in UI, converted to seconds
    limitValueLaps: $('#limitValueLaps'),
    limitSoftEnd: $('#limitSoftEnd'),

    // Rank + sanity
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

    // Misc toggles
    entrantsCount: $('#entrantsCount'),
    decoderBypass: $('#decoderBypass'),
    entrantsBypass: $('#entrantsBypass'),

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

  // State
  let MODES = {};
  let currentModeId = null;
  let config = {};                 // live form snapshot for Summary

  const CUSTOM_ID = '__custom__';
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(n, hi));

  function readStoredDecoderBypass() {
    try {
      const val = localStorage.getItem(DECODER_BYPASS_KEY);
      if (val === null) return null;
      return val === '1';
    } catch (_) {
      return null;
    }
  }

  function persistDecoderBypass(on) {
    try {
      localStorage.setItem(DECODER_BYPASS_KEY, on ? '1' : '0');
    } catch (_) {
      /* ignore storage failures */
    }
  }

  // Status chip helper
  function setChip(el, status, msg) {
    if (!el) return;
    el.classList.remove('ok', 'warn', 'err');
    if (status) el.classList.add(status);
    el.textContent = msg;
  }

  // Pretty duration for the Summary line
  function secondsToHuman(s) {
  if (!Number.isFinite(s) || s < 0) return '-';
    if (s === 0) return 'Unlimited';
    const m = Math.floor(s / 60);
    const ss = s % 60;
    return `${m}m ${ss}s`;
  }

  // Summary text in the right column
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

  // Enable/disable entire "Custom mode" row
  function setCustomEnabled(on) {
    els.customSave?.classList.toggle('is-disabled', !on);
    if (els.customLabel) els.customLabel.disabled = !on;
    if (els.btnSaveMode) els.btnSaveMode.disabled = !on;
  }

  // ------------------------------------------------------------------------
  // Modes: load from backend + paint options
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
        countdown: { start_enabled: true, start_from_s: 10, end_enabled: false, end_from_s: 10, timeout_s: 0 },
        announcements: { lap_indication: 'beep', rank_enabled: false, rank_interval_laps: 0, rank_change_speech: false, best_lap_speech: false },
      },
      practice: {
        label: 'Practice (Free Play)',
        limit: { type: 'time', value_s: 0, soft_end: true },
        rank_method: 'best_lap',
        min_lap_s: 4.1,
        countdown: { start_enabled: false, start_from_s: 10, end_enabled: false, end_from_s: 10, timeout_s: 0 },
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
    // Trailing 'Custom…' option
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
  // Custom Mode save (writes back to race_modes.yaml via backend)
  // ------------------------------------------------------------------------
  function bindCustomSave() {
    const doSave = async () => {
      // Ignore if not in Custom mode (row is disabled)
      if (els.btnSaveMode?.disabled) return;

      const label = (els.customLabel?.value || '').trim();
      if (!label) { alert('Provide a mode name.'); return; }

      const id = slugifyLabel(label);
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
  // Config <-> Form binding
  // ------------------------------------------------------------------------
  function applyModeToForm(modeId) {
    const m = MODES[modeId];
    currentModeId = modeId;

    const isCustom = (modeId === CUSTOM_ID);
    setCustomEnabled(isCustom);
    if (isCustom && els.customLabel) els.customLabel.value = '';

    // If Custom, start from a sensible baseline (Sprint) if available
    const base = isCustom ? (MODES['sprint'] || Object.values(MODES)[0] || {}) : (m || {});

    // Limit
    if (base.limit?.type === 'laps') {
      els.optLaps.checked = true;
      els.rowLapsValue.hidden = false;
      els.rowTimeValue.hidden = true;
      els.rowSoftEnd.hidden  = true;
      if (els.limitValueLaps) els.limitValueLaps.value = base.limit.value_laps ?? 10;
    } else {
      // time (including time=0 for free play)
      els.optTime.checked = true;
      els.rowLapsValue.hidden = true;
      els.rowTimeValue.hidden = false;
      const val = base.limit?.value_s ?? 900;
      if (els.limitValueS) els.limitValueS.value = Math.round(val / 60); // UI in minutes
      // Show soft_end only when time > 0
      const showSoft = (val > 0);
      els.rowSoftEnd.hidden = !showSoft;
      if (els.limitSoftEnd) els.limitSoftEnd.checked = !!base.limit?.soft_end && showSoft;
    }

    // Rank + filter
    if (els.rankMethod) els.rankMethod.value = base.rank_method || 'total_laps';
    if (els.minLapS) els.minLapS.value = base.min_lap_s ?? 4.1;

    // Countdowns (timeout default 0)
    if (els.startEnabled) els.startEnabled.checked = !!base.countdown?.start_enabled;
    if (els.startFromS)   els.startFromS.value     = base.countdown?.start_from_s ?? 10;
    if (els.endEnabled)   els.endEnabled.checked   = !!base.countdown?.end_enabled;
    if (els.endFromS)     els.endFromS.value       = base.countdown?.end_from_s ?? 10;
    if (els.timeoutS)     els.timeoutS.value       = base.countdown?.timeout_s ?? 0;

    // Announcements
    if (els.lapIndication)     els.lapIndication.value      = base.announcements?.lap_indication ?? 'beep';
    if (els.rankEnabled)       els.rankEnabled.checked      = !!base.announcements?.rank_enabled;
    if (els.rankInterval)      els.rankInterval.value       = base.announcements?.rank_interval_laps ?? 4;
    if (els.rankChangeSpeech)  els.rankChangeSpeech.checked = !!base.announcements?.rank_change_speech;
    if (els.bestLapSpeech)     els.bestLapSpeech.checked    = !!base.announcements?.best_lap_speech;

    // Keep a snapshot and repaint summary
    config = formToConfig();
    paintSummary();
    updateLimitUi(els);
  }

  function updateLimitUi(els) {
    const isTime = !!els.optTime?.checked;

    // Mutually exclusive entry fields
    if (els.limitValueS)     els.limitValueS.disabled     = !isTime;
    if (els.limitValueLaps)  els.limitValueLaps.disabled  =  isTime;

    // Grey entire rows accordingly
    els.rowTimeValue?.classList.toggle('is-disabled', !isTime);
    els.rowLapsValue?.classList.toggle('is-disabled',  isTime);

    // Soft end applies only when time > 0
    const minutes = parseInt(els.limitValueS?.value || '0', 10) || 0;
    const t = minutes * 60;
    const softEnabled = isTime && t > 0;
    if (els.limitSoftEnd) els.limitSoftEnd.disabled = !softEnabled;
    els.rowSoftEnd?.classList.toggle('is-disabled', !softEnabled);
  }

  // Build the existing config shape from the form (kept for UI)
  function formToConfig() {
    const isTime = !!els.optTime?.checked;
    const timeMin = clamp(parseInt(els.limitValueS?.value, 10) || 0, 0, 6 * 60);
    const timeVal = timeMin * 60;
    const lapsVal = clamp(parseInt(els.limitValueLaps?.value, 10) || 1, 1, 2000);

    const cfg = {
      event_label: els.eventLabel?.value?.trim() || '',
      session_label: els.sessionLabel?.value?.trim() || '',

      // These two are UI toggles; we'll map them into the backend's 'bypass' block:
      decoder_bypass: !!els.decoderBypass?.checked,
      entrants_bypass: !!els.entrantsBypass?.checked,

      mode_id: currentModeId, // informational for UI

      limit: isTime ? {
        type: 'time',
        value_s: timeVal,
        soft_end: (timeVal > 0) ? !!els.limitSoftEnd?.checked : true /* irrelevant at 0 */
      } : {
        type: 'laps',
        value_laps: lapsVal
      },

      rank_method: els.rankMethod?.value || 'total_laps',
      min_lap_s: parseFloat(els.minLapS?.value) || 0,

      countdown: {
        start_enabled: !!els.startEnabled?.checked,
        start_from_s: Math.max(1, parseInt(els.startFromS?.value, 10) || 10),
        end_enabled: !!els.endEnabled?.checked,
        end_from_s: Math.max(1, parseInt(els.endFromS?.value, 10) || 10),
        timeout_s: Math.max(0, parseInt(els.timeoutS?.value, 10) || 0),
      },

      announcements: {
        lap_indication: els.lapIndication?.value || 'beep',
        rank_enabled: !!els.rankEnabled?.checked,
        rank_interval_laps: Math.max(0, parseInt(els.rankInterval?.value, 10) || 0),
        rank_change_speech: !!els.rankChangeSpeech?.checked,
        best_lap_speech: !!els.bestLapSpeech?.checked,
      },

      relay: MODES[currentModeId]?.relay || null,
    };

    // When time==0, hide soft-end concerns (treated as free play)
    if (cfg.limit.type === 'time' && cfg.limit.value_s === 0) {
      cfg.countdown.start_enabled = false;
      cfg.countdown.end_enabled = false;
      cfg.countdown.timeout_s = 0;
    }

    // Sounds block (optional controls may be absent in the markup)
    const beepLastOn  = !!($('#beepOnLast')?.checked);
    const beepLastVal = parseInt($('#beepOnLastValue')?.value || '0', 10) || 0;
    const whiteModeSel = $('#whiteFlagMode')?.value || 'auto';
    const whiteAtVal   = parseInt($('#whiteFlagAt')?.value || '60', 10) || 60;

    cfg.sounds = {
      countdown_beep_last_s: beepLastOn ? beepLastVal : 0,
      starting_horn: !!($('#startingHorn')?.checked),
      white_flag: (whiteModeSel === 'time') ? { mode: 'time', at_s: whiteAtVal } :
                 (whiteModeSel === 'off')  ? { mode: 'off' } :
                                             { mode: 'auto' },
      checkered_horn: !!($('#checkeredHorn')?.checked)
    };

    return cfg;
  }

  // Convert the UI cfg into the backend's expected 'session_config' shape
  function toSessionConfig(cfg) {
    return {
      event_label:   cfg.event_label,
      session_label: cfg.session_label,
      mode_id:       cfg.mode_id || 'sprint',
      limit:         cfg.limit,
      rank_method:   cfg.rank_method,
      min_lap_s:     cfg.min_lap_s,
      countdown:     cfg.countdown,
      announcements: cfg.announcements,
      sounds:        cfg.sounds,
      bypass: {
        decoder:  !!cfg.decoder_bypass,
        entrants: !!cfg.entrants_bypass,
      },
      ...(cfg.relay ? { relay: cfg.relay } : {}),
    };
  }

  // Summary chip
  function paintSummary() {
    if (!els.summaryText) return;
    els.summaryText.textContent = summarize(config);
  }

  // Live form updates → recompute config + summary
  function bindLiveUpdates() {
    els.form?.addEventListener('input', () => {
      // Toggle visibility of time/laps rows
      const isTime = !!els.optTime?.checked;
      if (els.rowTimeValue) els.rowTimeValue.hidden = !isTime;
      if (els.rowLapsValue) els.rowLapsValue.hidden = isTime;

      // Soft end shows only when time > 0
      if (isTime) {
        const timeVal = (parseInt(els.limitValueS?.value || '0', 10) || 0) * 60;
        if (els.rowSoftEnd) els.rowSoftEnd.hidden = !(timeVal > 0);
      } else if (els.rowSoftEnd) {
        els.rowSoftEnd.hidden = true;
      }

      updateLimitUi(els);
      config = formToConfig();
      paintSummary();
    });

    els.modeSelect?.addEventListener('change', (e) => {
      applyModeToForm(e.target.value);
    });
  }

  // ------------------------------------------------------------------------
  // Audio helpers
  // ------------------------------------------------------------------------
  async function playSound(name, fallback='beep') {
    const cacheBust = Date.now();
    const urls = [
      `/config/sounds/${name}?v=${cacheBust}`,
      `/assets/sounds/${name}?v=${cacheBust}`
    ];
    for (const u of urls) {
      try {
        const a = new Audio(u);
        await a.play();
        return true;
      } catch (e) { /* try next */ }
    }
    // Tiny synth fallback
    try {
      const actx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = actx.createOscillator();
      const gain = actx.createGain();
      if (fallback === 'horn') {
        osc.type = 'sawtooth'; osc.frequency.value = 220; gain.gain.value = 0.08;
        osc.connect(gain).connect(actx.destination); osc.start();
        setTimeout(() => { osc.frequency.value = 180; }, 150);
        setTimeout(() => { osc.stop(); actx.close(); }, 450);
      } else {
        osc.type = 'square'; osc.frequency.value = 880; gain.gain.value = 0.05;
        osc.connect(gain).connect(actx.destination); osc.start();
        setTimeout(() => { osc.stop(); actx.close(); }, 100);
      }
    } catch {}
    return false;
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
    if (els.decoderBypass) {
      els.decoderBypass.addEventListener('change', () => {
        persistDecoderBypass(!!els.decoderBypass.checked);
        refreshReadiness();
      });
    }
    setInterval(refreshReadiness, 5000);
    refreshReadiness();
  }

  // ------------------------------------------------------------------------
  // Save / Start
  // ------------------------------------------------------------------------
  async function saveConfig() {
    // Legacy endpoint retained for backward compatibility; no-op for new flow.
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

  // Returns ENABLED entrants mapped to the minimal shape Race Setup needs.
  async function fetchEnabledEntrants() {
    const res = await fetch('/admin/entrants', { cache: 'no-store' });
    if (!res.ok) throw new Error(`Failed to fetch entrants (${res.status})`);
    const all = await res.json();

    return all
      .filter(e => !!e.enabled)  // only enabled entrants
      .map(e => ({
        id: e.id,                                // REQUIRED, non-null
        name: e.name,
        number: e.number,                        // leave as-is; server stringifies
        tag: (e.tag != null ? String(e.tag).trim() : null),
        enabled: true,                           // we filtered already
        status: ((e.status || 'ACTIVE') + '').toUpperCase(),
      }));
  }

  // NEW: authoritative start that posts the combined payload to /race/setup
  async function startRace() {
    const cfg = formToConfig();

    // Guardrails (matches previous UX)
    const decoderOk  = els.decoderBypass?.checked || els.chipDecoder?.classList.contains('ok');
    const entrantsOk = els.chipEntrants?.classList.contains('ok') || els.chipEntrants?.classList.contains('warn');
    if (!decoderOk || !entrantsOk) {
      alert('Not ready: need entrants and (decoder or bypass).');
      return;
    }

    try {
      // Build pieces
      const entrants = await fetchEnabledEntrants();                 // authoritative roster
      const session_config = toSessionConfig(cfg);            // normalized schema
      const race_id = Math.floor(Date.now() / 1000);          // stable-enough id

      // Cache for Race Control bootstrap
      localStorage.setItem('rc.session', JSON.stringify(session_config));
      localStorage.setItem('rc.race_id', String(race_id));
      localStorage.setItem('rc.entrants', JSON.stringify(entrants));

      // Post to the new unified endpoint
      const payload = { race_id, entrants, session_config };
      const r = await fetch('/race/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        throw new Error(`Setup failed (${r.status}): ${txt}`);
      }

      // Lock form and go
      $$('input, select, button', els.form).forEach(el => el.disabled = true);
      window.location.assign(`/ui/operator/race_control.html?session=${encodeURIComponent(race_id)}`);
    } catch (err) {
      console.error(err);
      alert('Could not start the race. Check backend and console.');
    }
  }

  function bindActions() {
    els.btnSaveConfig?.addEventListener('click', saveConfig);
    els.btnStartRace?.addEventListener('click', (e) => {
      e.preventDefault();
      startRace();
    });

    // Audio test buttons
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
    els.btnTestHorn?.addEventListener('click', () => { playSound('start_horn.wav', 'horn'); });
  }

  // ------------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------------
  async function boot() {
    MODES = await loadModes();
    paintModes(MODES);

    if (els.decoderBypass) {
      const stored = readStoredDecoderBypass();
      if (stored !== null) {
        els.decoderBypass.checked = stored;
      }
      persistDecoderBypass(!!els.decoderBypass.checked);
    }

    setCustomEnabled(els.modeSelect?.value === CUSTOM_ID);

    // Default to Sprint if present; else first mode; else Custom
    let defaultId = 'sprint';
    if (!MODES[defaultId]) defaultId = Object.keys(MODES)[0] || CUSTOM_ID;
    if (els.modeSelect) els.modeSelect.value = defaultId;
    applyModeToForm(defaultId);

    bindLiveUpdates();
    bindCustomSave();
    bindReadiness();
    bindActions();

  // Wire dynamic row enable/disable helpers
    wireRowToggles();
  }

  if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);

  // ------------------------------------------------------------
  // Utility: dim/disable a row in-place, without changing HTML
  // ------------------------------------------------------------
  function setRowEnabled(rowSel, cbSel, on) {
    const row = document.querySelector(rowSel);
    const cb  = cbSel ? document.querySelector(cbSel) : null;
    if (!row) return;

    // Visual dimming
    row.classList.toggle('is-disabled', !on);

    // Functional disabling of all form controls in the row except the toggle
    row.querySelectorAll('input, select, textarea, button').forEach(el => {
      if (cb && el === cb) return;           // keep the controlling checkbox clickable
      el.disabled = !on;
    });
  }

  // Bind a checkbox to its row: checkbox checked = row enabled
  function bindToggleRow(cbSel, rowSel) {
    const cb = document.querySelector(cbSel);
    if (!cb) return;
    const sync = () => setRowEnabled(rowSel, cbSel, cb.checked);
    cb.addEventListener('change', sync);
    // Initialize on load
    sync();
  }

  // Wire up existing rows (only if they exist in markup)
  function wireRowToggles() {
    // Countdowns
    bindToggleRow('#startEnabled',   '#rowStartCountdown');
    bindToggleRow('#endEnabled',     '#rowEndCountdown');

    // Timeout: prefer a checkbox if present; otherwise dim when value == 0
    (function () {
      const cb   = document.querySelector('#timeoutEnabled');
      const row  = '#rowTimeout';
      const pad  = document.querySelector('#timeoutS');

      if (cb) {
        bindToggleRow('#timeoutEnabled', row);
      } else if (pad) {
        const sync = () => {
          const on = (parseInt(pad.value || '0', 10) || 0) > 0;
          setRowEnabled(row, null, on);
        };
        pad.addEventListener('input', sync);
        sync();
      }
    })();

    // Rank announcements
    bindToggleRow('#rankEnabled', '#rowRankAnnouncements');

    // Timing sounds (right pane)
    bindToggleRow('#beepSecondsEnabled', '#rowBeepSeconds');
    bindToggleRow('#whiteFlagSound',     '#rowWhiteFlag');
  }
})();