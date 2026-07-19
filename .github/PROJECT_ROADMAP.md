# ChronoCoreRS - Long-Term Project Roadmap

> **Read this at the start of every work session on this project**, especially if picking
> up where a previous session left off. Update it as items are completed or re-prioritized.

_Last updated: 2026-07-19_

---

## Current Status

**Broadcast graphics stream — essentially done.** Tower, ticker, and status overlays have
been tested live in both OBS and vMix. Remaining work here is small display/polish tweaks
only, not a blocking priority. Reference docs: `docs/technical_reference.md` §17,
`docs/operators_guide.md` §17.

---

## Next Up (in priority order)

> Items build on one another in this order - venv/script robustness (1) underpins the
> firewall work (2), which underpins the venv strategy decision (3), which underpins
> installer packaging (4).

### 1. Fix/consolidate startup scripts & batch files for manual operation - DONE (2026-07-19)

**Goal:** Every script a user might double-click or run manually (`ChronoCore.bat`,
`scripts/Run-Server.ps1`, `scripts/Run-Operator.ps1`, `scripts/Run-Spectator.ps1`/`.sh`)
should reliably work, and there should be clear written expectations for how Python is
supposed to be set up on the host machine.

**What was done:**
- Extracted the venv bootstrap (find Python, verify 3.12+, create/repair venv, install
  deps) into a single shared file: `scripts/lib/Ensure-Venv.ps1`, exposing one function -
  `Invoke-EnsureVenv -Root $Root` - that both `Run-Operator.ps1` and `Run-Server.ps1` now
  dot-source and call, instead of each maintaining its own copy.
- `Run-Server.ps1` now gets the same broken-venv detection/recovery and `py -3` fallback
  that `Run-Operator.ps1` already had - previously it only had the older, less robust
  inline logic.
- `ChronoCore.bat` no longer duplicates venv creation at all (that could drift from the
  PS1 logic); it now only does a fast native "is Python findable" pre-check (checking
  both `python` and `py -3`, matching the PS1 scripts) before delegating fully to
  `Run-Operator.ps1` for venv/dependency setup.
- `Run-Spectator.ps1`/`.sh` reviewed - they don't touch Python/venv at all (they just
  launch a browser at a URL), so no changes were needed there.
- Added a short "Requirements" callout to `README.md`'s Quick Start section documenting
  the `python`/`py` launcher expectation and that `.venv` is now fully self-managing.
- Smoke-tested: refactored `Run-Server.ps1` starts uvicorn successfully end-to-end.

---

### 2. Windows Firewall rules for all network-facing services

**Goal:** OSC in/out, the HTTP server, and any other network-facing pieces should be able
to communicate across the network without the user having to manually click through
Windows Firewall prompts or configure rules by hand.

**Current state:**
- `Run-Server.ps1` already creates two firewall rules scoped to the venv's `python.exe`
  program path (not specific ports): an inbound rule (allows the HTTP server to accept
  connections) and an outbound rule (labeled for OSC lighting output). Because these are
  program-scoped rather than port-scoped, they likely already cover OSC traffic too, but
  this hasn't been explicitly verified.
- `Run-Operator.ps1` and `Run-Spectator.ps1` do not create any firewall rules.
- OSC output (`app.hardware...osc_out`, default port 9000) and OSC input (`osc_in`,
  default port 9010) are configured in `config/config.yaml` — worth confirming rules
  cover both directions/ports explicitly rather than relying on incidental program-scope
  coverage.

**Open questions / next steps:**
- Decide whether firewall rules should be created once at install time (e.g. from an
  installer or a one-time setup script) rather than re-checked on every server launch.
- Verify behavior when the venv is rebuilt/moved (the program-path-scoped rule would
  become stale and need recreating — ties into item 3 below).

> _Agent note (2026-07-19): sub-tasks below added to make this actionable when we pick it
> up. Remove this note once reviewed/adjusted._
>
> - [ ] 2.1 Empirically verify the existing program-scoped inbound/outbound rules in
>       `Run-Server.ps1` actually permit OSC UDP traffic (send a real OSC packet with the
>       rule present vs. removed, on both the sending and receiving machine).
> - [ ] 2.2 Decide: keep program-path-scoped rules, or switch to explicit port-scoped
>       rules (8000 TCP for HTTP, 9000 UDP out / 9010 UDP in for OSC) so they survive a
>       venv rebuild without needing to be recreated.
> - [ ] 2.3 Move rule creation out of "checked on every launch" into a one-time setup
>       step (dedicated `scripts/Setup-Firewall.ps1`, or fold into the future installer
>       from item 4) to avoid repeat UAC prompts.
> - [ ] 2.4 Add the same firewall handling to `Run-Operator.ps1` (currently only
>       `Run-Server.ps1` creates rules, but the operator app also runs the same backend).

---

### 3. Venv strategy — do we still need it, and how do we make it robust for new installs?

