# ChronoCore Operator’s Guide

This guide provides step-by-step instructions for race-day staff to run the system, troubleshoot tags, and manage flags.  
It complements the Technical Reference Guide but stays non-technical and focused on workflows.

---

## 1. Pre-Race Setup

- Start the backend server:  
  ```bash
  uvicorn backend.server:app --reload --port 8000
  ```
- Open Operator Console UI (`/ui/operator/index.html`).
- Confirm network & timing receivers are connected.
- Load the race session (entrant list & race ID).

---

## 2. Pre-Race Mode (Flag = Pre)

- Flag starts in **pre**.  
- Send karts out for parade/test laps.  
- Watch standings list: each kart’s tag should appear at least once.  
- Verify all transponders are working before going green.  

**If a kart’s tag is missing:**  
- Check hardware placement.  
- If needed, reassign a tag: `/engine/entrant/assign_tag`.  

---

## 3. Race Start (Flag = Green)

- Announce countdown.  
- Set flag to **green**.  
- Race clock starts; standings reset to begin scoring.  
- First pass after green arms each kart.  
- Second valid pass (≥ min lap time) starts Lap 1.  

---

## 4. During Race

- **Yellow** → Caution, laps still count, marshals enforce rules.  
- **Red** → Race halted, laps still increment if karts cross.  
- **White** → Final lap indicator (optional).  
- Monitor standings (laps, last, best, pace).  
- Watch for provisional entrants (“Unknown ####”). Assign them mid-race if needed.  
- Pit timing (if enabled): verify pit in/out events are captured.  

---

## 5. Race Finish (Flag = Checkered)

- When leader crosses line under checkered, standings and clock freeze.  
- Additional passes are ignored (classification locked).  
- Snapshot is now official race result.  

---

## 6. Post-Race Actions

- Export results (CSV or DB snapshot).  
- Mark DNFs or DQs if necessary.  
- Clear race session before loading next race.  

---

## 7. Troubleshooting Quick Reference

- **Entrant missing from standings:** check enabled flag or tag assignment.  
- **Unknown entrant created:** system saw a new tag; assign it to correct racer.  
- **Clock not running:** verify flag is green.  
- **Race froze too early:** check for accidental checkered flag.  

---

## 8. Flags & Spectator Screen (2025-09-30)

**Flag meanings shown on the spectator screen:**  
- **Green** – Race On  
- **Yellow** – Caution  
- **Red** – Race Stopped  
- **White** – Final Lap  
- **Checkered** – Finish  
- **Blue** – Driver Swap (used in endurance races for mandatory pit cycles)  

**Changing flags during a race:**  
- Normally, the Operator Console has flag buttons to click.  
- If needed, flags can also be set directly via the API (for example, if the UI is not responding).  

**Via PowerShell (API call):**
```powershell
Invoke-RestMethod `
  -Method POST `
  -Uri "http://localhost:8000/engine/flag" `
  -Body (@{ flag = "green" } | ConvertTo-Json) `
  -ContentType "application/json"
```

Replace `"green"` with the desired flag: `"yellow"`, `"red"`, `"white"`, `"checkered"`, `"blue"`.

---

### 8.1 Flag Operation Cheat Sheet (2025-10-21)

**Phases:** `pre`, `countdown`, `green`, `white`, `checkered`

**Flags:** `PRE`, `GREEN`, `YELLOW`, `RED`, `BLUE`, `WHITE`, `CHECKERED`

Allowed flag presses by current phase:

| Phase | Allowed Flags |
| --- | --- |
| `pre` | `PRE`, `GREEN` (operator can arm/start green) |
| `countdown` | `PRE` only (timer flips to `GREEN` when countdown expires) |
| `green` | `GREEN`, `YELLOW`, `RED`, `BLUE`, `WHITE`, `CHECKERED` (may always return to `GREEN`) |
| `white` | `GREEN`, `YELLOW`, `RED`, `BLUE`, `WHITE`, `CHECKERED` (may always return to `GREEN`) |
| `checkered` | `CHECKERED` (locked; change phase via End/Reset controls) |

- Sending the same flag twice is idempotent. The call returns **200 OK** and leaves state unchanged.
- Countdown only permits returning to `PRE`; the clock promotes to `GREEN` when the timer elapses.

**In the Operator UI:**
- The flag pill in the header mirrors the active flag with a high-contrast color and stays on `PRE` during countdown until the race actually goes green.
- Flag buttons are disabled whenever a flag is illegal for the current phase (for example, only `PRE` is enabled during countdown). While racing, `GREEN` is always enabled so you can recover to green quickly.
- After you click a flag the UI polls `/race/state` a little faster (~250 ms) so the change appears immediately.

**Quick spot-check:**
1. From `green`, press **Yellow** → pill shows Yellow.
2. Then press **Green** → pill shows Green again.
3. During **countdown**, only **PRE** is enabled; use it to abort the start.
4. Clicking the same flag twice is a no-op but still reports success, which is expected.

---

## 9. Choosing and Using Decoders

The race timing system can work with several different hardware decoders. You only need one active at a time. This is controlled in the configuration file (`config/config.yaml`).

1. **Locate the config file**  
   - Navigate to the `config/` folder and open `config.yaml`.

2. **Find the `app.decoder` section**  
   It looks like this:  
   ```yaml
   app:
     decoder:
       enabled: true
       mode: ilap_serial
   ```

3. **Set the decoder mode**  
   Change the `mode:` line to match the hardware you are using:  
   - `ilap_serial` → I-Lap (default, most PRS events use this)  
   - `ambrc_serial` → AMB/MyLaps (older RC-style timing)  
   - `trackmate_serial` → Trackmate IR timing loop  
   - `cano_tcp` → UHF readers like Impinj/Core Speedway, connected by network  
   - `tcp_line` → Generic TCP feed (advanced / simulator)  
   - `mock` → Test generator (no hardware needed)  

4. **Check serial or TCP settings**  
   - For USB/serial devices (I-Lap, Trackmate, AMBrc), make sure the `port` is set correctly (`COM3`, `/dev/ttyUSB0`, etc.).  
   - For networked decoders (cano_tcp, tcp_line), set the `host` and `port`.  

5. **Restart the backend service**  
   After changing the file, restart the backend so it reloads the config.  
   The current status can be checked with `/decoder/status`.  

---

---

## 10. Tag Management & Conflict Behavior (2025-10-04)

# ChronoCore Operator Guide — Tag Management & Conflict Behavior
*Revision:* 2025‑10‑04

## Why this matters
Operators assign I‑Lap transponder tags to entrants. ChronoCore now guarantees that a tag can belong to **only one enabled entrant at a time**, while allowing disabled entrants to retain historical tags. The UI and API will nudge you toward safe, predictable outcomes.

---

## Core behaviors (operator‑facing)
- Assigning the **same** tag to the **same** entrant is idempotent → **200 OK**. Nothing changes behind the scenes and that’s fine.
- A tag can only belong to **one enabled** entrant. If another enabled entrant already has it, the system responds with **409 Conflict** and keeps everything unchanged.
- Clearing a tag: sending an empty string or whitespace clears it to **No Tag**.

---

## Typical workflows

### 1) Assign or update a tag
1. Select the entrant in the Operator UI.
2. Enter the tag (e.g., `1234567`), save/submit.
3. If the value is unchanged, you’ll still see a success confirmation (idempotent update).

### 2) Resolve a tag collision (409)
When you try to assign a tag that an **enabled** entrant already holds:
1. The UI will show a **409 Conflict** message (e.g., “Tag already assigned to another enabled entrant”).  
2. To resolve:
   - **Clear** the tag on the other entrant, **or**
   - **Disable** the other entrant (if appropriate), **or**
   - Choose a **different** tag for the current entrant.
3. After resolving, re‑apply the change.

### 3) Reload the session after admin edits
If you use the Entrants Admin page or import tools to change tags or enable flags, **reload the session roster** so the in‑memory engine mirrors the database.

---

## Health & readiness checks
- **Liveness:** `GET /healthz` → `{ "status": "ok" }`  
  Confirms the service is up.
- **Readiness:** `GET /readyz` → confirms the database path and schema availability.  
  Useful if something seems “stuck.”

---

## Troubleshooting

**“Entrant X not found”** when assigning a tag  
- The entrant id doesn’t exist in the database yet. Seed it via the Entrants Admin tool (or roster import), then reload the session.

**409 Conflict** when enabling an entrant or assigning a tag  
- Another **enabled** entrant has that tag. Clear or change the other entrant’s tag, or disable that entrant.

**400 error on `/engine/load`**  
- The roster payload is malformed (e.g., missing `id` or a non‑integer `id`). Fix the data, then retry.

---

## Where the database file lives (defaults)
- Default path: `backend/db/laps.sqlite`  
- Can be overridden in `config/app.yaml`:
```yaml
app:
  engine:
    persistence:
      db_path: backend/db/laps.sqlite
```

---

## Quick reference (operator‑level)
| Action | Expected result |
|---|---|
| Assign same tag to same entrant | **200 OK** (no change), UI remains consistent |
| Assign tag used by *enabled* entrant | **409 Conflict** (nothing changes) |
| Clear a tag (send empty/whitespace) | **200 OK**, tag cleared |
| Reload session after admin edits | Engine mirrors DB; changes take effect |

---

*End of operator update.*

---



---

# Operator’s Guide — Tag Scanning & Entrants Integration (v2025-10-06)
...

---

## 11. Engine Host Setup (2025 Update)

The Operator UI needs to know which **engine host** to talk to. This can be:

- **Same-origin** (if you open the Operator UI directly from the engine’s built-in web server)  
- **Local engine** (developer testing on your laptop)  
- **Fixed host** (a kiosk or field deployment where the engine runs on a set IP/port)  

### Where to Configure

Engine host defaults now live in **`config/app.yaml`** under `app.client.engine`.

```yaml
app:
  client:
    engine:
      mode: fixed
      fixed_host: "10.77.0.10:8000"
      prefer_same_origin: true
      allow_client_override: false
```

### Keys

- `mode`:  
  - `localhost` → always use `127.0.0.1:8000` (developer testing)  
  - `fixed` → always use `fixed_host` (field deployment)  
  - `auto` → use same-origin if possible; otherwise fall back to `fixed_host`, then `localhost`  

- `fixed_host`: The IP:port of your engine server, required if `mode` is `fixed`.  

- `prefer_same_origin`: When true, pages opened via the engine web server ignore host strings and just use same-origin.  

- `allow_client_override`: When true, operators can set a custom engine host in the **Settings** page. When false, the field is read-only.  

### Effective Engine Display

The Operator UI footer always shows the **Effective Engine** host it is connected to. This string comes from the resolution rules above.

- Example (localhost): `Engine: 127.0.0.1:8000`  
- Example (fixed): `Engine: 10.77.0.10:8000`  
- Example (same-origin): `Engine: same-origin`  

If you see “Disconnected — retrying…” in the footer, check this host setting first.  

---
