/* ==========================================================================
  CCRS - Entrants & Tags (Bootstrap refactor)
  - Works with the Bootstrap-only entrants.html we just migrated
  - Removes reliance on custom CSS (.field/.pane/.ent-row/.btn-chip/.hidden)
  - Renders a real Bootstrap table inside #rows
  - Uses d-none for visibility toggles
============================================================================ */
(function () {
  'use strict';

  // ----- Require CCRS helpers (from /ui/js/base.js) -----
  if (!window.CCRS) {
    console.error('[Entrants] window.CCRS missing. Check /ui/js/base.js path.');
    return;
  }
  const CCRS = window.CCRS;
  const { $, fetchJSON, postJSON, makePoller, setNetStatus } = CCRS;

  // ----- DOM refs (match Bootstrap entrants.html) -----
  const els = {
    // readiness
    readyWarn: $('#readyWarn'),

    // scan controls
    entTag: $('#entTag'),
    scanBtn: $('#scanBtn'),
    stopBtn: $('#stopBtn'),
    assignBtn: $('#assignBtn'),
    scanBar: $('#scanBar'),
    scanMsg: $('#scanMsg'),

    // form
    entForm: $('#entForm'),
    entId: $('#entId'),
    entNumber: $('#entNumber'),
    entName: $('#entName'),
    entOrg: $('#entOrg'),
    entSpoken: $('#entSpoken'),
    entStatus: $('#entStatus'),
    entEnabled: $('#entEnabled'),
    entStatusOtherRow: $('#entStatusOtherRow'),
    entStatusOther: $('#entStatusOther'),
    entUpdated: $('#entUpdated'),
    entUpdatedAt: $('#entUpdatedAt'),

    entColor: $('#entColor'),
    entColorPicker: $('#entColorPicker'),

    saveBtn: $('#saveBtn'),
    resetBtn: $('#resetBtn'),
    newBtn: $('#newBtn'),
    formMsg: $('#formMsg'),

    // list
    rowsRoot: $('#rows'),
    q: $('#q'),
    refreshBtn: $('#refreshBtn'),
    filterBtns: document.querySelectorAll('button[data-filter]'),

    // bulk
    importBtn: $('#importBtn'),
    exportBtn: $('#exportBtn'),

    // quick-create dialog
    quickCreateModal: $('#quickCreateModal'),
    qcTag: $('#qcTag'),
    qcNumber: $('#qcNumber'),
    qcName: $('#qcName'),
    qcEnabled: $('#qcEnabled'),
    qcCreate: $('#qcCreate'),
    qcCancel: $('#qcCancel'),
    qcMsg: $('#qcMsg'),
  };

  // ----- Config -----
  const CONFIG = {
    SCAN_MS: window.CCRS?.CONFIG?.SCAN_MS ?? 10_000,
    MIN_TAG_LEN: window.CCRS?.CONFIG?.MIN_TAG_LEN ?? 7,
    SSE_URL: window.CCRS?.CONFIG?.SSE_URL ?? '/sensors/stream',
    POLL_URL: window.CCRS?.CONFIG?.POLL_URL ?? '/sensors/peek',
    POLL_INTERVAL: window.CCRS?.CONFIG?.POLL_INTERVAL ?? 200,
  };

  // ----- State -----
  const ROSTER_BY_ID = new Map();  // id -> normalized row
  let ALL = [];                   // normalized rows
  let dbReady = false;
  let selectedId = null;
  let filterMode = 'all';         // all | enabled | disabled
  let sortKey = 'number';
  let sortDir = 1;

  // scan state
  let scanCancel = null;
  let scanTimer = null;
  let scanBarTimer = null;

  // ----- Utilities -----
  const digitsOnly = (s) => (s ?? '').toString().replace(/\D+/g, '');
  const clamp = (s, max) => (s ?? '').toString().trim().slice(0, max);
  const isValidNum = (s) => /^\d+$/.test(s) && Number(s) > 0;
  const isValidTeam = (s) => s.length >= 2 && s.length <= 40;
  const isValidTag = (s) => s === '' || /^\d+$/.test(s);

  function normalizeEntrant(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const id = raw.id ?? raw.entrant_id ?? null;
    return {
      id,
      number: String(raw.number ?? ''),
      name: raw.name ?? '',
      tag: raw.tag ?? null,
      enabled: Boolean(raw.enabled ?? 1),
      status: raw.status ?? 'ACTIVE',
      organization: raw.organization ?? '',
      spoken_name: raw.spoken_name ?? '',
      color: raw.color ?? null,
      updated_at: raw.updated_at ?? null,
      logo: raw.logo ?? null,
    };
  }

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
    if (hex.length === 6) return `#${hex}`.toUpperCase();
    return null;
  }
  function isStrictHex6(v) {
    return typeof v === 'string' && /^#[0-9A-F]{6}$/.test(v);
  }

  function setDNone(el, hide) {
    if (!el) return;
    el.classList.toggle('d-none', !!hide);
  }

  function setAlert(containerEl, text, kind) {
    if (!containerEl) return;
    if (!text) {
      containerEl.innerHTML = '';
      return;
    }
    const cls =
      kind === 'ok' ? 'alert-success' :
        kind === 'warn' ? 'alert-warning' :
          kind === 'info' ? 'alert-info' :
            'alert-danger';
    containerEl.innerHTML = `<div class="alert ${cls} py-2 mb-0" role="alert">${escapeHtml(text)}</div>`;
  }

  function escapeHtml(s) {
    return String(s ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function setReadyUI(ready) {
    dbReady = !!ready;
    setDNone(els.readyWarn, ready); // show warning when NOT ready
  }

  function findById(id) {
    return ALL.find((r) => r.id === id) || null;
  }
  function findByTag(tag) {
    return ALL.find((r) => (r.tag ?? '') === tag) || null;
  }

  // ----- Table host (Bootstrap) -----
  function ensureTableScaffold() {
    const tbody = document.getElementById('rows');
    if (!tbody) return null;
    return { tbody };
  }


  function highlightSelection() {
    const tbody = els.rowsRoot?.querySelector('tbody[data-ccrs="entrants-body"]');
    if (!tbody) return;

    tbody.querySelectorAll('tr[data-id]').forEach((tr) => {
      const id = Number(tr.dataset.id);
      tr.classList.toggle('table-active', selectedId != null && id === selectedId);
    });
  }

  // ----- Networking -----
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

      ROSTER_BY_ID.clear();
      const normalized = [];
      for (const raw of data) {
        const n = normalizeEntrant(raw);
        if (n && n.id != null) {
          ROSTER_BY_ID.set(n.id, n);
          normalized.push(n);
        }
      }
      ALL = normalized;
      render();
      setNetStatus(true, `OK - ${ALL.length} entrants`);
      highlightSelection();
    } catch (err) {
      console.error('[Entrants] loadEntrants failed:', err?.message || err);
      setNetStatus(false, 'Failed to load entrants');
    }
  }

  async function saveEntrant(one) {
    try {
      const res = await postJSON('/admin/entrants', { entrants: [one] });

      if (res.status === 409) {
        setAlert(els.formMsg, 'That tag is already assigned to another enabled entrant.', 'error');
        return { ok: false, code: 409 };
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => '');
        setAlert(els.formMsg, txt || `Unexpected error (${res.status}).`, 'error');
        return { ok: false, code: res.status };
      }

      let body = null;
      try { body = await res.json(); } catch (_) { }
      await loadEntrants();
      setAlert(els.formMsg, 'Entrant saved.', 'ok');
      setNetStatus(true, 'Entrant saved');
      return { ok: true, code: 200, body };
    } catch (err) {
      setAlert(els.formMsg, err?.message || 'Save failed; check connection/logs.', 'error');
      setNetStatus(false, 'Save failed');
      return { ok: false, code: 0 };
    }
  }

  async function getEntrantInUse(id) {
    try {
      return await fetchJSON(`/admin/entrants/${id}/inuse?ts=${Date.now()}`, { cache: 'no-store' });
    } catch (e) {
      return { id, counts: { passes: 0, lap_events: 0 } };
    }
  }

  async function deleteEntrant(id) {
    try {
      const res = await postJSON('/admin/entrants/delete', { ids: [id] });
      if (!res.ok) {
        const txt = await res.text().catch(() => '');
        return { ok: false, code: res.status, body: txt };
      }
      return { ok: true, code: 200 };
    } catch (err) {
      return { ok: false, code: 0 };
    }
  }

  // ----- Render -----
  function filteredSortedRows() {
    const q = (els.q?.value || '').trim().toLowerCase();

    const filt = (row) => {
      if (filterMode === 'enabled' && !row.enabled) return false;
      if (filterMode === 'disabled' && row.enabled) return false;
      if (!q) return true;

      const hay = [row.number, row.name, row.tag ?? ''].join(' ').toLowerCase();
      return hay.includes(q);
    };

    const cmp = (a, b) => {
      const k = sortKey;
      const av = (a[k] ?? '').toString();
      const bv = (b[k] ?? '').toString();
      if (k === 'number' || k === 'id') return (Number(av) - Number(bv)) * sortDir;
      return av.localeCompare(bv) * sortDir;
    };

    return ALL.filter(filt).sort(cmp);
  }

  function render() {
    const scaffold = ensureTableScaffold();
    if (!scaffold) return;

    const { tbody } = scaffold;
    const rows = filteredSortedRows();

    tbody.innerHTML = rows.map((r) => {
      const enabledBadge = r.enabled
        ? '<span class="badge text-bg-success">Yes</span>'
        : '<span class="badge text-bg-secondary">No</span>';

      const tag = r.tag
        ? `<span class="font-monospace">${escapeHtml(r.tag)}</span>`
        : '<span class="text-body-secondary">-</span>';

      // Use truncation without custom CSS: Bootstrap's text-truncate needs a block element
      // In table-layout:fixed, it will truncate naturally inside the fixed cell.
      const team = `<div class="text-truncate" title="${escapeHtml(r.name)}">${escapeHtml(clamp(r.name, 64))}</div>`;

      return `
      <tr data-id="${Number(r.id)}">
        <td>${enabledBadge}</td>
        <td>${tag}</td>
        <td><span class="font-monospace">${escapeHtml(r.number ?? '')}</span></td>
        <td>${team}</td>
        <td>
          <div class="btn-group btn-group-sm" role="group">
            <button type="button" class="btn btn-outline-light" data-act="edit">Edit</button>
            <button type="button" class="btn btn-outline-info" data-act="tag">Set Tag</button>
            <button type="button" class="btn btn-outline-danger" data-act="delete">Delete</button>
          </div>
        </td>
        <td class="text-body-secondary">${escapeHtml(r.status ?? 'ACTIVE')}</td>
      </tr>
    `;
    }).join('');

    highlightSelection();
  }


  // ----- Form hydrate / clear -----
  function clearForm() {
    selectedId = null;
    if (els.entId) els.entId.value = '';
    if (els.entNumber) els.entNumber.value = '';
    if (els.entName) els.entName.value = '';
    if (els.entTag) els.entTag.value = '';
    if (els.entEnabled) els.entEnabled.checked = true;

    if (els.entOrg) els.entOrg.value = '';
    if (els.entSpoken) els.entSpoken.value = '';
    if (els.entStatus) els.entStatus.value = 'ACTIVE';

    if (els.entColor) els.entColor.value = '';
    if (els.entColorPicker) els.entColorPicker.value = '#22C55E';

    if (els.entUpdated) els.entUpdated.value = '';
    if (els.entUpdatedAt) els.entUpdatedAt.textContent = '-';

    setAlert(els.formMsg, '', 'info');
    highlightSelection();
  }

  function fillForm(row) {
    const r = row && typeof row === 'object' ? row : {};
    selectedId = r.id ?? null;

    if (els.entId) els.entId.value = r.id ?? '';
    if (els.entNumber) els.entNumber.value = r.number ?? '';
    if (els.entName) els.entName.value = r.name ?? '';
    if (els.entTag) els.entTag.value = r.tag ?? '';
    if (els.entEnabled) els.entEnabled.checked = !!r.enabled;

    if (els.entOrg) els.entOrg.value = r.organization ?? '';
    if (els.entSpoken) els.entSpoken.value = r.spoken_name ?? '';
    if (els.entStatus) els.entStatus.value = r.status || 'ACTIVE';

    const cNorm = normalizeHexColor(r.color);
    if (els.entColor) els.entColor.value = cNorm || '';
    if (els.entColorPicker) els.entColorPicker.value = cNorm || '#22C55E';

    if (r.updated_at) {
      const d = new Date(r.updated_at * 1000);
      if (els.entUpdated) els.entUpdated.value = d.toLocaleString();
      if (els.entUpdatedAt) els.entUpdatedAt.textContent = d.toLocaleString();
    } else {
      if (els.entUpdated) els.entUpdated.value = '';
      if (els.entUpdatedAt) els.entUpdatedAt.textContent = '-';
    }

    setAlert(els.formMsg, '', 'info');
    highlightSelection();
  }

  function validateForm(show) {
    const num = (els.entNumber?.value || '').trim();
    const name = (els.entName?.value || '').trim();
    const tag = (els.entTag?.value || '').trim();

    let msg = '';
    if (!isValidNum(num)) msg = 'Number must be a positive integer.';
    else if (!isValidTeam(name)) msg = 'Team must be 2–40 characters.';
    else if (!isValidTag(tag)) msg = 'Tag must be digits only (or blank).';

    if (show) setAlert(els.formMsg, msg, msg ? 'error' : 'ok');
    return !msg;
  }

  function formToPayload() {
    const id = els.entId?.value ? Number(els.entId.value) : null;
    const numberStr = (els.entNumber?.value || '').trim();
    const name = String(els.entName?.value || '').slice(0, 40);

    const tagRaw = (els.entTag?.value || '').trim();
    const tag = tagRaw === '' ? null : digitsOnly(tagRaw);

    const enabled = !!els.entEnabled?.checked;

    const organization = (els.entOrg?.value || '').trim();
    const spoken_name = (els.entSpoken?.value || '').trim();
    const status = els.entStatus?.value || 'ACTIVE';

    const c = normalizeHexColor(els.entColor?.value || '');
    const color = (c && isStrictHex6(c)) ? c : null;

    return { id, number: Number(numberStr), name, tag, enabled, status, organization, spoken_name, color };
  }

  // ----- Conflict -----
  function handleConflict(tagDigits) {
    setAlert(els.formMsg, 'Tag already assigned to another enabled entrant.', 'error');
    const r = findByTag(tagDigits);
    if (r) {
      selectedId = r.id;
      fillForm(r);
    }
    if (els.entTag) {
      els.entTag.value = tagDigits;
      els.entTag.focus();
    }
    setNetStatus(false, '409 - duplicate tag');
  }

  // ----- Quick-create -----
  function openQuickCreate(tagDigits) {
    if (!els.quickCreateModal) return;
    els.qcTag.value = tagDigits;
    els.qcName.value = `Unknown ${tagDigits}`;
    els.qcNumber.value = '';
    els.qcEnabled.checked = true;
    setAlert(els.qcMsg, '', 'info');
    els.quickCreateModal.showModal();
    setTimeout(() => els.qcNumber.focus(), 30);
  }

  async function createFromQuick() {
    const num = (els.qcNumber.value || '').trim();
    const name = (els.qcName.value || '').trim();
    const tag = (els.qcTag.value || '').trim();
    const enabled = !!els.qcEnabled.checked;

    if (!isValidNum(num)) { setAlert(els.qcMsg, 'Number must be a positive integer.', 'error'); return; }
    if (!isValidTeam(name)) { setAlert(els.qcMsg, 'Team must be 2–40 characters.', 'error'); return; }

    const payload = {
      id: null,
      number: Number(num),
      name,
      tag: tag ? digitsOnly(tag) : null,
      enabled,
      color: null,
      organization: '',
      spoken_name: '',
      status: 'ACTIVE',
    };

    const { ok, code } = await saveEntrant(payload);
    if (!ok) {
      if (code === 409) { els.quickCreateModal.close(); handleConflict(digitsOnly(tag)); return; }
      setAlert(els.qcMsg, 'Save failed - check connection/logs.', 'error');
      return;
    }

    els.quickCreateModal.close();
    if (els.entTag) {
      els.entTag.value = digitsOnly(tag);
      els.entTag.focus();
    }

    const created = findByTag(digitsOnly(tag));
    if (created) fillForm(created);
  }

  // ----- Scan adapter (SSE preferred, polling fallback) -----
  async function startScanSession(onTag) {
    // Preflight SSE
    let sseOk = false;
    try {
      const head = await fetch(CONFIG.SSE_URL, {
        method: 'GET',
        headers: { 'Accept': 'text/event-stream' },
        cache: 'no-store',
      });
      sseOk = head.ok && (head.headers.get('content-type') || '').includes('text/event-stream');
    } catch { sseOk = false; }

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
            const tag = String(data.tag || '').trim();
            if (!/^\d+$/.test(tag) || tag.length < CONFIG.MIN_TAG_LEN) return;
            if (seen.has(tag)) return;
            seen.add(tag);
            onTag(tag);
            close();
          } catch { }
        });

        es.onerror = () => close();
        return () => close();
      } catch {
        // fall through
      }
    }

    // Poll fallback
    let active = true;
    let lastSeenAt = null;
    let pollTimer = null;

    const tick = async () => {
      if (!active) return;
      try {
        const r = await fetch(CONFIG.POLL_URL, { cache: 'no-store' });
        if (!r.ok) throw new Error(`poll ${r.status}`);
        const js = await r.json();
        const tag = js?.tag ? String(js.tag).trim() : '';
        const seen_at = js?.seen_at ?? null;

        if (tag && /^\d+$/.test(tag) && tag.length >= CONFIG.MIN_TAG_LEN) {
          if (!lastSeenAt || !seen_at || seen_at !== lastSeenAt) {
            lastSeenAt = seen_at || `t${Date.now()}`;
            onTag(tag);
            stop();
            return;
          }
        }
      } catch { }
      if (active) pollTimer = setTimeout(tick, CONFIG.POLL_INTERVAL);
    };

    const stop = () => {
      active = false;
      if (pollTimer) clearTimeout(pollTimer);
    };

    pollTimer = setTimeout(tick, 0);
    return () => stop();
  }

  function setScanUI(state, tagText) {
    if (!els.entTag) return;

    if (state === 'scanning') {
      els.entTag.readOnly = true;
      els.scanBtn.disabled = true;
      els.stopBtn.disabled = false;
      els.assignBtn.disabled = true;
      if (els.scanBar) els.scanBar.style.width = '0%';
      if (els.scanMsg) els.scanMsg.textContent = 'Listening for tag…';
      return;
    }

    if (state === 'captured') {
      els.entTag.readOnly = false;
      els.scanBtn.disabled = false;
      els.stopBtn.disabled = true;
      els.assignBtn.disabled = false;
      if (els.scanMsg) els.scanMsg.textContent = tagText ? `Captured ${tagText}` : 'Captured';
      if (els.scanBar) els.scanBar.style.width = '0%';
      return;
    }

    if (state === 'timeout' || state === 'error') {
      els.entTag.readOnly = false;
      els.scanBtn.disabled = false;
      els.stopBtn.disabled = true;
      els.assignBtn.disabled = !String(els.entTag.value || '').trim();
      if (els.scanMsg) els.scanMsg.textContent = (state === 'timeout') ? 'Scan timed out' : 'Scanner offline';
      if (els.scanBar) els.scanBar.style.width = '0%';
      return;
    }

    // idle
    els.entTag.readOnly = false;
    els.scanBtn.disabled = false;
    els.stopBtn.disabled = true;
    els.assignBtn.disabled = !String(els.entTag.value || '').trim();
    if (els.scanMsg) els.scanMsg.textContent = '';
    if (els.scanBar) els.scanBar.style.width = '0%';
  }

  function startProgressBar(ms) {
    if (!els.scanBar) return;
    const start = Date.now();
    clearInterval(scanBarTimer);
    scanBarTimer = setInterval(() => {
      const pct = Math.max(0, Math.min(100, ((Date.now() - start) / ms) * 100));
      els.scanBar.style.width = pct.toFixed(1) + '%';
    }, 100);
  }
  function stopProgressBar() {
    clearInterval(scanBarTimer);
    scanBarTimer = null;
  }

  async function onScanStart() {
    if (!dbReady) { setNetStatus(false, 'DB not ready'); return; }

    setScanUI('scanning');
    startProgressBar(CONFIG.SCAN_MS);

    scanCancel = await startScanSession((tag) => onTagCaptured(tag));

    clearTimeout(scanTimer);
    scanTimer = setTimeout(() => onScanTimeout(), CONFIG.SCAN_MS);
  }

  function onScanTimeout() {
    try { scanCancel && scanCancel(); } catch { }
    scanCancel = null;
    stopProgressBar();
    setScanUI('timeout');
  }

  function onScanStop() {
    try { scanCancel && scanCancel(); } catch { }
    scanCancel = null;
    clearTimeout(scanTimer);
    stopProgressBar();
    setScanUI('idle');
  }

  function onTagCaptured(tag) {
    clearTimeout(scanTimer);
    stopProgressBar();
    try { scanCancel && scanCancel(); } catch { }
    scanCancel = null;

    els.entTag.value = tag;
    setScanUI('captured', tag);

    const existing = findByTag(tag);
    if (existing) {
      fillForm(existing);
      setNetStatus(true, `Tag already on ${existing.name} (#${existing.number})`);
    } else {
      els.entNumber?.focus();
    }
  }

  async function onAssign() {
    const tag = String(els.entTag?.value || '').trim();
    if (!/^\d+$/.test(tag) || tag.length < CONFIG.MIN_TAG_LEN) {
      setNetStatus(false, 'Tag must be 7+ digits');
      return;
    }

    // If selected: assign to selected entrant
    if (selectedId != null) {
      const full = ROSTER_BY_ID.get(selectedId) || findById(selectedId);
      if (!full) { setNetStatus(false, 'Selection lost - refresh'); return; }

      const body = {
        id: full.id,
        number: Number(full.number || 0),
        name: full.name,
        tag,
        enabled: !!full.enabled,
        status: full.status ?? 'ACTIVE',
        organization: full.organization ?? '',
        spoken_name: full.spoken_name ?? '',
        color: normalizeHexColor(full.color) || null,
      };

      const { ok, code } = await saveEntrant(body);
      if (!ok && code === 409) { handleConflict(tag); return; }
      if (ok) setNetStatus(true, 'Tag assigned');
      return;
    }

    // No selection: if tag belongs to existing, load it
    const existing = findByTag(tag);
    if (existing) {
      fillForm(existing);
      setNetStatus(true, `Loaded ${existing.name} (#${existing.number})`);
      return;
    }

    // Otherwise: quick-create
    openQuickCreate(tag);
  }

  // ----- Events -----
  function wireEvents() {
    // Filter buttons
    els.filterBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        els.filterBtns.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        filterMode = btn.dataset.filter || 'all';
        render();
      });
    });

    // Search
    els.q?.addEventListener('input', render);

    // Basic sort via header clicks (optional): if you later want clickable <th>, wire here.

    // Form buttons
    els.resetBtn?.addEventListener('click', clearForm);
    els.newBtn?.addEventListener('click', clearForm);

    els.saveBtn?.addEventListener('click', async () => {
      if (!validateForm(true)) return;
      if (!dbReady) { setAlert(els.formMsg, 'DB is not ready; please wait.', 'error'); return; }

      const payload = formToPayload();
      const { ok } = await saveEntrant(payload);
      if (ok) clearForm();
    });

    // Guards
    els.entNumber?.addEventListener('input', () => {
      els.entNumber.value = digitsOnly(els.entNumber.value);
    });
    els.entTag?.addEventListener('input', () => {
      const v = digitsOnly(els.entTag.value);
      if (els.entTag.value !== v) els.entTag.value = v;
      if (!scanCancel) els.assignBtn.disabled = !v.trim();
    });

    // Color sync
    if (els.entColor && els.entColorPicker) {
      els.entColor.addEventListener('input', () => {
        const norm = normalizeHexColor(els.entColor.value);
        if (norm && isStrictHex6(norm)) {
          els.entColorPicker.value = norm;
          if (els.entColor.value !== norm) els.entColor.value = norm;
          els.entColor.classList.remove('is-invalid');
        } else {
          // Bootstrap-native error state (no custom CSS)
          els.entColor.classList.add('is-invalid');
        }
      });

      els.entColorPicker.addEventListener('input', () => {
        const v = String(els.entColorPicker.value || '').toUpperCase();
        if (isStrictHex6(v)) {
          els.entColor.value = v;
          els.entColor.classList.remove('is-invalid');
        }
      });
    }

    // Refresh / export
    els.refreshBtn?.addEventListener('click', loadEntrants);
    els.exportBtn?.addEventListener('click', () => {
      window.location.href = `/admin/entrants/export.csv?_=${Date.now()}`;
    });

    // Scan controls
    els.scanBtn?.addEventListener('click', onScanStart);
    els.stopBtn?.addEventListener('click', onScanStop);
    els.assignBtn?.addEventListener('click', onAssign);

    // Quick-create
    els.qcCreate?.addEventListener('click', (e) => { e.preventDefault(); createFromQuick(); });
    els.qcCancel?.addEventListener('click', () => els.quickCreateModal?.close());
    els.quickCreateModal?.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); createFromQuick(); }
    });

    // Delegated table actions
    els.rowsRoot?.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;

      const tr = btn.closest('tr[data-id]');
      const id = tr ? Number(tr.dataset.id) : null;
      if (!id) return;

      const act = btn.dataset.act;
      const full = ROSTER_BY_ID.get(id) || findById(id);
      if (!full) return;

      if (act === 'edit') {
        fillForm(full);
        return;
      }

      if (act === 'tag') {
        const current = String(full.tag ?? '');
        const val = prompt(`Set tag for “${full.name}” (digits only; blank = clear):`, current);
        if (val === null) return;

        const clean = digitsOnly(val.trim());
        if (val.trim() !== '' && clean === '') { setNetStatus(false, 'Tag must be digits only'); return; }

        const body = {
          id: full.id,
          number: Number(full.number || 0),
          name: full.name,
          tag: val.trim() === '' ? null : clean,
          enabled: !!full.enabled,
          status: full.status ?? 'ACTIVE',
          organization: full.organization ?? '',
          spoken_name: full.spoken_name ?? '',
          color: normalizeHexColor(full.color) || null,
        };

        const { ok, code } = await saveEntrant(body);
        if (!ok && code === 409) handleConflict(clean);
        if (ok) {
          selectedId = id;
          highlightSelection();
        }
        return;
      }

      if (act === 'delete') {
        const teamName = String(full.name ?? '').trim();
        const label = `${full.number ?? '-'} · ${teamName}`;

        let needsTypedConfirm = false;
        let counts = { passes: 0, lap_events: 0 };
        try {
          const info = await getEntrantInUse(id);
          counts = info?.counts || counts;
          needsTypedConfirm = ((counts.passes || 0) + (counts.lap_events || 0)) > 0;
        } catch { }

        if (!needsTypedConfirm) {
          const ok = confirm(`Delete entrant permanently?\n\n${label}\n\nThis cannot be undone.`);
          if (!ok) return;
        } else {
          const typed = prompt(
            `This entrant has recorded data (passes: ${counts.passes}, laps: ${counts.lap_events}).\n` +
            `To confirm hard delete, type the Team name exactly:\n\n${teamName}\n`
          );
          if (typed == null) return;
          if (String(typed).trim().toLowerCase() !== teamName.toLowerCase()) {
            setAlert(els.formMsg, 'Delete cancelled - team name did not match.', 'error');
            setNetStatus(false, 'Delete cancelled');
            return;
          }
        }

        const res = await deleteEntrant(id);
        if (!res.ok) {
          const msg = (res.code === 404) ? 'Entrant not found (already deleted?)' : 'Delete failed.';
          setAlert(els.formMsg, msg, 'error');
          setNetStatus(false, msg);
          return;
        }

        // Update local state and UI
        ROSTER_BY_ID.delete(id);
        ALL = ALL.filter((r) => r.id !== id);
        if (selectedId === id) clearForm();
        render();

        setAlert(els.formMsg, 'Entrant deleted.', 'ok');
        setNetStatus(true, 'Entrant deleted');
      }
    });

    // Row click selects (nice UX)
    els.rowsRoot?.addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-id]');
      if (!tr) return;
      // ignore action button clicks (handled above)
      if (e.target.closest('button[data-act]')) return;
      selectedId = Number(tr.dataset.id);
      highlightSelection();
    });
  }

  // ----- Boot -----
  (function start() {
    setNetStatus(true, 'Connecting…');
    setReadyUI(false);

    const readyPoll = makePoller(pollReady, 2500, () => setReadyUI(false));
    readyPoll.start();
    pollReady().catch(() => setReadyUI(false));

    wireEvents();
    loadEntrants();

    // initial scan UI
    setScanUI('idle');
  })();

  // Optional console hook
  window.CCRS.scanTest = onTagCaptured;
})();
