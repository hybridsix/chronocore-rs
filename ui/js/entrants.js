/* ==========================================================================
   CCRS — Entrants & Tags page logic
   - Forward-only helpers via window.CCRS
   - CCRS.setNetStatus(ok:boolean, message:string)
   - DB pill uses /readyz (immediate + poller)
   - Entrants CRUD via /admin/entrants (bulk contract, single item)
   ========================================================================== */
(function () {
  'use strict';

  /* --- SAFETY SHIM: guarantee a global fillForm(row) that hydrates ALL fields --- */
(function () {
  if (typeof window.fillForm === 'function') return;  // already defined elsewhere

  function normHex(v) {
    if (v == null) return null;
    const s = String(v).trim().replace(/^#/, '');
    if (/^[0-9a-f]{3}$/i.test(s)) return ('#' + s[0]+s[0]+s[1]+s[1]+s[2]+s[2]).toUpperCase();
    if (/^[0-9a-f]{6}$/i.test(s)) return ('#' + s).toUpperCase();
    return null;
  }

  window.fillForm = function fillForm(row = {}) {
    if (!row || typeof row !== 'object') row = {};

    // Core
    const byId = (id) => document.getElementById(id);
    const setVal = (id, v='') => { const el = byId(id); if (el) el.value = v; };
    const setChk = (id, v=false) => { const el = byId(id); if (el) el.checked = !!v; };

    setVal('entId',      row.id ?? '');
    setVal('entNumber',  row.number ?? '');
    setVal('entName',    row.name ?? '');
    setVal('entTag',     row.tag ?? '');
    setChk('entEnabled', !!row.enabled);

    // Extras
    const statusEl = byId('entStatus');  if (statusEl) statusEl.value = row.status || 'ACTIVE';
    const orgEl    = byId('entOrg');     if (orgEl)    orgEl.value    = row.organization || '';
    const spEl     = byId('entSpoken');  if (spEl)     spEl.value     = row.spoken_name || '';

    // Color text + picker
    const cNorm = normHex(row.color);
    const colorText   = byId('entColor');
    const colorPicker = byId('entColorPicker');
    if (colorText)   colorText.value   = cNorm || '';
    if (colorPicker) colorPicker.value = cNorm || '#22C55E';

    // Last Updated: supports <time id="entUpdated"> or <input id="entUpdatedAt">
    const updEl = byId('entUpdated') || byId('entUpdatedAt');
    if (updEl) {
      if (row.updated_at) {
        const d = new Date(row.updated_at * 1000);
        if (updEl.tagName === 'TIME') {
          updEl.dateTime = d.toISOString();
          updEl.textContent = d.toLocaleString();
        } else {
          updEl.value = d.toLocaleString();
        }
      } else {
        if (updEl.tagName === 'TIME') {
          updEl.dateTime = '';
          updEl.textContent = '—';
        } else {
          updEl.value = '';
        }
      }
    }
  };
})();


/* ===================== CCRS MODEL INDEX (injected) =====================
   Global single source of truth for entrants by id. We keep this idempotent
   so repeated script loads won't redeclare or throw.
======================================================================== */
window.ROSTER_BY_ID = window.ROSTER_BY_ID || new Map();

// Normalize backend rows into a stable shape used by the editor.
window.normalizeEntrant = window.normalizeEntrant || function normalizeEntrant(raw) {
  if (!raw) return null;
  const id = raw.id ?? raw.entrant_id ?? null;
  return {id,
    number:       String(raw.number ?? ''),
    name:         raw.name ?? '',
    tag:          raw.tag ?? null,
    enabled:      Boolean(raw.enabled ?? 1),
    status:       raw.status ?? 'ACTIVE',
    organization: raw.organization ?? '',
    spoken_name:  raw.spoken_name ?? '',
    color:        raw.color ?? null,
    updated_at:   raw.updated_at ?? null,
    logo:         raw.logo ?? null,};
};



  // --- Helpers from base.js (forward-only) ---
  if (!window.CCRS) {
    console.error('[Entrants] CCRS helpers missing. Check /ui/js/base.js path.');
    return;
  }
  const { $, fetchJSON, postJSON, makePoller, setNetStatus } = window.CCRS;

  /* =========================
     Element refs & page state
     ========================= */
  const els = {
    // header
    readyPill:   $('#readyPill'),
    readyWarn:   $('#readyWarn'),

    // scan tag elements (single Tag field + buttons)
    scanBtn:   $('#scanBtn'),
    stopBtn:   $('#stopBtn'),
    assignBtn: $('#assignBtn'),
    scanBar:   $('#scanBar'),
    scanMsg:   $('#scanMsg'),
    scanProg:  document.querySelector('.scan-progress'),

    // drawer
    entForm:     $('#entForm'),
    entId:       $('#entId'),
    entNumber:   $('#entNumber'),
    entName:     $('#entName'),
    entTag:      $('#entTag'),
    entEnabled:  $('#entEnabled'),
    saveBtn:     $('#saveBtn'),
    resetBtn:    $('#resetBtn'),
    newBtn:      $('#newBtn'),
    formMsg:     $('#formMsg'),

    // NEW — color controls (hex text + native picker)
    entColor:        $('#entColor'),        // text input (hex)
    entColorPicker:  $('#entColorPicker'),  // <input type="color">

    // table & filters
    rows:        $('#rows'),
    q:           $('#q'),
    refreshBtn:  $('#refreshBtn'),
    filterChips: document.querySelectorAll('.btn-chip[data-filter]'),
    sortHeads:   document.querySelectorAll('.ent-thead .clickable'),

    // modals
    importBtn:   $('#importBtn'),
    importModal: $('#importModal'),

    quickCreateModal: $('#quickCreateModal'),
    qcTag:       $('#qcTag'),
    qcNumber:    $('#qcNumber'),
    qcName:      $('#qcName'),
    qcEnabled:   $('#qcEnabled'),
    qcCreate:    $('#qcCreate'),
    qcCancel:    $('#qcCancel'),
    qcMsg:       $('#qcMsg'),
  };

  /* ===== Config (can be overridden by YAML-injected globals) ===== */
  const CONFIG = {
    SCAN_MS:        window.CCRS?.CONFIG?.SCAN_MS ?? 10_000,
    MIN_TAG_LEN:    window.CCRS?.CONFIG?.MIN_TAG_LEN ?? 7,
    SSE_URL:        window.CCRS?.CONFIG?.SSE_URL ?? '/ilap/stream',
    POLL_URL:       window.CCRS?.CONFIG?.POLL_URL ?? '/ilap/peek',
    POLL_INTERVAL:  window.CCRS?.CONFIG?.POLL_INTERVAL ?? 200,  // ms
  };

  /* ===== Scan state ===== */
  let scanTimer = null;
  let scanStartedAt = 0;
  let scanBarTimer = null;
  let lastScannedTag = null;
  let scanCancel = null; // function to stop adapter

  // dataset & UI flags
  let ALL = [];                 // GET /admin/entrants (enabled + disabled)
  let dbReady = false;          // /readyz
  let selectedId = null;        // currently selected entrant id
  let filterMode = 'all';       // 'all' | 'enabled' | 'disabled'
  let sortKey = 'number';       // sort column
  let sortDir = 1;              // 1 asc, -1 desc

  /* ================================================
     Scan Adapter: SSE preferred, Polling fallback
     =============================================== */
  async function startScanSession(onTag) {
    // --- Preflight SSE to avoid noisy 404 and reconnect loops ---
    let sseOk = false;
    try {
      const head = await fetch(CONFIG.SSE_URL, {
        method: 'GET',
        headers: { 'Accept': 'text/event-stream' },
        cache: 'no-store'
      });
      sseOk = head.ok && (head.headers.get('content-type') || '').includes('text/event-stream');
    } catch (_) { sseOk = false; }

    if (sseOk) {
      try {
        const es = new EventSource(CONFIG.SSE_URL, { withCredentials: false });
        let active = true;
        const seen = new Set();
        const close = () => { if (active) { active = false; es.close(); } };

        es.addEventListener('tag', (evt) => {
          if (!active) return;
          try {
            const data = JSON.parse(evt.data || '{}');
            const tag = (data.tag || '').trim();
            if (!/^\d+$/.test(tag) || tag.length < CONFIG.MIN_TAG_LEN) return;
            if (seen.has(tag)) return;
            seen.add(tag);
            onTag(tag);
            close(); // first valid tag wins
          } catch { /* ignore parse errors */ }
        });

        // If SSE errors (server drops), close immediately; caller’s timeout still runs.
        es.onerror = () => { close(); };

        return () => close();
      } catch (_) {
        // fall through to polling
      }
    }

    // --- Polling fallback ---
    let active = true;
    const seenAt = { value: null };

    const tick = async () => {
      if (!active) return;
      try {
        const r = await fetch(CONFIG.POLL_URL, { cache: 'no-store' });
        if (!r.ok) throw new Error(`poll ${r.status}`);
        const { tag, seen_at } = await r.json();
        if (tag && /^\d+$/.test(tag) && tag.length >= CONFIG.MIN_TAG_LEN) {
          if (seenAt.value && seen_at && seen_at === seenAt.value) {
            // same sample; ignore
          } else {
            seenAt.value = seen_at || `t${Date.now()}`;
            onTag(String(tag));
            stop(); // first valid wins
            return;
          }
        }
      } catch { /* ignore poll errors during window */ }
      if (active) pollTimer = setTimeout(tick, CONFIG.POLL_INTERVAL);
    };

    let pollTimer = setTimeout(tick, 0);
    const stop = () => { active = false; clearTimeout(pollTimer); };

    return () => stop();
  }


  /* =========================
     scan UI helpers
     ========================= */
  function setScanUI(state, tagText) {
    const { entTag, scanBtn, stopBtn, assignBtn, scanProg, scanBar, scanMsg } = els;

    // Always show track; we’ll style with classes
    scanProg.classList.add('active'); // keep visible baseline

    if (state === 'scanning') {
      entTag.classList.add('scanning');
      entTag.readOnly = true;

      scanBtn.disabled = true;
      stopBtn.disabled = false;
      assignBtn.disabled = true;

      scanProg.classList.remove('ok', 'error');
      scanBar.style.width = '0%';
      scanMsg.textContent = 'Listening for tag…';

    } else if (state === 'captured') {
      entTag.classList.remove('scanning');
      entTag.classList.add('flash-ok');
      setTimeout(() => entTag.classList.remove('flash-ok'), 550);
      entTag.readOnly = false;

      scanBtn.disabled = false;
      stopBtn.disabled = true;
      assignBtn.disabled = false;

      scanProg.classList.add('ok');
      setTimeout(() => { scanProg.classList.remove('ok'); scanBar.style.width = '0%'; }, 600);
      scanMsg.textContent = tagText ? `Captured ${tagText}` : 'Captured';

    } else if (state === 'timeout' || state === 'error') {
      entTag.classList.remove('scanning');
      entTag.classList.add('flash-timeout');
      setTimeout(() => entTag.classList.remove('flash-timeout'), 700);
      entTag.readOnly = false;

      scanBtn.disabled = false;
      stopBtn.disabled = true;
      assignBtn.disabled = !els.entTag.value.trim();

      scanProg.classList.add('error');
      // show red for ~2s, then reset to empty
      setTimeout(() => {
        scanProg.classList.remove('error');
        scanBar.style.width = '0%';
        scanMsg.textContent = '';
      }, 2000);
      scanMsg.textContent = state === 'timeout' ? 'Scan timed out' : 'Scanner offline';

    } else { // idle
      entTag.classList.remove('scanning', 'flash-ok', 'flash-timeout');
      entTag.readOnly = false;

      scanBtn.disabled = false;
      stopBtn.disabled = true;
      assignBtn.disabled = !els.entTag.value.trim();

      // keep track visible but empty
      scanProg.classList.remove('ok', 'error');
      scanBar.style.width = '0%';
      scanMsg.textContent = '';
    }
  }


  function startProgressBar(ms) {
    const { scanBar } = els;
    const start = Date.now();
    clearInterval(scanBarTimer);
    scanBarTimer = setInterval(() => {
      const elapsed = Date.now() - start;
      const pct = Math.max(0, Math.min(100, (elapsed / ms) * 100));
      scanBar.style.width = pct.toFixed(1) + '%';
    }, 100);
  }

  function stopProgressBar() {
    clearInterval(scanBarTimer);
    scanBarTimer = null;
  }

  /* =========================
     Small utils & validators
     ========================= */
  const digitsOnly = s => (s ?? '').replace(/\D+/g, '');
  const clamp      = (s, max) => (s ?? '').trim().slice(0, max);
  const isValidNum  = s => /^\d+$/.test(s) && Number(s) > 0;
  const isValidTeam = s => s.length >= 2 && s.length <= 40;
  const isValidTag  = s => s === '' || /^\d+$/.test(s);

    // === NEW: Color helpers (normalize to #RRGGBB uppercase) =================
    function normalizeHexColor(str) {
      if (!str) return null;
      let s = String(str).trim();
      if (!s) return null;
      if (s[0] !== '#') s = '#' + s;

      const hex = s.slice(1).replace(/[^0-9a-fA-F]/g, '');
      if (hex.length === 3) {
        const R = hex[0], G = hex[1], B = hex[2];
        return `#${R}${R}${G}${G}${B}${B}`.toUpperCase();
      }
      if (hex.length === 6) {
        return `#${hex}`.toUpperCase();
      }
      return null;
    }
    function isStrictHex6(v) {
      return typeof v === 'string' && /^#[0-9A-F]{6}$/.test(v);
    }



  function setFormMsg(text, kind) {
    els.formMsg.textContent = text || '';
    els.formMsg.className = 'form-msg ' + (kind || '');
  }
  function setQCMsg(text, kind) {
    els.qcMsg.textContent = text || '';
    els.qcMsg.className = 'form-msg ' + (kind || '');
  }

  function setReadyUI(ready) {
    dbReady = !!ready;
    if (els.readyPill) {
      els.readyPill.textContent = ready ? 'DB: ready' : 'DB: not ready';
      els.readyPill.classList.toggle('bad', !ready);
    }
    if (els.readyWarn) els.readyWarn.classList.toggle('hidden', !!ready);
  }

  function highlightSelection() {
    document.querySelectorAll('.ent-row').forEach(div => {
      const rid = Number(div.dataset.id || 0);
      div.classList.toggle('selected', selectedId != null && rid === selectedId);
    });
  }
  function flashRowById(id, klass = 'captured') {
    const row = document.querySelector(`.ent-row[data-id="${id}"]`);
    if (!row) return;
    row.classList.add(klass);
    row.scrollIntoView({ block: 'nearest' });
    setTimeout(() => row.classList.remove(klass), 900);
  }

  function findById(id)  { return ALL.find(r => r.id === id); }
  function findByTag(t)  { return ALL.find(r => (r.tag ?? '') === t); }




    /* =========================
     COLOR HELPERS
     ========================= */
  // Normalize a user-provided string to "#RRGGBB" (uppercase).
  // Accepts "#RGB" or "RGB" and expands it; returns null if invalid.
  function normalizeHexColor(str) {
    if (!str) return null;
    let s = String(str).trim();

    // Allow missing leading '#'
    if (s[0] !== '#') s = '#' + s;

    // Remove stray characters (only hex)
    const hex = s.slice(1).replace(/[^0-9a-fA-F]/g, '');

    if (hex.length === 3) {
      // Expand #RGB -> #RRGGBB
      const [r, g, b] = hex.toUpperCase().split('');
      return `#${r}${r}${g}${g}${b}${b}`;
    } else if (hex.length === 6) {
      return `#${hex.toUpperCase()}`;
    }
    return null; // invalid length
  }

  // Return true if value looks like a valid #RRGGBB (strict)
  function isStrictHex6(v) {
    return typeof v === 'string' && /^#[0-9A-F]{6}$/.test(v);
  }








  /* =========================
     Networking
     ========================= */
  async function pollReady() {
    try {
      const r = await fetch('/readyz', { cache: 'no-store' });
      setReadyUI(r.ok);
      CCRS.setNetStatus(true, r.ok ? 'Ready' : 'Not ready');
    } catch {
      setReadyUI(false);
      CCRS.setNetStatus(false, 'Offline');
    }
  }

  async function loadEntrants() {
    try {
      const data = await CCRS.fetchJSON('/admin/entrants', { cache: 'no-store' });
      if (!Array.isArray(data)) throw new Error('Bad entrants payload');
      
      // Build normalized lookup for Edit hydration (id -> full row)
      window.ROSTER_BY_ID.clear();
      const __arr = Array.isArray(data) ? data : [];
      for (const e of __arr) {
        const n = window.normalizeEntrant(e);
        if (n && n.id != null) window.ROSTER_BY_ID.set(n.id, n);
      }
ALL = data;
      render();
      CCRS.setNetStatus(true, `OK — ${ALL.length} entrants`);
      highlightSelection();
    } catch (err) {
      console.error('[Entrants] loadEntrants failed:', err.message || err);
      CCRS.setNetStatus(false, 'Failed to load entrants');
    }
  }

  async function saveEntrant(one) {
    try {
      const res = await CCRS.postJSON('/admin/entrants', { entrants: [ one ] });

      if (res.status === 409) {
        setFormMsg('That tag is already assigned to another enabled entrant.', 'error');
        return { ok:false, code:409, body:null };
      }
      if (!res.ok) {
        const txt = await res.text().catch(()=> '');
        setFormMsg(txt || `Unexpected error (${res.status}).`, 'error');
        return { ok:false, code:res.status, body:txt };
      }

      let body = null;
      try { body = await res.json(); } catch (_) {}
      await loadEntrants();
      setFormMsg('Entrant saved.', 'ok');
      CCRS.setNetStatus(true, 'Entrant saved');
      return { ok:true, code:200, body };
    } catch (err) {
      const code = err?.response?.status ?? 0;
      setFormMsg(err.message || 'Save failed; check connection/logs.', 'error');
      CCRS.setNetStatus(false, 'Save failed');
      return { ok:false, code, body:null };
    }
  }

  /* =========================
     Render helpers
     ========================= */
  function filteredSortedRows() {
    const q = (els.q.value || '').trim().toLowerCase();

    const filt = (row) => {
      if (filterMode === 'enabled' && !row.enabled) return false;
      if (filterMode === 'disabled' && row.enabled) return false;
      if (!q) return true;
      const hay = [
        String(row.number ?? ''),
        String(row.name ?? ''),
        String(row.tag ?? ''),
      ].join(' ').toLowerCase();
      return hay.includes(q);
    };

    const cmp = (a, b, key) => {
      const av = (a[key] ?? '').toString();
      const bv = (b[key] ?? '').toString();
      if (key === 'number' || key === 'id') return (Number(av) - Number(bv)) * sortDir;
      return av.localeCompare(bv) * sortDir;
    };

    return ALL.filter(filt).sort((a, b) => cmp(a, b, sortKey));
  }

  function render() {
    const rows = filteredSortedRows();
    els.rows.innerHTML = '';

    for (const row of rows) {
      const el = document.createElement('div');
      el.className = 'ent-row' + (row.enabled ? '' : ' disabled');
      el.dataset.id = row.id;

      // NEW ORDER (Enabled | Tag | Number | Team | Actions | Status)
      el.innerHTML = `
        <div>${row.enabled ? 'Yes' : 'No'}</div>
        <div class="tag-cell"><span class="mono">${row.tag ?? '—'}</span></div>
        <div class="right mono">${row.number ?? ''}</div>
        <div class="name">${clamp(row.name ?? '', 64)}</div>
        <div class="actions">
          <button class="btn btn-sm btn-chip" data-act="edit" title="Edit">Edit</button>
          <button class="btn btn-sm btn-chip" data-act="tag" title="Assign tag">Set Tag</button>
        </div>
        <div class="right small muted"></div>
      `;

      el.querySelector('[data-act="edit"]').addEventListener('click', () => window.fillForm(row));
      el.querySelector('[data-act="tag"]').addEventListener('click', async () => {
        const current = String(row.tag ?? '');
        const val = prompt(`Set tag for “${row.name}” (digits only; blank = clear):`, current);
        if (val === null) return;
        const clean = val.trim();
        if (clean !== '' && !/^\d+$/.test(clean)) { CCRS.setNetStatus(false, 'Tag must be digits only'); return; }
        const body = {
          id,
          number: full.number,
          name: full.name,
          tag: clean === '' ? null : clean,
          enabled: !!full.enabled,
          status: full.status ?? 'ACTIVE',
          organization: full.organization ?? '',
          spoken_name: full.spoken_name ?? '',
          color: full.color ?? null,
        };

        el.classList.add('captured');
        const { ok, code } = await saveEntrant(body);
        if (!ok) {
          el.classList.remove('captured');
          if (code === 409) handleConflict(clean);
        } else {
          selectedId = row.id;
          highlightSelection();
          flashRowById(row.id, 'captured');
        }
      });

      el.addEventListener('dblclick', (ev) => {
        // Ignore double-clicks that originate from action buttons
        if (ev.target.closest('.actions')) return;

        // Resolve id from row or dataset
        const id = (row.id ?? row.entrant_id ?? Number(el?.dataset?.id));

        // Prefer a normalized, cached full row when available
        const full = (window.ROSTER_BY_ID instanceof Map)
          ? (window.ROSTER_BY_ID.get(id) || (window.normalizeEntrant ? window.normalizeEntrant(row) : row))
          : row;

        window.fillForm(full);
      });

      els.rows.appendChild(el);
    }

    highlightSelection();
  }

  /* =========================
     Drawer / Form
     ========================= */
  function clearForm() {
    selectedId = null;
    try { els && els.entId && (els.entId.value = ''); } catch(_){}
    try { els && els.entNumber && (els.entNumber.value = ''); } catch(_){}
    try { els && els.entName && (els.entName.value = ''); } catch(_){}
    try { els && els.entTag && (els.entTag.value = ''); } catch(_){}
    try { els && els.entEnabled && (els.entEnabled.checked = true); } catch(_){}

    const setVal = (id, v='') => { const el = document.getElementById(id); if (el) el.value = v; };
    setVal('entOrg', '');
    setVal('entSpoken', '');
    setVal('entColor', '');
    setVal('entColorPicker', '#22C55E');
    const statusEl = document.getElementById('entStatus'); if (statusEl) statusEl.value = 'ACTIVE';

    const updEl = document.getElementById('entUpdated') || document.getElementById('entUpdatedAt');
    if (updEl) {
      if (updEl.tagName === 'TIME') { updEl.dateTime = ''; updEl.textContent = '—'; }
      else { updEl.value = ''; }
    }
    document.querySelectorAll('.ent-row.active').forEach(el => el.classList.remove('active'));
    setFormMsg && setFormMsg('', 'ok');
}
  function fillForm(row = {}) {
  // Robust left-pane hydration with normalized row
  if (!row || typeof row !== 'object') row = {};
  selectedId = row.id ?? null;

  // Core
  try { els && els.entId && (els.entId.value = row.id ?? ''); } catch (_){}
  try { els && els.entNumber && (els.entNumber.value = row.number ?? ''); } catch (_){}
  try { els && els.entName && (els.entName.value = row.name ?? ''); } catch (_){}
  try { els && els.entTag && (els.entTag.value = row.tag ?? ''); } catch (_){}
  try { els && els.entEnabled && (els.entEnabled.checked = !!row.enabled); } catch (_){}

  // Extras via document.getElementById to be resilient
  (function(){
    const statusEl = document.getElementById('entStatus');
    if (statusEl) statusEl.value = row.status || 'ACTIVE';

    const orgEl = document.getElementById('entOrg');
    if (orgEl) orgEl.value = row.organization || '';

    const spokenEl = document.getElementById('entSpoken');
    if (spokenEl) spokenEl.value = row.spoken_name || '';

    const normalizeHex = (v) => {
      if (v == null) return null;
      const s = String(v).trim().replace(/^#/, '');
      if (/^[0-9a-f]{3}$/i.test(s)) return ('#' + s[0]+s[0]+s[1]+s[1]+s[2]+s[2]).toUpperCase();
      if (/^[0-9a-f]{6}$/i.test(s)) return ('#' + s).toUpperCase();
      return null;
    };
    const c = normalizeHex(row.color);
    const colorText   = document.getElementById('entColor');
    const colorPicker = document.getElementById('entColorPicker');
    if (colorText)   colorText.value   = c || '';
    if (colorPicker) colorPicker.value = c || '#22C55E';

    const updEl = document.getElementById('entUpdated') || document.getElementById('entUpdatedAt');
    if (updEl) {
      if (row.updated_at) {
        const d = new Date(row.updated_at * 1000);
        if (updEl.tagName === 'TIME') { updEl.dateTime = d.toISOString(); updEl.textContent = d.toLocaleString(); }
        else { updEl.value = d.toLocaleString(); }
      } else {
        if (updEl.tagName === 'TIME') { updEl.dateTime = ''; updEl.textContent = '—'; }
        else { updEl.value = ''; }
      }
    }
  })();
}


  function validateForm(show) {
    const num = els.entNumber.value.trim();
    const name = els.entName.value.trim();
    const tag = els.entTag.value.trim();
    let msg = '';
    if (!isValidNum(num)) msg = 'Number must be a positive integer.';
    else if (!isValidTeam(name)) msg = 'Team must be 2–40 characters.';
    else if (!isValidTag(tag)) msg = 'Tag must be digits only (or blank).';
    if (show) setFormMsg(msg, msg ? 'error' : 'ok');
    return !msg;
  }

  function formToPayload() {
    const id        = els.entId?.value ? Number(els.entId.value) : null;
    const numberStr = els.entNumber?.value?.trim() ?? '';
    const name      = String(els.entName?.value ?? '').slice(0, 40);

    const tagRaw = els.entTag?.value?.trim() ?? '';
    const tag    = tagRaw === '' ? null : tagRaw.replace(/\D+/g, '');

    const enabled = !!(els.entEnabled && els.entEnabled.checked);

    // Normalize color to #RRGGBB or null
    let color = null;
    if (els.entColor) {
      const s = String(els.entColor.value || '').trim().replace(/^#/, '');
      if (/^[0-9a-f]{3}$/i.test(s)) color = ('#' + s[0]+s[0]+s[1]+s[1]+s[2]+s[2]).toUpperCase();
      else if (/^[0-9a-f]{6}$/i.test(s)) color = ('#' + s).toUpperCase();
    }

    // NEW: pull the extra fields
    const organization = document.getElementById('entOrg')?.value?.trim() || '';
    const spoken_name  = document.getElementById('entSpoken')?.value?.trim() || '';
    const status       = document.getElementById('entStatus')?.value || 'ACTIVE';

    return {
      id,
      number: Number(numberStr),
      name,
      tag,
      enabled,
      color,
      organization,   // <<< now included
      spoken_name,    // <<< now included
      status,
    };
  }


  /* =========================
     409 Conflict handling
     ========================= */
  function handleConflict(tagDigits) {
    setFormMsg('Tag already assigned to another enabled entrant.', 'error');
    const r = findByTag(tagDigits);
    if (r) {
      flashRowById(r.id, 'conflict');
      selectedId = r.id;
      highlightSelection();
    }
    // focus the single Tag field
    els.entTag.value = tagDigits;
    els.entTag.focus();
    CCRS.setNetStatus(false, '409 — duplicate tag');
  }

  /* =========================
     Quick-create flows (used by Assign when unknown tag) 
     ========================= */
  function openQuickCreate(tagDigits) {
    els.qcTag.value = tagDigits;
    els.qcName.value = `Unknown ${tagDigits}`;
    els.qcNumber.value = '';
    els.qcEnabled.checked = true;
    setQCMsg('', '');
    els.quickCreateModal.showModal();
    setTimeout(() => els.qcNumber.focus(), 30);
  }

  async function createFromQuick() {
    const num = els.qcNumber.value.trim();
    const name = els.qcName.value.trim();
    const tag = els.qcTag.value.trim();
    const enabled = !!els.qcEnabled.checked;

    if (!isValidNum(num))   { setQCMsg('Number must be a positive integer.', 'error'); return; }
    if (!isValidTeam(name)) { setQCMsg('Team must be 2–40 characters.', 'error'); return; }

    const payload = {
      id: null,
      number: Number(num),
      name,
      tag: tag ? tag.replace(/\D+/g, '') : null,
      enabled,
      color: null,
      organization: '',
      spoken_name: '',
      status: 'ACTIVE',
    };

    const { ok, code, body } = await saveEntrant(payload);

    if (!ok) {
      if (code === 409) { els.quickCreateModal.close(); handleConflict(digitsOnly(tag)); return; }
      setQCMsg('Save failed — check connection/logs.', 'error');
      return;
    }

    // Focus the newly created entrant (via assigned_ids or by tag)
    let newId = null;
    try {
      const assigned = body?.assigned_ids;
      if (Array.isArray(assigned) && assigned.length) newId = assigned[0];
    } catch (_) {}

    if (!newId) {
      const r = findByTag(digitsOnly(tag));
      if (r) newId = r.id;
    }

    els.quickCreateModal.close();

    // put the captured tag back into the single Tag field, focus it
    els.entTag.value = digitsOnly(tag);
    els.entTag.focus();

    if (newId != null) {
      selectedId = newId;
      const row = findById(newId);
      if (row) window.fillForm(row);
      flashRowById(newId, 'captured');
    }
  }

  /* =========================
     Event wiring & startup
     ========================= */
  function wireEvents() {
    // Filters & sorting
    els.q.addEventListener('input', render);
    els.filterChips.forEach(btn => {
      btn.addEventListener('click', () => {
        els.filterChips.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        filterMode = btn.dataset.filter;
        render();
      });
    });
    els.sortHeads.forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = 1; }
        render();
      });
    });

    // Drawer buttons
    els.resetBtn.addEventListener('click', clearForm);
    els.newBtn.addEventListener('click', clearForm);
    els.saveBtn.addEventListener('click', async () => {
      if (!validateForm(true)) return;
      if (!dbReady) { setFormMsg('DB is not ready; please wait.', 'error'); return; }
      const one = formToPayload();
      //console.log('[POST /admin/entrants] payload:', one); // DEBUG
      const { ok } = await saveEntrant(one);
      if (ok) clearForm();
    });

    // Drawer keyboard: Enter = save; Esc = reset
    // Keyboard shortcuts (scoped to the form)
    // - Ignore keystrokes while the user is typing in an editable control.
    // - Use Alt+T to start Scan so plain 't' in names never fires.
    // - Keep Esc to stop an active scan (safe and expected).
    els.entForm.addEventListener('keydown', (ev) => {
      const el = ev.target;
      const tag = (el && el.tagName) ? el.tagName.toUpperCase() : '';
      const typing = (
        tag === 'INPUT' ||
        tag === 'TEXTAREA' ||
        tag === 'SELECT' ||
        (el && el.isContentEditable)
      );
      if (typing) return; // do not trigger shortcuts while typing in a field

      const k = (ev.key || '').toLowerCase();

      // Stop scan: Escape (no modifier)
      if (k === 'escape' && scanCancel) {
        ev.preventDefault();
        onScanStop();
        return;
      }

      // Start scan: Alt+T (prevents collisions with 't' in names)
      if (k === 't' && ev.altKey && !scanCancel) {
        ev.preventDefault();
        onScanStart();
        return;
      }
    });


    // Input guards
    els.entNumber.addEventListener('input', () => {
      els.entNumber.value = els.entNumber.value.replace(/[^\d]/g, '');
    });
    els.entTag.addEventListener('input', () => {
      if (!/^\d*$/.test(els.entTag.value)) {
        els.entTag.value = els.entTag.value.replace(/[^\d]/g, '');
      }
      // Enable Assign if any tag is present when idle
      if (!scanCancel) els.assignBtn.disabled = !els.entTag.value.trim();
    });

      // NEW — Color sync: hex <-> picker (soft validation)
      if (els.entColor && els.entColorPicker) {
        // Hex -> Picker
        els.entColor.addEventListener('input', () => {
          const norm = normalizeHexColor(els.entColor.value);
          if (norm && isStrictHex6(norm)) {
            els.entColorPicker.value = norm;
            if (els.entColor.value !== norm) els.entColor.value = norm; // normalize in-place
            els.entColor.classList.remove('input-error');
          } else {
            // keep typing allowed; just add a gentle error style (optional CSS)
            els.entColor.classList.add('input-error');
          }
        });

        // Picker -> Hex
        els.entColorPicker.addEventListener('input', () => {
          const v = String(els.entColorPicker.value || '').toUpperCase();
          if (isStrictHex6(v)) {
            els.entColor.value = v;
            els.entColor.classList.remove('input-error');
          }
        });
      }


    // Refresh & bulk stub
    els.refreshBtn.addEventListener('click', loadEntrants);
    els.importBtn.addEventListener('click', () => els.importModal.showModal());

    // Quick-create modal
    els.qcCreate.addEventListener('click', (e) => { e.preventDefault(); createFromQuick(); });
    els.qcCancel.addEventListener('click', () => els.quickCreateModal.close());
    els.quickCreateModal.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); createFromQuick(); }
    });

    // Scan/Stop/Assign
    els.scanBtn.addEventListener('click', onScanStart);
    els.stopBtn.addEventListener('click', onScanStop);
    els.assignBtn.addEventListener('click', onAssign);

    // --- Scan control fns inside wireEvents (scoped) ---
    async function onScanStart() {
      if (!dbReady) { CCRS.setNetStatus(false, 'DB not ready'); return; }
      lastScannedTag = null;
      setScanUI('scanning');
      startProgressBar(CONFIG.SCAN_MS);

      // Start adapter (SSE preferred, else polling)
      scanCancel = await startScanSession((tag) => onTagCaptured(tag));

      // Timeout in SCAN_MS
      clearTimeout(scanTimer);
      scanStartedAt = Date.now();
      scanTimer = setTimeout(() => onScanTimeout(), CONFIG.SCAN_MS);
    }

    function onScanTimeout() {
      try { scanCancel && scanCancel(); } catch {}
      scanCancel = null;
      stopProgressBar();
      setScanUI('timeout');
    }

    function onScanStop() {
      try { scanCancel && scanCancel(); } catch {}
      scanCancel = null;
      clearTimeout(scanTimer);
      stopProgressBar();
      setScanUI('idle');
    }

    function onTagCaptured(tag) {
      // First valid tag wins within the window
      clearTimeout(scanTimer);
      stopProgressBar();
      try { scanCancel && scanCancel(); } catch {}
      scanCancel = null;

      lastScannedTag = tag;
      els.entTag.value = tag;
      setScanUI('captured', tag);

      // Lookup locally
      const existing = findByTag(tag);
      if (existing) {
        fillForm(existing);
        selectedId = existing.id;
        highlightSelection();
        flashRowById(existing.id, 'captured');
        CCRS.setNetStatus(true, `Tag already on ${existing.name} (#${existing.number})`);
      } else {
        // Nudge required fields
        els.entNumber.focus();
        document.querySelector('label[for="entNumber"]')?.classList.add('attn');
        document.querySelector('label[for="entName"]')?.classList.add('attn');
        setTimeout(() => {
          document.querySelector('label[for="entNumber"]')?.classList.remove('attn');
          document.querySelector('label[for="entName"]')?.classList.remove('attn');
        }, 1200);
      }
    }

    async function onAssign() {
      const tag = (els.entTag.value || '').trim();
      if (!/^\d+$/.test(tag) || tag.length < CONFIG.MIN_TAG_LEN) {
        CCRS.setNetStatus(false, 'Tag must be 7+ digits');
        return;
      }

      // If a row is selected → assign to that entrant
      if (selectedId != null) {
        const row = findById(selectedId);
        if (!row) { CCRS.setNetStatus(false, 'Selection lost — refresh'); return; }
        const body = {
          id,
          number: full.number,
          name: full.name,
          tag: clean === '' ? null : clean,
          enabled: !!full.enabled,
          status: full.status ?? 'ACTIVE',
          organization: full.organization ?? '',
          spoken_name: full.spoken_name ?? '',
          color: full.color ?? null,
        };

        const { ok, code } = await saveEntrant(body);
        if (!ok && code === 409) { handleConflict(tag); return; }
        if (ok) { flashRowById(row.id, 'captured'); CCRS.setNetStatus(true, 'Tag assigned'); }
        return;
      }

      // No selection:
      const existing = findByTag(tag);
      if (existing) {
        selectedId = existing.id;
        fillForm(existing);
        flashRowById(existing.id, 'captured');
        CCRS.setNetStatus(true, `Loaded ${existing.name} (#${existing.number})`);
        return;
      }

      // Unknown → quick-create
      openQuickCreate(tag);
    }

    // expose a safe testing hook for the console
    window.CCRS = window.CCRS || {};
    window.CCRS.scanTest = onTagCaptured;


  }

  // Boot
  (function start() {
    // Immediate “connecting” text; flips on data/ready events
    CCRS.setNetStatus(true, 'Connecting…');

    // Ready poller + immediate check so pill updates fast
    const readyPoll = makePoller(pollReady, 2500, () => setReadyUI(false));
    readyPoll.start();
    pollReady().catch(() => setReadyUI(false));

    // Initial data load & wireup
    loadEntrants();
    wireEvents();
  })();

})();

