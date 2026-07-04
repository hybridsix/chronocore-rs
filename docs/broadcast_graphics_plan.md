# Broadcast Graphics Long-Term Plan

## Current State (Recovered)

Completed in workspace:
- Broadcast tower screen scaffold: ui/spectator/broadcast_tower.html
- Broadcast ticker screen scaffold: ui/spectator/broadcast_ticker.html
- Broadcast screen picker: ui/spectator/broadcast_select.html
- Shared broadcast runtime logic: ui/js/broadcast_tower.js
- Shared broadcast styles/theme: ui/css/broadcast_tower.css
- Operator launch point added: ui/operator/index.html (Open Broadcast Screens button)
- Ops utility page scaffolded: ui/operator/health_status.html

In progress:
- Backend route/static exposure validation for all new pages in production run modes
- End-to-end validation with live /race/state data and flag transitions

## Product Goal

Deliver stable, low-latency, OBS-friendly broadcast overlays driven by ChronoCore live state.

## Milestones

1. Foundation (Done or nearly done)
- Build tower and ticker pages with shared JS and CSS.
- Add operator entry point to open broadcast screen selector.
- Keep pages transparent/background-safe for scene overlays.

2. Data Correctness
- Validate field mapping from /race/state (position, number, name, gap, flags).
- Confirm behavior for empty fields, unknown entrants, and missing colors.
- Confirm clock behavior for both lap-limited and time-limited sessions.

3. Visual Polish
- Fine tune typography and spacing for 1080p readability.
- Verify team color contrast and fallback styling.
- Reduce animation noise while preserving position-change clarity.

4. Operational Reliability
- Add watchdog/fallback UX for fetch failures.
- Validate reconnect behavior during backend restart.
- Verify no memory leak from row churn and ticker rebuilds.

5. Broadcast Feature Expansion
- Add lower-third card page (single driver highlight).
- Add full-screen leaderboard page.
- Add pit board / incident strip page.
- Add simple URL params for style variants and row limits.

6. QA and Release
- Smoke test in Chromium and OBS browser source.
- Test with sprint and endurance race modes.
- Document operator workflow and scene setup in docs.

## Technical Backlog (Prioritized)

High:
- Add explicit empty-state rendering on tower/ticker when standings are unavailable.
- Cap/normalize very long names and numbers consistently.
- Verify CSS scaling strategy if capture resolution differs from 1920x1080.

Medium:
- Extract shared broadcast constants into one module.
- Add query-string config support (mode, refresh interval, max rows).
- Add optional sponsor/logo override from config.

Low:
- Add subtle audio/reactive cues (optional, disabled by default).
- Theme presets for day/night event packages.

## Definition of Done (Initial Broadcast Pack)

- Tower and ticker run continuously for 2+ hours without visual or polling failures.
- State transitions (pre/green/yellow/red/checkered/blue) are reflected within one poll interval.
- Operator can launch broadcast screens from home UI in one click.
- Documentation exists for opening pages and adding them to OBS scenes.

## Notes

This file was reconstructed from current workspace changes when prior chat/session plan notes were unavailable.