**Plain-language explainer (for reference):** A Python "venv" (virtual environment) is a
self-contained folder with its own copy of the Python interpreter and installed packages,
isolated from whatever Python is installed system-wide. It exists so that ChronoCoreRS's
exact dependency versions (FastAPI, uvicorn, pandas, PySide6, etc.) don't conflict with
other Python projects/programs on the same machine, and so a fresh checkout can install a
known-good set of packages via `pip install -r backend/requirements.txt` without touching
system Python at all.

**Why it's been causing problems here:** A venv is **not portable** — it hardcodes the
absolute path to the system Python it was created from inside its launcher scripts. If the
project folder is moved, or synced to a different machine via Nextcloud (as this repo is),
the venv's `python.exe` stub can point at a Python install path that doesn't exist on the
new machine, breaking it silently until someone tries to run it. This happened in this
project on 2026-07-19 and required deleting and rebuilding `.venv` from scratch.

**Open questions / next steps:**
- Do we keep the venv model for end users, or move toward something more self-contained
  (e.g. a bundled/embeddable Python distribution shipped alongside the app, so there's no
  dependency on whatever Python happens to be installed on the host)? This overlaps
  heavily with item 4 (installer-friendly packaging).
- If we keep venv: make broken-venv detection/recovery (already added to
  `Run-Operator.ps1`) consistent across all entry scripts (ties into item 1). **Done as
  of 2026-07-19** — see item 1.
- If we move to a bundled/frozen distribution: venv concerns mostly disappear for end
  users, but dev workflow (this repo, active development) would likely still use a venv.

> _Agent note (2026-07-19): recommendation + sub-tasks added. Remove/adjust once reviewed._
>
> **Recommendation:** keep the venv model for *development* (this repo) since item 1 now
> makes it self-healing and low-friction for contributors. For *end-user installs*,
> treat this as a packaging problem and solve it via item 4 (bundled/frozen Python)
> rather than continuing to invest in the raw venv experience for non-developers.
>
> - [ ] 3.1 Prototype a PyInstaller (or embeddable Python) build of the operator app to
>       see how large/practical a fully self-contained executable is.
> - [ ] 3.2 If prototype looks viable, decide the dev-vs-end-user split explicitly (dev =
>       venv via `scripts/lib/Ensure-Venv.ps1`, end-user = bundled build from item 4) and
>       document it so it doesn't get re-litigated later.
> - [ ] 3.3 If bundling isn't practical, invest instead in making `.venv` recreation
>       faster/more resilient (e.g. detect the Nextcloud-sync-path-break scenario
>       specifically and message it clearly to the user).

---

### 4. Installer-friendly packaging, official releases, and hiding ancillary console windows

**Goal:** Make it realistic for a new user on a new computer to install and run
ChronoCoreRS without needing to understand Python, virtual environments, or PowerShell at
all. Also stop popping up separate visible PowerShell/console windows for background
processes (e.g. the lap logger window spawned by `Run-Server.ps1`) — fold that
functionality into the main application process instead, so end users only ever see the
polished UI.

**Current state:**
- No installer or packaging tooling exists yet (no PyInstaller spec, no Inno
  Setup/NSIS script, no MSIX).
- `Run-Server.ps1` currently launches the lap logger via `Start-Process` in its own
  separate PowerShell window (`-NoExit` with a visible command prompt).
- The desktop app path (`Run-Operator.ps1` → pywebview/PySide6) is the closest thing to a
  "real app" experience today, but it still depends on a venv being bootstrapped first.

**Ideas to evaluate:**
- Package the backend + desktop shell with PyInstaller (or similar) into a single
  distributable executable/installer, bundling Python so end users never install it
  separately.
- Run ancillary processes (lap logger, etc.) as background threads/subprocesses of the
  main app process rather than separate visible console windows — or at minimum launch
  them with hidden windows and surface their status/logs inside the operator UI instead.
- Look at Inno Setup or a similar Windows installer builder for an actual "Setup.exe"
  experience, including the firewall rule creation from item 2 as an install step.
- Define what an "official release" looks like (versioned build, changelog entry,
  attached installer artifact) — likely ties into `CHANGELOG.md` conventions already in
  use.

> _Agent note (2026-07-19): sub-tasks added to sequence this once items 1-3 land. Remove
> once reviewed._
>
> - [ ] 4.1 Convert the lap logger launch in `Run-Server.ps1` from a visible
>       `Start-Process pwsh -NoExit` window into either (a) an in-process background
>       thread of the main server, or (b) a hidden-window subprocess with output/status
>       surfaced in the operator UI instead of a raw console.
> - [ ] 4.2 Build a PyInstaller spec for the operator desktop app (backend + pywebview
>       shell) as the first packaging milestone; treat browser-mode server as secondary.
> - [ ] 4.3 Wrap the PyInstaller output in an Inno Setup installer that also creates the
>       firewall rules from item 2 and drops a Start Menu / Desktop shortcut.
> - [ ] 4.4 Define "official release" process: version bump + `CHANGELOG.md` entry +
>       installer artifact attached to a GitHub Release.

---

## Parking Lot

- Nothing yet — add smaller/uncategorized ideas here as they come up during the above
  work so they don't get lost.