/* =======================================================================
   HINTS → ⓘ TOOLTIP SYSTEM
   - Converts every .hint.small inside .pane--left .field into an on-demand
     tooltip: a small ⓘ button next to the label and a positioned popover.
   - Accessibility: the original hint node remains in DOM (visually hidden
     via CSS) so screen readers can continue to announce it.
   - Close behavior: click outside or press Escape.
   ======================================================================= */

/* ===== Helper: simple unique id for tooltip elements ==================== */
let __tipSeq = 0;
function __nextTipId() {
  __tipSeq += 1;
  return `tip_${Date.now().toString(36)}_${__tipSeq}`;
}

/* ===== Close all open popovers within a scope (left pane) =============== */
function __closeAllTooltips(scope, returnFocus = false) {
  const openBtns = scope.querySelectorAll('.info-tip-btn[aria-expanded="true"]');
  const pops     = scope.querySelectorAll('.info-tip-popover:not([hidden])');
  pops.forEach((p) => p.setAttribute('hidden', ''));
  openBtns.forEach((b) => {
    b.setAttribute('aria-expanded', 'false');
    if (returnFocus) b.focus();
  });
}

/* ===== Initialize tooltips: scan fields, inject button + popover =========
   NOTE: This function is idempotent — it won't duplicate buttons if called
         more than once (e.g., after partial rerenders).
========================================================================= */
// === Auto-hide timing ======================================================
const TIP_AUTO_HIDE_MS = 1500; // 1.5s; bump to 2000 if you want a bit longer

