(function () {
  "use strict";

  const { fetchJSON, makePoller } = window.CCRS;

  const root = document.getElementById("broadcastRoot");
  if (!root) return;

  const mode = String(root.dataset.mode || "tower").toLowerCase();
  const isTower = mode === "tower";
  const isTicker = mode === "ticker";

  const rowsHost = document.getElementById("towerRows");
  const raceNameEl = document.getElementById("towerRaceName") || document.getElementById("tickerRaceName");
  const raceMetaEl = document.getElementById("towerRaceMeta") || document.getElementById("tickerRaceMeta");
  const intervalTrack = document.getElementById("intervalTrack");
  const towerLogo = document.getElementById("towerLogo") || document.getElementById("tickerLogo");
  const towerLogoFallback = document.getElementById("towerLogoFallback") || document.getElementById("tickerLogoFallback");

  const FLAG_CLASSES = new Set(["pre", "green", "yellow", "red", "white", "checkered", "blue"]);
  const MAX_ROWS = 16;
  const ROW_H = 48;

  const prevStateByEntrant = new Map();
  const rowEls = new Map();

  function normFlag(flag, phase) {
    const f = String(flag || phase || "pre").toLowerCase();
    return FLAG_CLASSES.has(f) ? f : "pre";
  }

  function setFlag(flag, phase) {
    const next = normFlag(flag, phase);
    root.classList.forEach((cls) => {
      if (cls.startsWith("broadcast--")) root.classList.remove(cls);
    });
    root.classList.add(`broadcast--${next}`);
  }

  function fmtClockMs(ms) {
    if (!Number.isFinite(ms)) return "--:--";
    const s = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }

  function fmtGap(row) {
    const lapsDown = Number(row.lap_deficit || 0);
    if (lapsDown > 0) return `+${lapsDown} LAPS`;
    const g = Number(row.gap_s || 0);
    if (!Number.isFinite(g) || g <= 0.0001) return "LEADER";
    return `+${g.toFixed(3)}`;
  }

  function updateHeader(state) {
    if (!raceNameEl || !raceMetaEl) return;
    const name = state.session_label || state.event_label || "Race";
    raceNameEl.textContent = name;

    const limit = state.limit || {};
    const type = String(limit.type || "").toLowerCase();

    if (type === "laps") {
      const total = Number(limit.value_laps ?? limit.value ?? 0);
      const leaderLaps = Array.isArray(state.standings) && state.standings.length ? Number(state.standings[0].laps || 0) : 0;
      const remain = Math.max(0, total - leaderLaps);
      raceMetaEl.textContent = `${leaderLaps}/${total} LAPS (${remain} TO GO)`;
      return;
    }

    if (type === "time") {
      const remain = Number(state.countdown_remaining_s);
      if (Number.isFinite(remain)) {
        raceMetaEl.textContent = `COUNTDOWN ${fmtClockMs(remain * 1000)}`;
        return;
      }
      const ms = Number(state.clock_ms);
      raceMetaEl.textContent = `RACE CLOCK ${fmtClockMs(ms)}`;
      return;
    }

    raceMetaEl.textContent = `RACE CLOCK ${fmtClockMs(Number(state.clock_ms))}`;
  }

  function ensureRow(key) {
    if (!rowsHost) return null;
    let el = rowEls.get(key);
    if (el) return el;

    el = document.createElement("div");
    el.className = "tower-row is-new";
    el.dataset.key = key;
    el.innerHTML = [
      '<div class="tower-pos"></div>',
      '<div class="tower-car"><span></span></div>',
      '<div class="tower-name"></div>',
      '<div class="tower-gap"></div>'
    ].join("");
    rowsHost.appendChild(el);
    rowEls.set(key, el);
    return el;
  }

  function updateRows(state) {
    if (!rowsHost || !isTower) return;
    const rows = (Array.isArray(state.standings) ? state.standings : []).slice(0, MAX_ROWS);
    const keep = new Set();

    rows.forEach((row, idx) => {
      const key = String(row.entrant_id || row.number || idx);
      keep.add(key);

      const el = ensureRow(key);
      if (!el) return;
      el.style.transform = `translateY(${idx * ROW_H}px)`;

      const posEl = el.querySelector(".tower-pos");
      const carWrap = el.querySelector(".tower-car");
      const carEl = el.querySelector(".tower-car span");
      const nameEl = el.querySelector(".tower-name");
      const gapEl = el.querySelector(".tower-gap");

      posEl.textContent = String(row.position || idx + 1);
      carEl.textContent = String(row.number || "--");
      nameEl.textContent = String(row.name || `Entrant ${row.entrant_id || ""}`);

      const color = row.color || "#475569";
      carWrap.style.setProperty("--team-color", color);

      const gapText = fmtGap(row);
      gapEl.textContent = gapText;
      gapEl.classList.toggle("is-leader", gapText === "LEADER");

      const prev = prevStateByEntrant.get(key);
      if (prev !== row.position) {
        el.classList.remove("is-new");
        void el.offsetWidth;
        el.classList.add("is-new");
      }
      prevStateByEntrant.set(key, row.position);
    });

    Array.from(rowEls.keys()).forEach((key) => {
      if (!keep.has(key)) {
        const dead = rowEls.get(key);
        if (dead && dead.parentNode) dead.parentNode.removeChild(dead);
        rowEls.delete(key);
        prevStateByEntrant.delete(key);
      }
    });
  }

  function updateTicker(state) {
    if (!intervalTrack || !isTicker) return;
    const rows = (Array.isArray(state.standings) ? state.standings : []).slice(0, MAX_ROWS);
    if (!rows.length) {
      intervalTrack.innerHTML = "";
      return;
    }

    const html = rows
      .map((row, idx) => {
        const pos = row.position || idx + 1;
        const car = row.number || "--";
        const name = row.name || "Entrant";
        const gap = fmtGap(row);
        return `<div class="interval-item"><span class="i-pos">${pos}</span><span class="i-car">${car}</span><span class="i-name">${name}</span><span class="i-gap">${gap}</span></div>`;
      })
      .join("");

    // Duplicate once to create seamless crawl loop.
    intervalTrack.innerHTML = html + html;
  }

  async function tick() {
    const state = await fetchJSON("/race/state");
    setFlag(state.flag, state.phase);
    updateHeader(state);
    updateRows(state);
    updateTicker(state);
  }

  function initLogoFallback() {
    if (!towerLogo || !towerLogoFallback) return;
    towerLogo.addEventListener("error", () => {
      towerLogo.classList.add("hidden");
      towerLogoFallback.classList.remove("hidden");
    });
  }

  function init() {
    initLogoFallback();
    root.style.transform = "none";

    root.classList.toggle("mode-tower", isTower);
    root.classList.toggle("mode-ticker", isTicker);

    const poll = makePoller(tick, 333, () => {});
    poll.start();
  }

  window.addEventListener("DOMContentLoaded", init);
})();
