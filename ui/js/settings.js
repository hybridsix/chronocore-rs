/* =======================================================================
   CCRS Settings — Page Logic (Verbose-Commented)
   -----------------------------------------------------------------------
   Responsibilities
   1) Left-nav behavior
      - Smoothly scroll the INTERNAL right-pane scroller when a nav anchor
        is clicked (e.g., #ingest), not the window.
      - Maintain an "active" highlight using a scrollspy (IntersectionObserver)
        based on which <section> is most visible in the right pane.

   2) Runtime hydration
      - GET /setup/runtime to obtain the merged runtime config. This is a
        read-only snapshot used to prefill controls. The exact shape is kept
        intentionally small and stable.

   3) Patch building + Apply/Save stubs
      - Collect values from inputs to form a MINIMAL patch object that mirrors
        the runtime structure (engine.*, race.*, ui.*). The patch is sent to
        either /setup/apply_hot (no restart) or /setup/save_restart (persist +
        recycle engine). Server-side will validate and reject unknown keys.

   Notes
   - This file avoids external dependencies. Keep it friendly for maintainers.
   - All selectors are defensive (elements may be null during early wiring).
   ======================================================================= */

(function () {
  'use strict';

  // ---------------------------------------------------------------------
  // Namespace + tiny query helper
  // ---------------------------------------------------------------------
  const CCRS = window.CCRS || {};
  const $ = (sel, root) => (root || document).querySelector(sel);

  // ---------------------------------------------------------------------
  // Element registry — if a control doesn't exist on the page yet, the
  // corresponding entry is simply null. Code below guards these accesses.
  // ---------------------------------------------------------------------
  const ids = [
    // Header / engine host bits
    'engineLabel', 'policySummary', 'effectiveHost', 'engineOverride',
    'testEngine', 'saveEngine', 'clearEngine', 'engineMsg',

    // Client role
    'role', 'saveRole',

    // Ingest + Diagnostics
    'debounce', 'minLapMs', 'debounceOverrides',
    'diagEnabled', 'diagBuffer', 'diagTransport', 'beepMax', 'testBeep',

    // Track lists
    'locList', 'bindList',

    // Flags
    'flagBlock', 'flagGrace',

    // Missed-lap
    'missedMode', 'missedWindow', 'missedK', 'missedMinGap', 'missedMaxSeq', 'missedMark',

    // UI
    'soundDefault', 'timeDisplay',

    // Apply area
    'configDiff', 'applyHot', 'applyRestart', 'revert', 'applyMsg'
  ];

  const el = ids.reduce((acc, id) => {
    acc[id] = document.getElementById(id) || null;
    return acc;
  }, {});

  // ---------------------------------------------------------------------
  // Boot sequence
  // ---------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', init);

  async function init() {
    // 1) Header pill — pull from CCRS helper if available
    if (typeof CCRS.effectiveEngineLabel === 'function' && el.engineLabel) {
      el.engineLabel.textContent = 'Engine: ' + CCRS.effectiveEngineLabel();
    }

    // 2) Left-nav scrolling + scrollspy inside the right pane
    enableLeftNav();

    // 3) Hydrate fields from runtime; failures are logged, not fatal
    try {
      const rt = await fetchRuntime();
      hydrate(rt);
    } catch (err) {
      console.warn('[settings] runtime load failed:', err);
    }

    // 4) Wire actions (safe even if elements are missing)
    el.applyHot     && el.applyHot.addEventListener('click', onApplyHot);
    el.applyRestart && el.applyRestart.addEventListener('click', onSaveRestart);
    el.revert       && el.revert.addEventListener('click', onRevert);

    el.saveRole && el.role && el.saveRole.addEventListener('click', () => {
      // Stored locally on this device only (policy may allow/deny override)
      try {
        localStorage.setItem('ccrs.role', el.role.value);
        toast('Saved role: ' + el.role.value);
      } catch {}
    });
  }

  // ---------------------------------------------------------------------
  // Left-nav behavior: internal smooth scroll + scrollspy
  // ---------------------------------------------------------------------
  function enableLeftNav() {
    const scroller = $('.pane__scroll');
    if (!scroller) return;

    // Anchor links that target in-page ids (e.g., #ingest)
    const links = Array.from(document.querySelectorAll('.sideNav .navLink[href^="#"]'));
    const sections = links
      .map(a => document.querySelector(a.getAttribute('href')))
      .filter(Boolean);

    // Smooth scroll inside the INTERNAL pane (not window)
    links.forEach(a => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
        const target = document.querySelector(a.getAttribute('href'));
        if (!target) return;

        // Set active immediately for instant feedback
        links.forEach(l => l.classList.remove('active'));
        a.classList.add('active');

        // Offset calculation: position target relative to the scroller
        const top = target.getBoundingClientRect().top
                  - scroller.getBoundingClientRect().top
                  + scroller.scrollTop
                  - 8; // tiny breathing room for sticky h2
        scroller.scrollTo({ top, behavior: 'smooth' });
      });
    });

    // Scrollspy: highlight the link whose section is most visible
    if (sections.length) {
      const linkById = Object.fromEntries(links.map(l => [l.getAttribute('href'), l]));
      const io = new IntersectionObserver((entries) => {
        let best = null, max = 0;
        for (const e of entries) {
          if (e.intersectionRatio >= max) { max = e.intersectionRatio; best = e.target; }
        }
        if (!best) return;
        links.forEach(l => l.classList.remove('active'));
        linkById['#' + best.id]?.classList.add('active');
      }, {
        root: scroller,
        threshold: [0.35, 0.55, 0.75] // tune for desired sensitivity
      });
      sections.forEach(sec => io.observe(sec));
    }
  }

  // ---------------------------------------------------------------------
  // Runtime I/O — load snapshot and hydrate fields
  // ---------------------------------------------------------------------
  async function fetchRuntime() {
    const res = await fetch('/setup/runtime', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  }

  function hydrate(rt) {
    // Ingest
    if (el.debounce) el.debounce.value = val(rt, 'engine.ingest.debounce_ms', '');
    if (el.minLapMs) el.minLapMs.value = val(rt, 'race.missed_lap.min_gap_ms', '');

    // Per-location debounce overrides (render-only for now)
    renderDebounceOverrides(val(rt, 'engine.ingest.per_location_debounce_ms', {}) || {});

    // Diagnostics
    el.diagEnabled   && (el.diagEnabled.value   = String(val(rt, 'engine.diagnostics.enabled', true)));
    el.diagBuffer    && (el.diagBuffer.value    = val(rt, 'engine.diagnostics.buffer_size', 500));
    el.diagTransport && (el.diagTransport.value = val(rt, 'engine.diagnostics.stream.transport', 'sse'));
    el.beepMax       && (el.beepMax.value       = val(rt, 'engine.diagnostics.beep.max_per_sec', 5));

    // Flags
    el.flagBlock && (el.flagBlock.value = (val(rt, 'race.flags.inference_blocklist', []) || []).join(','));
    el.flagGrace && (el.flagGrace.value = val(rt, 'race.flags.post_green_grace_ms', 3000));

    // Missed-lap
    const enabled = !!val(rt, 'race.missed_lap.enabled', false);
    const mode    = enabled ? val(rt, 'race.missed_lap.apply_mode', 'propose') : 'off';
    el.missedMode    && (el.missedMode.value    = mode);
    el.missedWindow  && (el.missedWindow.value  = val(rt, 'race.missed_lap.window_laps', 5));
    el.missedK       && (el.missedK.value       = val(rt, 'race.missed_lap.sigma_k', 2.0));
    el.missedMinGap  && (el.missedMinGap.value  = val(rt, 'race.missed_lap.min_gap_ms', 8000));
    el.missedMaxSeq  && (el.missedMaxSeq.value  = val(rt, 'race.missed_lap.max_consecutive_inferred', 1));
    el.missedMark    && (el.missedMark.value    = String(val(rt, 'race.missed_lap.mark_inferred', true)));

    // Track (read-only lists for now)
    renderLocations(val(rt, 'track.locations', {}));
    renderBindings(val(rt, 'track.bindings', []));

    // Engine host informational fields
    el.effectiveHost && (el.effectiveHost.value = val(rt, 'meta.engine_host', '—'));
    el.policySummary && (el.policySummary.value = 'Runtime: merged config');
  }

  // Render helpers for read-only lists ------------------------------------------------
  function renderDebounceOverrides(map) {
    const box = el.debounceOverrides; if (!box) return;
    box.innerHTML = '';
    const entries = Object.entries(map);
    if (!entries.length) {
      box.innerHTML = '<div class="small muted">No per-location overrides.</div>';
      return;
    }
    entries.forEach(([loc, ms]) => {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = `
        <div class="key">${loc}</div>
        <input type="number" class="input" value="${ms}" data-loc="${loc}" min="0" step="10">
        <button class="btn btn--subtle" data-remove="${loc}">Remove</button>`;
      box.appendChild(row);
    });
  }

  function renderLocations(locMap) {
    const box = el.locList; if (!box) return;
    box.innerHTML = '';
    const ids = Object.keys(locMap || {});
    if (!ids.length) { box.innerHTML = '<div class="small muted">No locations defined.</div>'; return; }
    ids.sort().forEach(id => {
      const row = document.createElement('div'); row.className = 'row';
      row.innerHTML = `<div class="key">${id}</div><div>${locMap[id]}</div>`;
      box.appendChild(row);
    });
  }

  function renderBindings(bindings) {
    const box = el.bindList; if (!box) return;
    box.innerHTML = '';
    if (!Array.isArray(bindings) || !bindings.length) {
      box.innerHTML = '<div class="small muted">No bindings defined.</div>';
      return;
    }
    bindings.forEach(b => {
      const row = document.createElement('div'); row.className = 'row';
      row.innerHTML = `
        <div class="key">${b.computer_id || '—'}</div>
        <div>${b.decoder_id || '—'}</div>
        <div>${b.port || '—'}</div>
        <div>→ <b>${b.location_id || 'UNKNOWN'}</b></div>`;
      box.appendChild(row);
    });
  }

  // ---------------------------------------------------------------------
  // Patch collection and apply/save actions
  // ---------------------------------------------------------------------
  function collectPatch() {
    const patch = { engine: {}, race: {} };

    // Ingest globals
    if (el.debounce && el.debounce.value !== '') {
      patch.engine.ingest = Object.assign({}, patch.engine.ingest, {
        debounce_ms: Number(el.debounce.value)
      });
    }

    // Diagnostics
    patch.engine.diagnostics = {
      enabled: (el.diagEnabled && el.diagEnabled.value === 'true') || false,
      buffer_size: numOrUndef(el.diagBuffer?.value),
      stream: { transport: (el.diagTransport && el.diagTransport.value) || 'sse' },
      beep:   { max_per_sec: numOrUndef(el.beepMax?.value, 5) }
    };

    // Flags
    patch.race.flags = {
      inference_blocklist: (el.flagBlock?.value || '')
        .split(',').map(s => s.trim()).filter(Boolean),
      post_green_grace_ms: numOrUndef(el.flagGrace?.value, 3000)
    };

    // Missed-lap
    const mode = (el.missedMode && el.missedMode.value) || 'off';
    patch.race.missed_lap = {
      enabled: mode !== 'off',
      apply_mode: mode === 'off' ? 'propose' : mode,
      window_laps: numOrUndef(el.missedWindow?.value, 5),
      sigma_k: numOrUndef(el.missedK?.value, 2.0),
      min_gap_ms: numOrUndef(el.missedMinGap?.value, 8000),
      max_consecutive_inferred: numOrUndef(el.missedMaxSeq?.value, 1),
      mark_inferred: (el.missedMark && el.missedMark.value === 'true') || true
    };

    // UI (optional; safe if controls exist)
    if (el.soundDefault || el.timeDisplay) {
      patch.ui = patch.ui || {};
      patch.ui.operator = {
        sound_default_enabled: el.soundDefault ? (el.soundDefault.value === 'true') : undefined,
        time_display: el.timeDisplay ? (el.timeDisplay.value || 'local') : undefined
      };
    }

    return pruneEmpty(patch);
  }

  function numOrUndef(v, dflt) {
    const n = Number(v);
    return Number.isFinite(n) ? n : (dflt === undefined ? undefined : dflt);
  }

  function pruneEmpty(obj) {
    if (!obj || typeof obj !== 'object') return obj;
    Object.keys(obj).forEach(k => {
      const v = obj[k];
      if (v && typeof v === 'object' && !Array.isArray(v)) pruneEmpty(v);
      const emptyObj = v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0;
      if (v === undefined || v === null || v === '' || emptyObj) delete obj[k];
    });
    return obj;
  }

  // Apply (hot) — no restart
  async function onApplyHot() {
    try {
      const patch = collectPatch();
      const res = await fetch('/setup/apply_hot', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(patch)
      });
      const data = await res.json();
      toast(data.ok ? 'Applied hot.' : 'Apply failed.');
      el.configDiff && (el.configDiff.textContent = JSON.stringify(patch, null, 2));
    } catch (err) {
      toast('Apply failed.');
      console.warn('[settings] apply hot failed:', err);
    }
  }

  // Save & restart — persist to config and recycle engine
  async function onSaveRestart() {
    try {
      const patch = collectPatch();
      const res = await fetch('/setup/save_restart', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(patch)
      });
      const data = await res.json();
      toast(data.ok ? 'Saved. Engine restarting…' : 'Save failed.');
      el.configDiff && (el.configDiff.textContent = JSON.stringify(patch, null, 2));
    } catch (err) {
      toast('Save failed.');
      console.warn('[settings] save/restart failed:', err);
    }
  }

  // Revert — rehydrate from the live runtime snapshot
  function onRevert() {
    fetchRuntime()
      .then(rt => { hydrate(rt); toast('Reverted to runtime.'); })
      .catch(() => toast('Revert failed.'));
  }

  // Small helper to surface status near the Apply buttons
  function toast(msg) {
    el.applyMsg && (el.applyMsg.textContent = msg);
  }

  // Path helper (safe get)
  function val(obj, path, dflt) {
    try { return path.split('.').reduce((a, k) => (a && a[k] != null ? a[k] : undefined), obj) ?? dflt; }
    catch { return dflt; }
  }
})();