// Arm a new auto-hide timer for this tooltip; cancel any existing one first.
function __armAutoHide(btn, pop) {
  __clearAutoHide(btn, pop);
  const tid = window.setTimeout(() => {
    // Only close if still open
    if (btn.getAttribute('aria-expanded') === 'true' && !pop.hasAttribute('hidden')) {
      btn.setAttribute('aria-expanded', 'false');
      pop.setAttribute('hidden', '');
    }
  }, TIP_AUTO_HIDE_MS);
  // Store timer id on both elements so we can cancel from either
  btn.dataset.tipTid = String(tid);
  pop.dataset.tipTid = String(tid);
}

// Cancel any pending auto-hide timer for this tooltip pair
function __clearAutoHide(btn, pop) {
  const ids = [btn?.dataset?.tipTid, pop?.dataset?.tipTid];
  ids.forEach(id => {
    if (id) {
      window.clearTimeout(Number(id));
    }
  });
  delete btn.dataset.tipTid;
  delete pop.dataset.tipTid;
}

function initFieldHintsAsTooltips() {
  const leftPane = document.querySelector('.pane--left');
  if (!leftPane) return;

  const fields = leftPane.querySelectorAll('.field');
  fields.forEach((field) => {
    const label = field.querySelector('.label');
    const hint  = field.querySelector('.hint.small');
    if (!label || !hint) return;

    const hintText = hint.textContent.trim();
    if (!hintText) return;

    // Avoid duplicates if re-initialized
    if (label.querySelector('.info-tip-btn')) return;

    // Create the ⓘ button (keyboard focusable)
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'info-tip-btn';
    btn.setAttribute('aria-label', 'Show help');
    btn.setAttribute('aria-expanded', 'false');
    btn.textContent = 'i'; // simple glyph; easy to read outdoors

    // Create the popover; positioned relative to the field wrapper
    const pop = document.createElement('div');
    pop.className = 'info-tip-popover';
    pop.setAttribute('role', 'tooltip');
    pop.setAttribute('hidden', '');
    pop.textContent = hintText;

    // Link button ↔ popover for a11y
    const tipId = __nextTipId();
    pop.id = tipId;
    btn.setAttribute('aria-controls', tipId);

    // Ensure the field is a positioning context for the popover
    if (getComputedStyle(field).position === 'static') {
      field.style.position = 'relative'; // safe on wrapper elements
    }

    // Insert the button at end of label and the popover inside the field
    label.appendChild(btn);
    field.appendChild(pop);

      // Toggle behavior (with auto-hide)
      // - Opens the popover and arms a 1.5s auto-hide timer.
      // - Clicking again closes immediately.
      // - Hovering the ⓘ or the popover pauses the timer; leaving re-arms it.
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = btn.getAttribute('aria-expanded') === 'true';

        // Close any other open tooltips first (also clears their timers)
        __closeAllTooltips(leftPane);

        if (!isOpen) {
          // Open this one
          btn.setAttribute('aria-expanded', 'true');
          pop.removeAttribute('hidden');

          // Start auto-hide countdown
          __armAutoHide(btn, pop);
        } else {
          // Manual close
          __clearAutoHide(btn, pop);
          btn.setAttribute('aria-expanded', 'false');
          pop.setAttribute('hidden', '');
        }
      });

    // Pause auto-hide while pointer or keyboard focus is on the ⓘ button
    btn.addEventListener('mouseenter', () => __clearAutoHide(btn, pop));
    btn.addEventListener('focus',      () => __clearAutoHide(btn, pop));

    // Pause auto-hide while pointer or keyboard focus is on the tooltip bubble
    pop.addEventListener('mouseenter', () => __clearAutoHide(btn, pop));
    pop.addEventListener('focus',      () => __clearAutoHide(btn, pop));

    // Re-arm auto-hide when leaving the ⓘ button (pointer or focus)
    btn.addEventListener('mouseleave', () => {
      if (btn.getAttribute('aria-expanded') === 'true' && !pop.hasAttribute('hidden')) {
        __armAutoHide(btn, pop);
      }
    });
    btn.addEventListener('blur', () => {
      if (btn.getAttribute('aria-expanded') === 'true' && !pop.hasAttribute('hidden')) {
        __armAutoHide(btn, pop);
      }
    });

    // Re-arm auto-hide when leaving the tooltip bubble (pointer or focus)
    pop.addEventListener('mouseleave', () => {
      if (btn.getAttribute('aria-expanded') === 'true' && !pop.hasAttribute('hidden')) {
        __armAutoHide(btn, pop);
      }
    });
    pop.addEventListener('blur', () => {
      if (btn.getAttribute('aria-expanded') === 'true' && !pop.hasAttribute('hidden')) {
        __armAutoHide(btn, pop);
      }
    });

  }); 

  // Global listeners: click outside + Escape closes any open popovers
  document.addEventListener('click', (ev) => {
    if (!leftPane.contains(ev.target)) __closeAllTooltips(leftPane);
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') __closeAllTooltips(leftPane, /*returnFocus*/true);
  });
}

/* ===== Auto-init on DOM ready (non-invasive) =============================
   We call this in addition to any existing boot logic you already have.
   If your code already has a DOMContentLoaded handler, this simply adds
   one more callback and won't interfere with your sequence.
========================================================================= */
document.addEventListener('DOMContentLoaded', () => {
  try {
    initFieldHintsAsTooltips();
  } catch (e) {
    // Fail-safe: tooltip issues should never block the app
    console.warn('Tooltip init failed:', e);
  }
});


// expose for external handlers
window.fillForm = fillForm;


/* __DELEGATED_EDIT__ — always hydrate with full row, even if a per-row handler ran first */
(function(){
  const root = document.getElementById('rows');
  if (!root) return;
  root.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act="edit"]');
    if (!btn) return;
    const rowEl = btn.closest('.ent-row');
    const id = Number(rowEl?.dataset?.id);
    const full = (window.ROSTER_BY_ID instanceof Map) ? (window.ROSTER_BY_ID.get(id) || null) : null;
    if (full) { window.fillForm(full); }
  });
})();
