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
  const tickerLapMetaEl = document.getElementById("tickerLapMeta");
  const intervalTrack = document.getElementById("intervalTrack");
  const towerLogo = document.getElementById("towerLogo") || document.getElementById("tickerLogo");
  const towerLogoFallback = document.getElementById("towerLogoFallback") || document.getElementById("tickerLogoFallback");
  const towerEventBannerEl = document.getElementById("towerEventBanner");

  const FLAG_CLASSES = new Set(["pre", "green", "yellow", "red", "white", "checkered", "blue"]);
  const MAX_ROWS = 16;
  const ROW_H = 48;

  const prevStateByEntrant = new Map();
  const rowEls = new Map();
  const bestLapByEntrant = new Map();
  const fakeCtx = {
    enabled: false,
    startMs: Date.now(),
    entrants: [],
    order: [],
    bestByEntrant: new Map(),
    lastByEntrant: new Map(),
    eventTick: 0,
    leaderLaps: 42,
    nextEventAtMs: 0,
    baseEventMs: 2300,
    cachedStandings: []
  };

  function buildFakeEntrants() {
    const palette = [
      "#d946ef", "#ef4444", "#f97316", "#eab308", "#22c55e", "#14b8a6", "#0ea5e9", "#3b82f6",
      "#6366f1", "#8b5cf6", "#ec4899", "#84cc16", "#06b6d4", "#f59e0b", "#10b981", "#f43f5e"
    ];
    const teams = [
      "Arc Flash", "Rivet Riot", "Turbo Toasters", "Spark Syndicate", "Volt Vultures", "Tin Rockets", "Grid Gremlins", "Axle Pact",
      "Jet Biscuits", "Rust Royale", "Chrome Goats", "Patch Panel", "Lunar Lugnuts", "Hammerline", "Nitro Noodles", "Brake Bias"
    ];

    const out = [];
    for (let i = 0; i < MAX_ROWS; i += 1) {
      out.push({
        entrant_id: 5000 + i,
        number: String(101 + i),
        name: teams[i],
        color: palette[i % palette.length]
      });
    }
    return out;
  }

  function ensureFakeSession() {
    if (fakeCtx.entrants.length) return;

    fakeCtx.startMs = Date.now();
    fakeCtx.entrants = buildFakeEntrants();
    fakeCtx.order = fakeCtx.entrants.slice();
    fakeCtx.bestByEntrant.clear();
    fakeCtx.lastByEntrant.clear();
    fakeCtx.eventTick = 0;
    fakeCtx.leaderLaps = 42;
    fakeCtx.nextEventAtMs = fakeCtx.startMs + 8000; // warmup/countdown phase before pass events

    fakeCtx.order.forEach((car, idx) => {
      fakeCtx.bestByEntrant.set(car.entrant_id, 37.8 + (idx * 0.14));
      fakeCtx.lastByEntrant.set(car.entrant_id, 38.5 + (idx * 0.16));
    });

    fakeCtx.cachedStandings = buildFakeStandings();
  }

  function fakeFlagForElapsed(elapsedMs) {
    const elapsedS = elapsedMs / 1000;
    if (elapsedS < 8) {
      return {
        phase: "pre",
        flag: "pre",
        countdown_remaining_s: Math.max(0, Math.ceil(8 - elapsedS))
      };
    }
    // Brief caution cycles for visual testing.
    const cycleS = Math.floor(elapsedS) % 90;
    if (cycleS >= 52 && cycleS < 58) {
      return { phase: "yellow", flag: "yellow", countdown_remaining_s: null };
    }
    if (cycleS >= 80 && cycleS < 84) {
      return { phase: "white", flag: "white", countdown_remaining_s: null };
    }
    return { phase: "green", flag: "green", countdown_remaining_s: null };
  }

  function buildFakeStandings() {
    return fakeCtx.order.map((row, idx) => {
      const pos = idx + 1;
      const laps = Math.max(0, fakeCtx.leaderLaps - Math.floor((pos - 1) / 5));
      const lapDeficit = Math.max(0, fakeCtx.leaderLaps - laps);
      const baseGap = pos === 1 ? 0 : (0.85 + ((pos - 1) * 0.94));

      return {
        entrant_id: row.entrant_id,
        number: row.number,
        name: row.name,
        color: row.color,
        position: pos,
        laps,
        lap_deficit: lapDeficit,
        gap_s: lapDeficit > 0 ? 0 : baseGap,
        last: fakeCtx.lastByEntrant.get(row.entrant_id),
        best: fakeCtx.bestByEntrant.get(row.entrant_id)
      };
    });
  }

  function applyFakePassEvent() {
    fakeCtx.eventTick += 1;

    // Leader increments roughly every few crossings.
    if (fakeCtx.eventTick % 4 === 0) {
      fakeCtx.leaderLaps += 1;
    }

    // Gentle position shuffles, event-driven only.
    if (fakeCtx.eventTick % 6 === 0) {
      const swapIdx = 1 + (Math.floor(fakeCtx.eventTick / 6) % (MAX_ROWS - 2));
      const tmp = fakeCtx.order[swapIdx];
      fakeCtx.order[swapIdx] = fakeCtx.order[swapIdx - 1];
      fakeCtx.order[swapIdx - 1] = tmp;
    }

    // One car crosses this cycle: update last lap and occasional best-lap improvement.
    const moverIdx = fakeCtx.eventTick % MAX_ROWS;
    const mover = fakeCtx.order[moverIdx];
    if (mover) {
      const baseLast = 38.2 + (moverIdx * 0.11);
      const lapJitter = ((fakeCtx.eventTick % 5) - 2) * 0.05;
      const lastLap = Math.max(33.0, baseLast + lapJitter);
      fakeCtx.lastByEntrant.set(mover.entrant_id, lastLap);

      const bestNow = Number(fakeCtx.bestByEntrant.get(mover.entrant_id));
      if (fakeCtx.eventTick % 5 === 0) {
        fakeCtx.bestByEntrant.set(mover.entrant_id, Math.max(30.0, bestNow - 0.04));
      }
    }

    const cadenceOffsets = [0, 120, -80, 160, -40, 90];
    const cadence = cadenceOffsets[fakeCtx.eventTick % cadenceOffsets.length];
    fakeCtx.nextEventAtMs += (fakeCtx.baseEventMs + cadence);
    fakeCtx.cachedStandings = buildFakeStandings();
  }

  function buildFakeState() {
    ensureFakeSession();

    const now = Date.now();
    const elapsedMs = now - fakeCtx.startMs;
    const phaseBits = fakeFlagForElapsed(elapsedMs);

    if ((phaseBits.phase === "green" || phaseBits.phase === "white") && now >= fakeCtx.nextEventAtMs) {
      // Catch up after tab inactivity, but keep bounded.
      let guard = 0;
      while (now >= fakeCtx.nextEventAtMs && guard < 4) {
        applyFakePassEvent();
        guard += 1;
      }
    }

    return {
      race_id: 999,
      event_label: "Maker Faire Orlando",
      session_label: "Visual Validation",
      phase: phaseBits.phase,
      flag: phaseBits.flag,
      countdown_remaining_s: phaseBits.countdown_remaining_s,
      clock_ms: elapsedMs,
      limit: {
        type: "laps",
        value_laps: 120
      },
      standings: fakeCtx.cachedStandings
    };
  }

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

  function fmtTickerInterval(row) {
    const lapsDown = Number(row.lap_deficit || 0);
    if (lapsDown > 0) {
      return lapsDown === 1 ? "-1 LAP" : `-${lapsDown} LAPS`;
    }
    const g = Number(row.gap_s || 0);
    if (!Number.isFinite(g) || g <= 0.0001) return "LEADER";
    return `-${g.toFixed(3)}`;
  }

  function updateHeader(state) {
    if (!raceNameEl || !raceMetaEl) return;
    const name = state.session_label || state.event_label || "Race";
    raceNameEl.textContent = name;

    if (towerEventBannerEl) {
      const evtLabel = state.event_label || "";
      towerEventBannerEl.textContent = evtLabel;
      towerEventBannerEl.style.display = evtLabel ? "" : "none";
    }

    const limit = state.limit || {};
    const type = String(limit.type || "").toLowerCase();

    if (type === "laps") {
      const total = Number(limit.value_laps ?? limit.value ?? 0);
      const leaderLaps = Array.isArray(state.standings) && state.standings.length ? Number(state.standings[0].laps || 0) : 0;
      const remain = Math.max(0, total - leaderLaps);
      raceMetaEl.textContent = `${leaderLaps}/${total} LAPS (${remain} TO GO)`;
      if (tickerLapMetaEl) tickerLapMetaEl.textContent = `LAP ${leaderLaps} OF ${total}`;
      return;
    }

    if (type === "time") {
      const remain = Number(state.countdown_remaining_s);
      if (Number.isFinite(remain)) {
        raceMetaEl.textContent = `COUNTDOWN ${fmtClockMs(remain * 1000)}`;
        if (tickerLapMetaEl) tickerLapMetaEl.textContent = `COUNTDOWN ${fmtClockMs(remain * 1000)}`;
        return;
      }
      const ms = Number(state.clock_ms);
      raceMetaEl.textContent = `RACE CLOCK ${fmtClockMs(ms)}`;
      if (tickerLapMetaEl) tickerLapMetaEl.textContent = `CLOCK ${fmtClockMs(ms)}`;
      return;
    }

    raceMetaEl.textContent = `RACE CLOCK ${fmtClockMs(Number(state.clock_ms))}`;
    if (tickerLapMetaEl) tickerLapMetaEl.textContent = `LIVE`;
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

      const bestNow = Number(row.best);
      if (Number.isFinite(bestNow) && bestNow > 0) {
        const prevBest = Number(bestLapByEntrant.get(key));
        if (!Number.isFinite(prevBest) || bestNow < prevBest - 0.0001) {
          el.classList.remove("is-best-lap");
          void el.offsetWidth;
          el.classList.add("is-best-lap");
        }
        bestLapByEntrant.set(key, bestNow);
      }
    });

    Array.from(rowEls.keys()).forEach((key) => {
      if (!keep.has(key)) {
        const dead = rowEls.get(key);
        if (dead && dead.parentNode) dead.parentNode.removeChild(dead);
        rowEls.delete(key);
        prevStateByEntrant.delete(key);
        bestLapByEntrant.delete(key);
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
        const gap = fmtTickerInterval(row);
        const color = row.color || "#475569";
        return [
          `<div class="interval-item">`,
          `<div class="i-top">`,
          `<span class="i-pos">${pos}</span>`,
          `<span class="i-car" style="--team-color:${color}"><span class="i-car-text">${car}</span></span>`,
          `<span class="i-name">${name}</span>`,
          `</div>`,
          `<div class="i-bottom">`,
          `<span class="i-gap">${gap}</span>`,
          `</div>`,
          `</div>`
        ].join("");
      })
      .join("");

    // Duplicate once to create seamless crawl loop.
    intervalTrack.innerHTML = html + html;
  }

  async function getState() {
    if (fakeCtx.enabled) {
      return buildFakeState();
    }
    return fetchJSON("/race/state");
  }

  async function tick() {
    const state = await getState();
    setFlag(state.flag, state.phase);
    updateHeader(state);
    updateRows(state);
    updateTicker(state);
  }

  async function loadFeatureFlags() {
    try {
      const features = await fetchJSON("/config/ui_features");
      fakeCtx.enabled = Boolean(features && features.broadcast && features.broadcast.testing_mode);
    } catch (_err) {
      fakeCtx.enabled = false;
    }
  }

  function initLogoFallback() {
    if (!towerLogo || !towerLogoFallback) return;
    towerLogo.addEventListener("error", () => {
      towerLogo.classList.add("hidden");
      towerLogoFallback.classList.remove("hidden");
    });
  }

  async function init() {
    await loadFeatureFlags();
    initLogoFallback();
    root.style.transform = "none";

    root.classList.toggle("mode-tower", isTower);
    root.classList.toggle("mode-ticker", isTicker);

    const poll = makePoller(tick, 333, () => {});
    poll.start();
  }

  window.addEventListener("DOMContentLoaded", init);
})();
