(function () {
  function onReady(fn){ if(document.readyState!=="loading") fn(); else document.addEventListener("DOMContentLoaded",fn); }
  function withPRS(cb, timeoutMs=6000){
    if (window.PRS) return cb(window.PRS);
    const t0 = Date.now(), iv = setInterval(()=>{
      if (window.PRS){ clearInterval(iv); cb(window.PRS); }
      else if (Date.now()-t0>timeoutMs){ clearInterval(iv); console.error("PRS not available"); }
    }, 20);
  }

  onReady(() => withPRS(({ $, fetchJSON, makePoller, setNetStatus }) => {
    // Elements
    const uidEl      = $("#uid");
    const teamEl     = $("#team");
    const orgEl      = $("#org");
    const carEl      = $("#car");
    const colorEl    = $("#color");
    const colorText  = $("#colorText");
    const enabledEl  = $("#enabled");

    const msgEl      = $("#formMsg");
    const rowsEl     = $("#rows");
    const scanBtn    = $("#scanBtn");
    const scanWrap   = $("#scanWrap");
    const scanBar    = $(".scanbar-fill");

    // keep footer status updating
    const netOk = (n) => setNetStatus(true, `OK — ${n} entrants`);

    // color two-way binding
    colorEl.addEventListener("input", ()=>{ colorText.value = colorEl.value.toUpperCase(); });
    colorText.addEventListener("input", ()=>{
      const v = colorText.value.trim();
      if (/^#?[0-9A-Fa-f]{6}$/.test(v)) colorEl.value = v.startsWith("#")? v : ("#"+v);
    });

    // State
    let entrants = [];   // { id, uid, team, org, car_num, color, enabled }
    let sortKey = "team";
    let sortAsc = true;
    let scanning = false;
    let scanTimer = null;

    // Utils
    const clean = (s) => (s ?? "").trim();
    const toInt = (v) => { const n = parseInt(v,10); return Number.isFinite(n)? n : null; };
    const setFormStatus = (ok, text) => { msgEl.textContent = text||""; msgEl.style.color = ok ? "var(--ok)" : "var(--error)"; };
    const fillForm = (e={})=>{
      uidEl.value     = e.uid ?? "";
      teamEl.value    = e.team ?? "";
      orgEl.value     = e.org ?? "";
      carEl.value     = (e.car_num ?? "") === null ? "" : (e.car_num ?? "");
      colorEl.value   = e.color ?? "#22A6F2";
      colorText.value = (e.color ?? "#22A6F2").toUpperCase();
      enabledEl.checked = Boolean(e.enabled ?? true);
    };
    const getForm = ()=>({
      uid: clean(uidEl.value) === "" ? null : clean(uidEl.value),
      team: clean(teamEl.value) || null,
      org: clean(orgEl.value) || null,
      car_num: clean(carEl.value)==="" ? null : toInt(carEl.value),
      color: clean(colorText.value) || null,
      enabled: enabledEl.checked ? 1 : 0,
      id: currentId()
    });

    let _currentId = null;
    const currentId = ()=> _currentId;
    const setCurrentId = (id)=>{ _currentId = id; };

    // Render table with 36 rows minimum
    function renderTable(){
      rowsEl.innerHTML = "";
      const sorted = entrants.slice().sort((a,b)=>{
        const key = sortKey;
        const av = (a[key]??"").toString().toLowerCase();
        const bv = (b[key]??"").toString().toLowerCase();
        if (av < bv) return sortAsc? -1 : 1;
        if (av > bv) return sortAsc? 1 : -1;
        return 0;
      });
      const rows = sorted.map(e => rowHTML(e));
      // pad to 36
      for (let i = rows.length; i < 36; i++){
        rows.push(emptyRowHTML());
      }
      rowsEl.innerHTML = rows.join("");
    }

    function rowHTML(e){
      const colorChip = e.color ? `<span class="chip" style="display:inline-block;width:18px;height:18px;border-radius:4px;border:1px solid #13202a;background:${e.color}"></span>` : "";
      const disabledCls = e.enabled ? "" : " disabled";
      return `<div class="ent-row row${disabledCls}" data-id="${e.id}">
        <div>${e.uid ?? ""}</div>
        <div>${e.org ?? ""}</div>
        <div>${e.team ?? ""}</div>
        <div>${e.car_num ?? ""}</div>
        <div>${colorChip}</div>
        <div>${e.enabled ? "Yes" : "No"}</div>
        <div class="right">
          <span class="link" data-act="edit" data-id="${e.id}">Edit</span>
          &nbsp;·&nbsp;
          <span class="link" data-act="delete" data-id="${e.id}">Delete</span>
        </div>
      </div>`;
    }
    function emptyRowHTML(){
      return `<div class="ent-row row">
        <div></div><div></div><div></div><div></div><div></div><div></div><div></div>
      </div>`;
    }

    async function loadEntrants(){
      try{
        const res = await fetchJSON("/admin/entrants");
        entrants = Array.isArray(res?.items) ? res.items : [];
        renderTable();
        netOk(entrants.length);
      }catch{
        setNetStatus(false, "Disconnected — retrying…");
      }
    }

    // Sorting clicks
    document.querySelectorAll("[data-sort]").forEach(btn=>{
      btn.addEventListener("click", ()=>{
        const k = btn.getAttribute("data-sort");
        if (k === sortKey) sortAsc = !sortAsc;
        else { sortKey = k; sortAsc = (k==="team"); }
        renderTable();
      });
    });

    // Row actions
    rowsEl.addEventListener("click", async (ev)=>{
      const link = ev.target.closest("[data-act]");
      const row  = ev.target.closest(".ent-row");
      if (!row) return;
      const id = parseInt(link?.dataset.id || row.dataset.id || "0", 10);
      if (!id) return;

      const e = entrants.find(x => x.id === id);
      if (!e) return;

      if (!link || link.dataset.act === "edit"){
        setCurrentId(e.id);
        fillForm(e);
        setFormStatus(true, "Loaded.");
        teamEl.focus();
        teamEl.select();
      } else if (link.dataset.act === "delete"){
        if (!confirm(`Delete '${e.team ?? e.org ?? e.uid ?? "entrant"}'?`)) return;
        try{
          const r = await fetch(`/admin/entrants/${id}`, { method:"DELETE" });
          if (!r.ok) throw new Error(await r.text());
          await loadEntrants();
          fillForm({});
          setCurrentId(null);
          setFormStatus(true, "Deleted.");
        }catch{
          setFormStatus(false, "Delete failed.");
        }
      }
    });

    // Save / Update (no required fields, but enforce uniqueness when provided)
    $("#saveBtn").addEventListener("click", async ()=>{
      const data = getForm();

      // Uniqueness checks (case-insensitive for team)
      const teamClash = data.team && entrants.some(e =>
        e.team?.toLowerCase() === data.team.toLowerCase() && e.id !== data.id);
      if (teamClash) return setFormStatus(false, "Team name must be unique.");

      const carClash = data.car_num != null && entrants.some(e =>
        e.car_num === data.car_num && e.id !== data.id);
      if (carClash) return setFormStatus(false, "Car # must be unique.");

      const uidClash = data.uid && entrants.some(e =>
        e.uid === data.uid && e.id !== data.id);
      if (uidClash) return setFormStatus(false, "UID must be unique.");

      try{
        const r = await fetch("/admin/entrants", {
          method:"POST",
          headers:{ "Content-Type":"application/json" },
          body: JSON.stringify(data)
        });
        if (!r.ok) throw new Error(await r.text());
        const saved = await r.json();
        setCurrentId(saved.id);
        await loadEntrants();
        setFormStatus(true, "Saved.");
      }catch(e){
        setFormStatus(false, "Save failed.");
      }
    });

    $("#clearBtn").addEventListener("click", ()=>{
      fillForm({});
      setCurrentId(null);
      setFormStatus(true, "Cleared.");
      uidEl.focus();
    });

    $("#deleteBtn").addEventListener("click", async ()=>{
      const id = currentId();
      if (!id){ setFormStatus(false, "Nothing to delete."); return; }
      if (!confirm("Delete this entrant?")) return;
      try{
        const r = await fetch(`/admin/entrants/${id}`, { method:"DELETE" });
        if (!r.ok) throw new Error(await r.text());
        await loadEntrants();
        fillForm({}); setCurrentId(null);
        setFormStatus(true, "Deleted.");
      }catch{
        setFormStatus(false, "Delete failed.");
      }
    });

    // --- Scanner: 10s bar, capture focus to Team, capture blip ---
    function stopScan(){
      scanning = false;
      scanBtn.textContent = "Scan";
      scanWrap.hidden = true;
      if (scanTimer) { clearInterval(scanTimer); scanTimer = null; }
    }
    async function scanTick(startMs, baseline){
      const elapsed = Date.now() - startMs;
      scanBar.style.width = `${Math.min(100, (elapsed/10000)*100)}%`;
      if (elapsed >= 10000){ stopScan(); setFormStatus(false, "No new tag detected."); return; }

      try{
        const data = await fetchJSON("/laps?limit=10");
        const items = data?.items || data?.rows || data || [];
        for (const it of items){
          const tag = String(it.tag_id ?? it.tag ?? "");
          if (!tag) continue;
          if (tag === (uidEl.value||"")) continue;
          if (!baseline.has(tag)){
            // found new tag
            const existing = entrants.find(e => e.uid === tag);
            if (existing){
              setCurrentId(existing.id);
              fillForm(existing);
            }else{
              setCurrentId(null);
              uidEl.value = tag;
              uidEl.classList.add("captured");
              setTimeout(()=> uidEl.classList.remove("captured"), 500);
            }
            stopScan();
            setFormStatus(true, `Captured UID ${tag}.`);
            teamEl.focus(); teamEl.select();
            return;
          }
        }
      }catch{
        setNetStatus(false, "Scanner: API error.");
      }
    }

    scanBtn.addEventListener("click", ()=>{
      if (scanning){ stopScan(); return; }
      scanning = true;
      scanBtn.textContent = "Stop";
      scanWrap.hidden = false;
      scanBar.style.width = "0%";
      setFormStatus(true, "Scanning for I-Lap passes…");

      (async ()=>{
        const baseline = new Set();
        try{
          const data = await fetchJSON("/laps?limit=10");
          const items = data?.items || data?.rows || data || [];
          for (const it of items){
            const tag = String(it.tag_id ?? it.tag ?? "");
            if (tag) baseline.add(tag);
          }
        }catch{}
        const start = Date.now();
        scanTimer = setInterval(()=> scanTick(start, baseline), 300);
      })();
    });

    // Initial load
    loadEntrants();
    // Keep the footer "connected" if we’re polling successfully
    makePoller(()=> netOk(entrants.length), 4000, ()=>{}).start();
  }));
})();
