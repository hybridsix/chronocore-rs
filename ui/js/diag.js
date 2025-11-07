/* =======================================================================
  CCRS Diagnostics / Live Sensors - Frontend Logic
   -----------------------------------------------------------------------
   Responsibilities
   1) Connect to the backend SSE endpoint (/diagnostics/stream)
      - Reconnect automatically if disconnected.
      - Parse each event and append to the table.
   2) Respect runtime settings
      - debounce_ms, beep.max_per_sec, diagnostics.enabled
      - All loaded from /setup/runtime.
   3) Manage UI behaviors
      - Pause/resume stream display
      - Clear log
      - Optional filters (known-only, show RSSI)
      - Audible beep on each detection (AudioContext)
   4) Maintain bounded buffer (max 500 rows)
   ======================================================================= */

(function () {
  'use strict';

  const CCRS = window.CCRS || {};
  const $ = (sel, root) => (root || document).querySelector(sel);

  // ---------------------------------------------------------------------
  // DOM references - all optional-safe
  // ---------------------------------------------------------------------
  const bodyEl       = $('#liveBody');
  const btnPause     = $('#btnPause');
  const btnClear     = $('#btnClear');
  const chkBeep      = $('#chkBeep');
  const chkKnownOnly = $('#chkKnownOnly');
  const chkShowRssi  = $('#chkShowRssi');
  const diagWarn     = $('#diagWarn');

  // ---------------------------------------------------------------------
  // Runtime + state
  // ---------------------------------------------------------------------
  let debounceMs    = 250;   // guard between beeps
  let beepMaxPerSec = 5;     // global beep throttle
  let diagnosticsOn = true;  // if false, show warning banner

  let paused          = false; // UI pause flag
  let lastBeepAt      = 0;     // timestamp of last beep
  let beepsThisSecond = 0;     // rolling counter for rate limit
  let currentSecond   = 0;     // track 1s windows

  let es = null; // EventSource instance
  let pageLoadTime = null; // Track when page loads to filter old data

  // ---------------------------------------------------------------------
  // Boot sequence
  // ---------------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', init);
  
  // Handle browser back/forward cache (bfcache) - clear when page is restored
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) {
      // Page was restored from bfcache, clear the table
      console.log('[diag] Page restored from cache, clearing table');
      const tbody = document.getElementById('liveBody');
      if (tbody) {
        tbody.innerHTML = '';
        tbody.textContent = '';
        while (tbody.firstChild) {
          tbody.removeChild(tbody.firstChild);
        }
      }
    }
  });

  async function init() {
    // Record page load time to filter out old data
    pageLoadTime = Date.now();
    console.log('[diag] Page load time:', new Date(pageLoadTime).toISOString());
    
    // Clear any leftover data from previous session - do this first before anything else
    const tbody = document.getElementById('liveBody');
    if (tbody) {
      // Use multiple methods to ensure clearing
      tbody.innerHTML = '';
      tbody.textContent = '';
      while (tbody.firstChild) {
        tbody.removeChild(tbody.firstChild);
      }
      console.log('[diag] Table cleared on init, row count:', tbody.rows?.length || 0);
    }
    
    // Populate header pill if helper available
    if (typeof CCRS.effectiveEngineLabel === 'function' && engineLabel) {
      engineLabel.textContent = 'Engine: ' + CCRS.effectiveEngineLabel();
    }

    // Load runtime settings to configure debounce + beep caps
    try {
      const rt = await loadRuntime();
      applyRuntime(rt);
    } catch (err) {
      console.warn('[diag] failed to load runtime', err);
    }

    // Wire UI controls
    btnPause?.addEventListener('click', togglePause);
    btnClear?.addEventListener('click', clearRows);
    if (chkBeep) chkBeep.checked = true;

    // RSSI default: ON at startup, and keep in sync on toggle
    if (chkShowRssi) {
      chkShowRssi.checked = true;            // default visible
      toggleRSSI(chkShowRssi.checked);       // apply initial visibility
      chkShowRssi.addEventListener('change', () => toggleRSSI(chkShowRssi.checked));
    }

    // Delay stream start slightly to ensure clear completes
    setTimeout(() => {
      // Start stream if diagnostics are enabled
      if (diagnosticsOn) {
        openStream();
      } else {
        diagWarn && (diagWarn.hidden = false);
      }
    }, 100);

    // Clear when leaving; quiet warm-up when returning
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        clearRows();
      } else {
        beginWarmup();
      }
    });
    window.addEventListener('pagehide', handlePageExit);
    window.addEventListener('beforeunload', handlePageExit);
  }

  // ---------------------------------------------------------------------
  // --- Page exit handler - clear data and close connection -------------
  // ---------------------------------------------------------------------
  function handlePageExit() {
    clearRows();
    if (es) {
      try {
        es.close();
        es = null;
      } catch (err) {
        console.warn('[diag] failed to close EventSource', err);
      }
    }
  }

  // ---------------------------------------------------------------------
  // --- Diagnostics UI state (warm-up + clear) --------------------------
  // ---------------------------------------------------------------------
  let warmup = true;            // suppress flash/beep for initial backlog
  let warmupTimer = null;
  const WARMUP_MS = 400;        // long enough to drain ring buffer quietly

  function beginWarmup() {
    clearTimeout(warmupTimer);
    warmup = true;
    warmupTimer = setTimeout(() => { warmup = false; }, WARMUP_MS);
  }

  function clearTable() {
    const tbody = document.getElementById('liveBody');
    if (tbody) tbody.textContent = '';
  }

  // ---------------------------------------------------------------------
  // Runtime loader - fetch /setup/runtime (same structure as Settings)
  // ---------------------------------------------------------------------
  async function loadRuntime() {
    const res = await fetch('/setup/runtime', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  }

  function applyRuntime(rt) {
    debounceMs    = get(rt, 'engine.ingest.debounce_ms', 250);
    beepMaxPerSec = get(rt, 'engine.diagnostics.beep.max_per_sec', 5);
    diagnosticsOn = !!get(rt, 'engine.diagnostics.enabled', true);
  }

  // ---------------------------------------------------------------------
  // SSE Connection - opens EventSource to /diagnostics/stream
  // ---------------------------------------------------------------------
  function openStream() {
    beginWarmup();                         // <- start silent window
    if (es) try { es.close(); } catch {}
    es = new EventSource('/diagnostics/stream');

    es.addEventListener('open', () => console.log('[diag] SSE open'));
    es.addEventListener('message', (e) => {
      try {
        const evt = JSON.parse(e.data);
        handleDetection(evt);
      } catch (err) {
        console.warn('[diag] parse error', err);
      }
    });
    es.addEventListener('error', (e) => console.warn('[diag] SSE error', e));
  }

  // ---------------------------------------------------------------------
  // Handle a single detection event
  // ---------------------------------------------------------------------
  function handleDetection(evt) {
    if (paused) return;

    // Filter out detections from before page load (ignore buffered historical data)
    if (evt.time && pageLoadTime) {
      try {
        const detectionTime = new Date(evt.time).getTime();
        if (detectionTime < pageLoadTime) {
          return; // Silently drop old detections
        }
      } catch (err) {
        // If we can't parse the time, allow it through
      }
    }

    // Filter out unknown entrants if checkbox is active
    const known = !!(evt.entrant && (evt.entrant.name || evt.entrant.number || evt.entrant.num));
    if (chkKnownOnly?.checked && !known) return;

    // Add row (no flash during warm-up), and beep only after warm-up
    insertRow(evt, { flash: !warmup });
    if (!warmup && chkBeep?.checked) maybeBeep();

    // Keep table bounded
    trimRows(500);
  }

  // ---------------------------------------------------------------------
  // Table manipulation helpers
  // ---------------------------------------------------------------------
  function insertRow(evt, opts = {}) {
    if (!bodyEl) return;
    const { flash = true } = opts;

    const tr = document.createElement('tr');
    if (flash) tr.className = 'newRow';

    // Build table row content safely
    const ts   = formatTime(evt.time);
  const src  = evt.source || '-';
  const tag  = evt.tag_id || '-';
  const num  = (evt.entrant && (evt.entrant.num || evt.entrant.number)) || '-';
  const name = (evt.entrant && evt.entrant.name) || '-';
  const rssi = evt.rssi != null ? String(evt.rssi) : '-';

    tr.innerHTML = `
      <td class="col-time">${ts}</td>
      <td class="col-source">${esc(src)}</td>
      <td class="col-tag">${esc(tag)}</td>
      <td class="col-num">${esc(num)}</td>
      <td class="col-entrant">${esc(name)}</td>
      <td class="col-rssi">${renderRssiCell(rssi)}</td>
    `;

    // Insert at top (newest first)
    if (bodyEl.firstChild) bodyEl.insertBefore(tr, bodyEl.firstChild);
    else bodyEl.appendChild(tr);

    // Highlight only when flashing is enabled
    if (flash) {
      setTimeout(() => tr.classList.remove('newRow'), 520);
    }
  }

  function trimRows(max) {
    if (!bodyEl) return;
    while (bodyEl.rows.length > max) bodyEl.deleteRow(bodyEl.rows.length - 1);
  }

  // ---------------------------------------------------------------------
  // RSSI rendering helpers
  // ---------------------------------------------------------------------

  // Map various RSSI formats to a 0..1 strength (IR systems sometimes use 0..255 raw).
  function rssiToStrength(rssi) {
    if (rssi == null || isNaN(rssi)) return 0;
    const v = Number(rssi);

    // If it's a plausible raw byte (0..255), map directly.
    if (v >= 0 && v <= 255) {
      return Math.min(1, Math.max(0, v / 255));
    }

    // Otherwise treat as "dBm-like" negative (e.g., -100 .. -40).
    // Clamp a typical practical IR range.
    const MIN = -110, MAX = -40;
    const clamped = Math.min(MAX, Math.max(MIN, v));
    return (clamped - MIN) / (MAX - MIN);
  }

  // Hue from red (0deg) -> yellow (60deg) -> green (120deg)
  function strengthToColor(str) {
    const hue = 0 + 120 * Math.max(0, Math.min(1, str)); // 0..120
    // Slightly brighter at higher strength
    const light = 45 + 10 * str; // 45%..55%
    return `hsl(${hue.toFixed(0)} 90% ${light.toFixed(0)}%)`;
  }

  // Build the RSSI cell innerHTML (number + smooth bar)
  function renderRssiCell(rssi) {
  const val = (rssi ?? rssi === 0) ? String(rssi) : '-';
    const s = rssiToStrength(rssi);
    const pct = (s * 100).toFixed(0) + '%';
    const color = strengthToColor(s);
    return `
      <div class="rssiWrap">
        <span class="rssiVal">${val}</span>
        <span class="rssiBar"><span class="rssiFill" style="width:${pct}; background-color:${color};"></span></span>
      </div>
    `;
  }

  // ---------------------------------------------------------------------
  // UI controls
  // ---------------------------------------------------------------------
  function togglePause() {
    paused = !paused;
    if (btnPause) {
      btnPause.textContent = paused ? 'Resume' : 'Pause';
      btnPause.setAttribute('aria-pressed', String(paused));
    }
  }

  function clearRows() {
    bodyEl && (bodyEl.innerHTML = '');
  }

  function toggleRSSI(show) {
    const idx = 5; // RSSI column index
    const table = bodyEl?.closest('table');
    if (!table) return;
    const th = table.tHead?.rows[0]?.cells[idx];
    if (th) th.style.display = show ? '' : 'none';
    const rows = table.tBodies[0]?.rows || [];
    for (const r of rows) if (r.cells[idx]) r.cells[idx].style.display = show ? '' : 'none';
  }

  // ---------------------------------------------------------------------
  // Beep logic - simple square wave tone
  // ---------------------------------------------------------------------
  function maybeBeep() {
    const now = Date.now();
    if (now - lastBeepAt < debounceMs) return;
    const sec = Math.floor(now / 1000);
    if (sec !== currentSecond) { currentSecond = sec; beepsThisSecond = 0; }
    if (beepsThisSecond >= beepMaxPerSec) return;
    lastBeepAt = now; beepsThisSecond++;
    toneBeep(880, 60);
  }

  function toneBeep(freq = 880, durMs = 60) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'square'; osc.frequency.value = freq; gain.gain.value = 0.05;
    osc.connect(gain).connect(ctx.destination); osc.start();
    setTimeout(() => { osc.stop(); ctx.close(); }, durMs);
  }

  // ---------------------------------------------------------------------
  // Small helpers
  // ---------------------------------------------------------------------
  function formatTime(iso) {
    try {
      const d = iso ? new Date(iso) : new Date();
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      const ms = String(d.getMilliseconds()).padStart(3, '0');
      return `${hh}:${mm}:${ss}.${ms}`;
  } catch { return iso || '-'; }
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function get(o, p, d) {
    try { return p.split('.').reduce((a, k) => a?.[k], o) ?? d; } catch { return d; }
  }
})();


