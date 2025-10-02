// ui/js/op_control.js
// =====================================================================
// Race Control page wiring
// - One-click flag buttons → POST /engine/flag
// - Live race clock + flag banner (poll /race/state)
// - Compact standings list (operator glance; spectator not required)
// =====================================================================

(() => {
  // --- Shortcuts & shared helpers from base.js (we own the stack; no fallbacks) ---
  const $ = (s) => document.querySelector(s);
  const { startWallClock, setNetStatus, makePoller, fmtClock } = window.PRS;

  // --- Footer wall clock (real-world time; not the race clock) ---
  startWallClock("#wallClock");

  // --- Engine endpoints used by this page ---
  const EP = { state: "/race/state", flag: "/engine/flag" };

  /**
   * postFlag(flag)
   * --------------
   * Fire-and-forget flag change to the engine.
   * Assumes the backend immediately updates state.flag and any timers as needed.
   */
  async function postFlag(flag) {
    const r = await fetch(EP.flag, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flag }),
    });
    if (!r.ok) throw new Error(`Flag post failed: ${r.status} ${r.statusText}`);
    setNetStatus(true, `Flag → ${flag}`);
  }

  // --- Wire up all .flagbtn buttons to POST /engine/flag with their data-flag value ---
  document.querySelectorAll(".flagbtn").forEach((btn) => {
    btn.addEventListener("click", () => postFlag(btn.dataset.flag));
  });

  /**
   * updateFlagBanner(flag)
   * ----------------------
   * Visualize the current flag state with shared CSS variants:
   *   .flag--pre, .flag--green, .flag--yellow, .flag--red, .flag--white, .flag--blue, .flag--checkered
   * When no flag is provided, hide the banner entirely.
   */
  function updateFlagBanner(flag) {
    const banner = $("#flagBanner");
    const label = $("#flagLabel");

    banner.className = "flag"; // reset base class (clears prior flag--* modifiers)
    if (!flag) {
      banner.classList.add("hidden");
      label.textContent = "";
      return;
    }

    banner.classList.remove("hidden");
    banner.classList.add(`flag--${flag}`);
    label.textContent = flag.toUpperCase();
  }

  /**
   * renderStandings(state)
   * ----------------------
   * Render a compact operator-focused table of the current standings.
   * We display: position, car number, team, current lap, last, best.
   * Note: we trust the engine's order in state.standings (authoritative).
   */
  function renderStandings(state) {
    const rowsEl = $("#rows");
    rowsEl.innerHTML = "";

    const rows = state.standings || [];
    rows.forEach((r, i) => {
      const el = document.createElement("div");
      el.className = "row compact";
      const last = r.last != null ? r.last.toFixed(3) : "—";
      const best = r.best != null ? r.best.toFixed(3) : "—";
      el.innerHTML = `
        <div>${i + 1}</div>
        <div class="car">${r.car_number ?? ""}</div>
        <div class="name">${r.name || ""}</div>
        <div class="right">${r.laps ?? 0}</div>
        <div class="right">${last}</div>
        <div class="right">${best}</div>
      `;
      rowsEl.appendChild(el);
    });
  }

  /**
   * Poller: /race/state every 1s
   * ----------------------------
   * On success:
   *  - Update #raceClock text via fmtClock(clock_ms)
   *  - Update flag banner
   *  - Render compact standings
   *  - Mark footer status OK
   * On failure:
   *  - Show disconnected status
   *  - Clear the race clock display
   */
  const poller = makePoller(
    async () => {
      const r = await fetch(EP.state, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(`/race/state failed: ${r.status} ${r.statusText}`);
      const s = await r.json();

      // Race clock pill (engine-owned, authoritative)
      $("#raceClock").textContent = s.clock_ms != null ? fmtClock(s.clock_ms) : "--:--";

      // Flag banner mirrors engine flag (supports "blue" as well)
      updateFlagBanner(s.flag);

      // Compact standings for operator glance
      renderStandings(s);

      // Footer connection pill
      setNetStatus(true, "OK");
    },
    1000,
    () => {
      setNetStatus(false, "Disconnected — retrying…");
      $("#raceClock").textContent = "--:--";
    }
  );

  // --- Launch the polling loop immediately on page load ---
  poller.start();
})();


