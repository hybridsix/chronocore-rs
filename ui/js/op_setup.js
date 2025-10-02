// ui/js/op_setup.js
// =====================================================================
// Race Setup page wiring
// - Read event + race_modes YAML to populate selects
// - Allow operators to pull entrants from current /race/state or paste JSON
// - POST /engine/load to merge entrants
// - Quick buttons: POST /engine/flag {flag:"pre"|"green"}
// =====================================================================

(() => {
  // --- Pull shared helpers from PRS namespace (defined in base.js) ---
  const { startWallClock, setNetStatus } = window.PRS;

  // --- Footer wall clock (real-world time, not race time) ---
  startWallClock("#wallClock");

  // --- Engine + Config endpoints (adjust paths here if needed) ---
  const EP = {
    state: "/race/state",
    load: "/engine/load",
    flag: "/engine/flag",
    yaml: {
      // Static YAML files served read-only by the web layer
      app:        "/config/app.yaml",
      config:     "/config/config.yaml",
      event:      "/config/event.yaml",
      race_modes: "/config/race_modes.yaml",
    },
  };

  /**
   * fetchYAML(url)
   * --------------
   * Fetch + parse a YAML document. Returns a JS object.
   * Throws on network/parsing errors to surface issues early.
   */
  async function fetchYAML(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${url}: ${r.status} ${r.statusText}`);
    const txt = await r.text();
    return jsyaml.load(txt) || {};
  }

  /**
   * renderParams(params)
   * --------------------
   * Render selected mode parameters as key/value rows for operator visibility.
   * We JSON-stringify nested objects for transparency.
   */
  function renderParams(params) {
    const kv = document.querySelector("#modeParams");
    kv.innerHTML = "";
    Object.entries(params || {}).forEach(([k, v]) => {
      const row = document.createElement("div");
      row.className = "kvrow";
      row.innerHTML = `
        <div class="k">${k}</div>
        <div class="v">${typeof v === "object" ? JSON.stringify(v) : String(v)}</div>
      `;
      kv.appendChild(row);
    });
  }

  /**
   * syncDerivedName()
   * -----------------
   * If the Race Name field is empty, derive a default from Event + Mode.
   */
  function syncDerivedName() {
    const ev = document.querySelector("#eventSelect").selectedOptions[0]?.textContent || "Event";
    const mode = document.querySelector("#modeSelect").selectedOptions[0]?.textContent || "Race";
    const rn = document.querySelector("#raceName");
    if (!rn.value) rn.value = `${ev} — ${mode}`;
  }

  /**
   * initYAML()
   * ----------
   * Load event + race_modes from YAML, populate selects, and render initial parameters.
   * Event select supports either:
   *   - event.yaml = { events: [ { name, date, location, ...}, ... ] }
   *   - event.yaml = { event: { name, date, location, ... } }  (single)
   */
  async function initYAML() {
    try {
      const [eventY, modesY] = await Promise.all([
        fetchYAML(EP.yaml.event),
        fetchYAML(EP.yaml.race_modes),
      ]);

      // --- Populate Events ---
      const evSel = document.querySelector("#eventSelect");
      const events =
        Array.isArray(eventY?.events) ? eventY.events : [eventY?.event].filter(Boolean);

      evSel.innerHTML = "";
      (events || []).forEach((ev) => {
        const opt = document.createElement("option");
        opt.value = ev.name || ev.id || ev.title || "";
        opt.textContent = ev.name || ev.title || opt.value || "Event";
        // Store common meta for autofill
        opt.dataset.date = ev.date || "";
        opt.dataset.loc = ev.location || ev.loc || "";
        evSel.appendChild(opt);
      });

      // Keep raceName, Date, Location in sync when event changes
      evSel.addEventListener("change", () => {
        const o = evSel.selectedOptions[0];
        document.querySelector("#raceName").value =
          `${o?.textContent || "Event"} — ${document.querySelector("#modeSelect").value || "Race"}`;
        document.querySelector("#raceDate").value = (o?.dataset.date || "").slice(0, 10);
        document.querySelector("#raceLoc").value = o?.dataset.loc || "";
      });

      // --- Populate Modes ---
      const modes = modesY?.modes || modesY; // support {modes:{...}} or a flat map
      const mSel = document.querySelector("#modeSelect");
      mSel.innerHTML = "";

      Object.keys(modes || {}).forEach((key) => {
        const m = modes[key];
        const opt = document.createElement("option");
        opt.value = key;
        opt.textContent = m.title || key;
        // Cache full params for the preview panel
        opt.dataset.params = JSON.stringify(m);
        mSel.appendChild(opt);
      });

      // When mode changes, refresh parameter preview and recompute default name
      mSel.addEventListener("change", () => {
        const o = mSel.selectedOptions[0];
        const p = o?.dataset.params ? JSON.parse(o.dataset.params) : {};
        renderParams(p);
        syncDerivedName();
      });

      // Trigger initial renders
      mSel.dispatchEvent(new Event("change"));
      evSel.dispatchEvent(new Event("change"));
    } catch (e) {
      console.error(e);
      setNetStatus(false, "YAML load failed");
    }
  }

  /**
   * pullEntrantsFromState()
   * -----------------------
   * Convenience helper for re-running heats:
   * - Reads /race/state
   * - Filters to enabled entrants
   * - Writes a minimal /engine/load payload into the textarea for editing
   */
  async function pullEntrantsFromState() {
    const r = await fetch(EP.state, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`/race/state failed: ${r.status} ${r.statusText}`);
    const s = await r.json();

    const entrants = (s.standings || [])
      .filter((e) => e.enabled !== false)
      .map((e) => ({
        entrant_id: e.entrant_id,
        car_number: e.car_number,
        name: e.name,
        tag: e.tag ?? e.tag_id,
        enabled: e.enabled !== false,
      }));

    const payload = { race_id: s.race_id || 1, entrants };
    document.querySelector("#entrantsJson").value = JSON.stringify(payload, null, 2);
    setNetStatus(true, "Entrants pulled from state");
  }

  /**
   * postFlag(flag)
   * --------------
   * Minimal wrapper to post a flag command to the engine.
   */
  async function postFlag(flag) {
    const r = await fetch(EP.flag, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flag }),
    });
    if (!r.ok) throw new Error(`Flag ${flag} failed: ${r.status} ${r.statusText}`);
    setNetStatus(true, `Flag: ${flag}`);
  }

  /**
   * loadEntrants()
   * --------------
   * Send the entrants payload to /engine/load.
   * The engine decides whether this is a merge or a replace.
   */
  async function loadEntrants() {
    const txt = document.querySelector("#entrantsJson").value.trim();
    if (!txt) throw new Error("No entrants JSON");

    const payload = JSON.parse(txt);
    const r = await fetch(EP.load, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(`Load failed: ${r.status} ${r.statusText}`);
    document.querySelector("#setupMsg").textContent = "Entrants loaded/merged.";
    setNetStatus(true, "Load OK");
  }

  // --- Wire UI events (buttons) ---
  document.querySelector("#btnPullFromState").addEventListener("click", async () => {
    try {
      await pullEntrantsFromState();
    } catch (e) {
      console.error(e);
      setNetStatus(false, "Failed to pull entrants");
    }
  });

  document.querySelector("#btnSampleEntrants").addEventListener("click", () => {
    document.querySelector("#entrantsJson").value = JSON.stringify(
      {
        race_id: 1,
        entrants: [
          { entrant_id: 1, car_number: 11, name: "Team A", tag: 300001, enabled: true },
          { entrant_id: 2, car_number: 7, name: "Blue Shell", tag: 300002, enabled: true },
        ],
      },
      null,
      2
    );
  });

  document.querySelector("#btnLoad").addEventListener("click", async () => {
    try {
      await loadEntrants();
    } catch (e) {
      console.error(e);
      document.querySelector("#setupMsg").textContent = "Load failed (check JSON).";
      setNetStatus(false, "Load failed");
    }
  });

  document.querySelector("#btnPreFlag").addEventListener("click", async () => {
    try {
      await postFlag("pre");
    } catch (e) {
      console.error(e);
      setNetStatus(false, "Flag failed");
    }
  });

  document.querySelector("#btnGreenFlag").addEventListener("click", async () => {
    try {
      await postFlag("green");
    } catch (e) {
      console.error(e);
      setNetStatus(false, "Flag failed");
    }
  });

  // --- Boot: fetch YAML and render selectors/params ---
  initYAML();
})();
