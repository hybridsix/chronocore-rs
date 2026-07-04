# Broadcast Graphics Handoff - 2026-07-04

## Goal
Carry forward work on PRS-style broadcast overlays (tower + top ticker) modeled after NASCAR proportions, with safe isolation between tower and ticker CSS.

## What Was Completed Today

1. Added config-driven broadcast test mode
- Added YAML switch at `app.ui.broadcast.testing_mode` in `config/config.yaml`.
- Exposed feature via `/config/ui_features` in `backend/server.py`.
- Broadcast JS now reads this flag and can run synthetic data mode.

2. Implemented synthetic fake data mode for visual testing
- Fake mode creates 16 entrants with team colors.
- Fake timing changed from "jitter every poll" to event-driven updates, simulating only start/finish crossing events.
- This makes lap/gap updates feel realistic for non-live setup tests.

3. Improved tower motion and visuals
- Gentler row movement transitions.
- Added best-lap highlight pulse behavior.
- Car numbers in tower are italicized.

4. Reworked ticker toward NASCAR-style structure
- Two-band design: top info band + bottom intervals crawl.
- Left PRS logo only (no right duplicate).
- Two-line scrolling cells:
  - Top line: place, car number, name
  - Bottom line: interval / laps down
- Car number chips now carry team color and are italicized.

5. Fixed overlay transparency behavior for OBS
- Added overlay-root transparent handling so base app background does not bleed into tower/ticker overlays.
- Added asset version bumps on overlay pages to force browser/OBS cache refresh.

6. Split CSS so tower and ticker no longer fight each other
- `ui/css/broadcast_tower.css` now serves tower + selector concerns.
- New `ui/css/broadcast_ticker.css` created for ticker-only styles.
- `ui/spectator/broadcast_ticker.html` now points to ticker CSS.

## Current In-Progress Files (not yet committed)
- `backend/server.py`
- `config/config.yaml`
- `ui/css/broadcast_tower.css`
- `ui/css/broadcast_ticker.css` (new)
- `ui/js/broadcast_tower.js`
- `ui/spectator/broadcast_ticker.html`
- `ui/spectator/broadcast_tower.html`

## Current State / Quality
- Tower is back near a good stopping point.
- Ticker is significantly improved and structurally cleaner after CSS split.
- Left-side ticker tile proportions are now adjustable without affecting top band or scrolling cells.
- This is a valid checkpoint to resume tomorrow with small visual refinements.

## Suggested First Steps Tomorrow

1. Visual recheck in browser/OBS
- Open:
  - `http://localhost:8000/ui/spectator/broadcast_tower.html`
  - `http://localhost:8000/ui/spectator/broadcast_ticker.html`
- In OBS browser source, refresh source cache once.

2. Ticker polish pass (small)
- Fine tune these only in `ui/css/broadcast_ticker.css`:
  - `--ticker-brand-col`
  - `--ticker-lap-col`
  - `--ticker-label-col`
  - `.interval-item` min width
  - top/bottom row heights

3. Confirm fake mode behavior
- Verify event-driven updates remain smooth and non-jittery.
- If needed, tune cadence values in `ui/js/broadcast_tower.js` fake event logic.

4. Commit after visual sign-off
- Stage only broadcast-related files from this list.
- Use one commit for "broadcast ticker/tower stabilization + css split".

## Fast Resume Commands

```powershell
git status --short
./scripts/Run-Server.ps1 -Port 8000
```

## Notes for Next Session
- Keep tower and ticker styling separated.
- Prefer proportion tweaks in ticker CSS only.
- Avoid merging ticker experimental blocks back into tower CSS.
- Preserve fake mode for pre-race stream setup workflows.
