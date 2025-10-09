/* ==========================================================================
   CCRS — Entrants & Tags page logic
   - Forward-only helpers via window.CCRS
   - setNetStatus(ok:boolean, message:string)
   - DB pill uses /readyz (immediate + poller)
   - Entrants CRUD via /admin/entrants (bulk contract, single item)
   ========================================================================== */
(function () {
  'use strict';

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
      setNetStatus(true, r.ok ? 'Ready' : 'Not ready');
    } catch {
      setReadyUI(false);
      setNetStatus(false, 'Offline');
    }
  }

  async function loadEntrants() {
    try {
      const data = await fetchJSON('/admin/entrants', { cache: 'no-store' });
      if (!Array.isArray(data)) throw new Error('Bad entrants payload');
      ALL = data;
      render();
      setNetStatus(true, `OK — ${ALL.length} entrants`);
      highlightSelection();
    } catch (err) {
      console.error('[Entrants] loadEntrants failed:', err.message || err);
      setNetStatus(false, 'Failed to load entrants');
    }
  }

  async function saveEntrant(one) {
    try {
      const res = await postJSON('/admin/entrants', { entrants: [ one ] });

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
      setNetStatus(true, 'Entrant saved');
      return { ok:true, code:200, body };
    } catch (err) {
      const code = err?.response?.status ?? 0;
      setFormMsg(err.message || 'Save failed; check connection/logs.', 'error');
      setNetStatus(false, 'Save failed');
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

      el.querySelector('[data-act="edit"]').addEventListener('click', () => fillForm(row));
      el.querySelector('[data-act="tag"]').addEventListener('click', async () => {
        const current = String(row.tag ?? '');
        const val = prompt(`Set tag for “${row.name}” (digits only; blank = clear):`, current);
        if (val === null) return;
        const clean = val.trim();
        if (clean !== '' && !/^\d+$/.test(clean)) { setNetStatus(false, 'Tag must be digits only'); return; }
        const body = { id: row.id, number: row.number, name: row.name, tag: clean === '' ? null : clean, enabled: row.enabled };
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
        if (ev.target.closest('.actions')) return;
        fillForm(row);
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
    els.entId.value = '';
    els.entNumber.value = '';
    els.entName.value = '';
    els.entTag.value = '';
    els.entEnabled.checked = true;
    setFormMsg('', '');
    highlightSelection();
  }
  function fillForm(row = {}) {
    // row now defaults to an empty object if undefined
    selectedId = row.id ?? null;

    els.entId.value      = row.id ?? '';
    els.entNumber.value  = row.number ?? '';
    els.entName.value    = row.name ?? '';
    els.entTag.value     = row.tag ?? '';
    els.entEnabled.checked = !!row.enabled;

    // NEW: defensive defaults for color sync
    if (els.entColor && els.entColorPicker) {
      const norm = normalizeHexColor(row.color);
      if (norm && isStrictHex6(norm)) {
        els.entColor.value    = norm;
        els.entColorPicker.value = norm;
        els.entColor.classList.remove('input-error');
      } else {
        els.entColor.value    = '';
        els.entColorPicker.value = '#22C55E';
        els.entColor.classList.remove('input-error');
      }
    }

    // ... rest of your fillForm() logic
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
    const id = els.entId.value ? Number(els.entId.value) : null;
    const numberStr = els.entNumber.value.trim();
    const name = clamp(els.entName.value, 40);
    const tagRaw = els.entTag.value.trim();
    const tag = tagRaw === '' ? null : digitsOnly(tagRaw);
    const enabled = !!els.entEnabled.checked;

      // NEW — Color normalized to #RRGGBB or null
    let color = null;
    if (els.entColor) {
      const norm = normalizeHexColor(els.entColor.value);
      color = (norm && isStrictHex6(norm)) ? norm : null;
    }

    return { id, number: Number(numberStr), name, tag, enabled, color };
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
    setNetStatus(false, '409 — duplicate tag');
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

    const payload = { id: null, number: Number(num), name, tag: digitsOnly(tag), enabled };
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
      if (row) fillForm(row);
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
      const { ok } = await saveEntrant(one);
      if (ok) clearForm();
    });

    // Drawer keyboard: Enter = save; Esc = reset
    els.entForm.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); if (validateForm(true)) saveEntrant(formToPayload()).then(({ok}) => { if (ok) clearForm(); }); }
      if (ev.key === 'Escape') { ev.preventDefault(); clearForm(); }
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

    // Keyboard shortcuts (optional but handy)
    els.entForm.addEventListener('keydown', (ev) => {
      if (ev.key === 't' && !scanCancel) { ev.preventDefault(); onScanStart(); }
      if (ev.key === 'Escape' && scanCancel) { ev.preventDefault(); onScanStop(); }
    });

    // --- Scan control fns inside wireEvents (scoped) ---
    async function onScanStart() {
      if (!dbReady) { setNetStatus(false, 'DB not ready'); return; }
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
        setNetStatus(true, `Tag already on ${existing.name} (#${existing.number})`);
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
        setNetStatus(false, 'Tag must be 7+ digits');
        return;
      }

      // If a row is selected → assign to that entrant
      if (selectedId != null) {
        const row = findById(selectedId);
        if (!row) { setNetStatus(false, 'Selection lost — refresh'); return; }
        const body = { id: row.id, number: row.number, name: row.name, tag, enabled: row.enabled };
        const { ok, code } = await saveEntrant(body);
        if (!ok && code === 409) { handleConflict(tag); return; }
        if (ok) { flashRowById(row.id, 'captured'); setNetStatus(true, 'Tag assigned'); }
        return;
      }

      // No selection:
      const existing = findByTag(tag);
      if (existing) {
        selectedId = existing.id;
        fillForm(existing);
        flashRowById(existing.id, 'captured');
        setNetStatus(true, `Loaded ${existing.name} (#${existing.number})`);
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
    setNetStatus(true, 'Connecting…');

    // Ready poller + immediate check so pill updates fast
    const readyPoll = makePoller(pollReady, 2500, () => setReadyUI(false));
    readyPoll.start();
    pollReady().catch(() => setReadyUI(false));

    // Initial data load & wireup
    loadEntrants();
    wireEvents();
  })();

})();
