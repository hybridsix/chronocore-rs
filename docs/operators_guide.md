# ChronoCore Operator's Guide

Welcome! This guide will help you run ChronoCore on race day. We'll cover everything from starting up the system to handling common issues. If you're looking for deep technical details, check out the Technical Reference Guide instead.

---

## 0. What's New (2025-11-13)

Here's what changed in the latest update:

- **Moxie Board Integration (2025-11-13)**: Added support for wireless Moxie Board scoring with configurable point pools and display positions. Enable via config.yaml to show moxie scoring in the operator UI.
- The header and footer now look the same across all pages. The **DB: Ready** indicator moved to the center of the footer, and we added a home icon plus hamburger menu at the top.
- New **Diagnostics / Live Sensors** page shows you a live stream of transponder reads. You can pause, clear the log, and even enable a beep sound for each detection.
- We clarified the difference between **Freeze** (when you take a snapshot) and **Frozen** (when the race automatically locks after the leader crosses under the checkered flag).
- **Results & Exports** now clearly shows Live Preview (while racing) versus Frozen Standings (official results). You can toggle between frozen and live views using the pill buttons at the top. Both lap and event CSV exports use consistent file names now.
- The Entrants & Tags page has an "Entrant Enabled" toggle and a **Status** dropdown (ACTIVE, DNS, DNF, RET, Other).
- **Qualifying Race Support**: After a qualifying race finishes (checkered flag), a "Freeze Grid Standings" button appears on Race Control. Click it to lock in the starting order for subsequent races based on best lap times. You can choose how to handle brake test failures: demote to back of grid, use next valid lap, or exclude entirely. Deleting a qualifying race from Results automatically clears its frozen grid.
- Added troubleshooting help for when laps aren't counting but you can see transponder reads coming through.


## 1. Pre-Race Setup

### 1.1 Installing and Starting the Software (Python 3.12 Required)

First, you'll need to install Python 3.12 if you haven't already. Then follow these steps:

**On Windows (PowerShell):**
```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn backend.server:app --reload --port 8000
```

**On Mac/Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn backend.server:app --reload --port 8000
```

**Quick health check:** Open your browser to `http://localhost:8000/health` and you should see `{"ok": true}`. The main UI is at `http://localhost:8000/ui`.

### 1.2 Pre-Race Checklist

Before the drivers arrive:

1. Start the backend server (see commands above)
2. Open the Operator Console at `/ui/operator/index.html`
3. Make sure your timing hardware is connected and powered on
4. Load your race session with the driver list and race ID

---

## 2. Pre-Race Mode (Flag = Pre)

When you first start up, the system is in **PRE** mode. This is your chance to make sure everything is working:

- The flag will show as **PRE** in the header
- Send your karts out for warm-up laps or a parade lap
- Watch the standings list - each kart's transponder should register at least once
- This is the time to catch any transponder problems before you go green

**If a kart's transponder isn't showing up:**
- First, check that the transponder is mounted securely and powered on
- If you need to swap transponders or reassign tags, you can do this on the Entrants & Tags page

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

## 4. During the Race

While the race is running, you can use different flags to manage the action:

- **Yellow** - Caution period. Laps still count, but marshals enforce no-passing rules.
- **Red** - Race stopped. If karts keep crossing the line, their laps still count (you'll sort it out after).
- **White** - Final lap indicator (system may throw this automatically depending on your race mode).
- **Blue** - Driver swap notification (used in endurance racing for mandatory pit stops).

**What to watch for:**
- Keep an eye on the live standings showing laps completed, last lap time, best lap, and pace
- If you see entries like "Unknown 1234", that's a transponder the system doesn't recognize. You can assign it to the correct driver on the fly.
- If pit timing is turned on, make sure pit-in and pit-out events are recording properly

---

## 5. Ending the Race (Flag = Checkered)

### 5.1 Understanding Race Finish Modes

ChronoCore supports two different race finish behaviors:

**Hard-End Mode (Traditional):**
- Click **CHECKERED** flag button
- Leader crosses the finish line
- Race **freezes immediately** - clock stops, standings lock
- Any transponder reads after this point are ignored

**Soft-End Mode (Recommended for Most Races):**
- System automatically throws **WHITE** flag at T-60s (time races) or lap N-1 (lap races) as a warning
- System automatically throws **CHECKERED** flag when time expires (T=0) or leader completes final lap
- Race **continues counting laps** for a configurable timeout period (default: 30 seconds)
- Drivers can finish their current lap and cross the line
- System tracks the order drivers cross after the limit is reached
- Race **freezes automatically** after the timeout expires

### 5.2 How Soft-End Works (Step by Step)

Let's say you're running a 10-lap race with soft-end enabled:

1. **Lap 9**: Leader completes lap 9
   - System automatically throws **WHITE** flag (warning: final lap coming)
   - Lights/sounds indicate final lap situation

2. **Lap 10**: Leader crosses at lap 10
   - System automatically throws **CHECKERED** flag
   - Checkered lights come on
   - Race **keeps running** - clock continues, lap counting continues
   - Leader gets finishing position #1

3. **During the 30-second window**:
   - Other drivers complete their current lap and cross the line
   - Each driver gets a finishing position based on when they cross: #2, #3, #4, etc.
   - System prevents drivers from starting additional laps (one crossing per driver)
   - Standings update to show final positions

4. **After 30 seconds**: Race freezes automatically
   - Clock stops at the timeout moment
   - Final standings locked
   - Official results ready

### 5.3 Why Use Soft-End?

**Advantages:**
- More accurate lap counts (drivers get credit for laps in progress)
- Natural race finish (no sudden cutoff mid-lap)
- Fair classification (finishing order based on actual crossing times)
- Better spectator experience (watch everyone finish)

**When to Use Hard-End:**
- Qualifying sessions where only best lap matters
- Practice sessions
- When you need immediate freeze for time-critical scheduling

### 5.4 What You'll See

During soft-end mode:
- **Flag banner**: Shows CHECKERED (race has finished)
- **Race clock**: Continues ticking up to the timeout
- **Standings**: Update as drivers cross the finish line
- **Position numbers**: Reflect finishing order, not just lap count
- **Running indicator**: Shows "Race Running" until timeout expires

### 5.5 Manual Override

You can still manually throw flags during soft-end:
- **YELLOW/RED**: Stop action if there's an incident
- **Checkered stays active**: You can't un-throw the checkered flag
- Race will still freeze after the configured timeout

### 5.6 Configuration

Soft-end is configured per race mode in `config/race_modes.yaml`:

```yaml
sprint_10_laps:
  label: "10 Lap Sprint"
  limit:
    type: laps
    value: 10
    soft_end: true              # Enable soft-end
    soft_end_timeout_s: 30      # 30 second window
```

Common timeout values:
- **Sprint races**: 30 seconds (default)
- **Endurance races**: 45-60 seconds (longer laps)
- **Club races**: 30-45 seconds

Easy as that!

## 6. After the Race

Once the race is over, here's what to do:

- **Export your results** - Use the Results & Exports page to download CSV files or save the full database
- **Mark DNFs or DQs** - If someone didn't finish or got disqualified, update their status on the Entrants page
- **Clear the session** - Before loading the next race, make sure to clear the current one so you start fresh

---

## 7. Troubleshooting Common Issues

### 7.1 Laps Aren't Counting (But Transponder Reads Are Showing Up)

This is actually pretty common. Here's what to check:

- Look at your `min_lap_s` setting - if it's set to 10 seconds and your drivers are doing 8-second laps, those won't count
- Check the duplicate window threshold - readings too close together get filtered out
- Make sure the **Entrant Enabled** toggle is turned ON for that driver
- Verify the tag is assigned to the right person
- Confirm you can see the passes coming through on the **Diagnostics / Live Sensors** page
- Check `/race/state` to see the raw `laps`, `last`, and `best` values for each driver

### 7.2 Other Common Problems

**Driver not showing in standings?**
Check if they're enabled and have a tag assigned.

**Getting "Unknown" entries?**
The system saw a transponder it doesn't recognize. Just assign it to the correct driver.

**Race clock not moving?**
Make sure the flag is set to green.

**Race ended too early?**
You might have accidentally hit the checkered flag button - easy to do when things get exciting!  

## 8. Understanding Race Flags (2025-09-30)

The spectator screen (and your operator console) show different colored flags during the race. Here's what they mean:

**Flag Colors:**
- **Green** - Race is ON! The clock is running.
- **Yellow** - Caution period. Everyone slow down, no passing.
- **Red** - Race stopped. Something serious happened.
- **White** - Final lap coming up!
- **Checkered** - Race is over. Congratulations to the winners!
- **Blue** - Driver swap time (mainly for endurance races with mandatory pit cycles)

**Changing Flags During a Race:**

Normally, you'll just click the flag buttons in the Operator Console. But if your UI isn't responding or you need to change flags from a script, you can do it directly via the API.

**Using PowerShell to change flags:**
```powershell
Invoke-RestMethod `
  -Method POST `
  -Uri "http://localhost:8000/engine/flag" `
  -Body (@{ flag = "green" } | ConvertTo-Json) `
  -ContentType "application/json"
```

Just replace `"green"` with whatever flag you need: `"yellow"`, `"red"`, `"white"`, `"checkered"`, or `"blue"`.

### 8.1 How Flag Phases Work (2025-10-21)

Behind the scenes, the race goes through different **phases**, and only certain flags work in each phase. You don't need to memorize this - the UI will gray out buttons that won't work - but it's helpful to understand what's happening.

**Race Phases:**
- `pre` - Getting ready, grid is forming up
- `countdown` - Optional countdown timer before the start
- `green` - Racing! The action is happening
- `white` - Final lap situation
- `checkered` - Race is over, everything's locked

**Which Flags Work When:**

| Phase | Available Flags | Notes |
| --- | --- | --- |
| `pre` | PRE, GREEN | You can arm the start or just send them green |
| `countdown` | PRE only | The timer will automatically go green when it expires, or you can abort to PRE |
| `green` | All flags | You can always get back to GREEN if you need to clear a caution |
| `white` | All flags | Same as green, just shows a white flag banner |
| `checkered` | CHECKERED only | Race is done - use the End/Reset controls to start a new session |

**A Few Things to Know:**
- If you click the same flag twice, nothing breaks - the system just says "okay, already there" and moves on
- During countdown, only the PRE button works because the system wants to control when you actually go green
- Once you're racing (green or white), you can always hit GREEN again to clear a yellow or red flag quickly
- After you press a flag, the UI polls a bit faster for a couple seconds so you see the change right away

**Quick Test:**
1. While green, click **Yellow** - you'll see the yellow flag appear
2. Click **Green** again - back to green
3. During a **countdown**, only **PRE** will work - use it if you need to abort the start
4. Clicking the same flag twice is totally fine - it just confirms what's already set

---

## 8.2 Lighting Integration with QLC+ (OSC Control)

ChronoCore can control professional lighting via QLC+ lighting software using OSC (Open Sound Control) protocol. This allows race flags to automatically trigger lighting cues, creating a synchronized visual experience for spectators.

### How It Works

**Bidirectional Communication:**
- **CCRS → QLC+** (Outbound): Flag changes in Race Control automatically send commands to lighting
- **QLC+ → CCRS** (Inbound): Lighting operator can trigger cautions/flags from lighting console

**What Gets Synchronized:**
- All race flags (GREEN, YELLOW, RED, WHITE, CHECKERED, BLUE)
- Countdown/staging (RED lights during countdown)
- Blackout (lights off during transitions/resets)

### Configuration

Edit `config/config.yaml` to enable OSC lighting:

```yaml
integrations:
  lighting:
    osc_out:
      enabled: true
      host: 192.168.1.101      # QLC+ computer IP
      port: 9000               # QLC+ OSC input port
      send_repeat:
        count: 2               # Send twice for UDP reliability
        interval_ms: 50
      addresses:
        flag:
          green:     "/ccrs/flag/green"
          yellow:    "/ccrs/flag/yellow"
          red:       "/ccrs/flag/red"
          white:     "/ccrs/flag/white"
          checkered: "/ccrs/flag/checkered"
          blue:      "/ccrs/flag/blue"
        blackout:    "/ccrs/blackout"
    
    osc_in:
      enabled: true
      host: 0.0.0.0            # Listen on all network interfaces
      port: 9010               # CCRS OSC input port
      paths:
        flag_prefix: "/ccrs/flag/"
        blackout: "/ccrs/blackout"
      threshold_on: 0.5        # Button value >= 0.5 = ON
      debounce_off_ms: 250     # Ignore OFF when switching flags
```

### When Lights Trigger

**Automatic Triggers (CCRS → QLC+):**
- **Race Start** → GREEN lights
- **Countdown** → RED lights (staging)
- **Flag Changes** → Corresponding flag color
- **Race End** → CHECKERED lights
- **Abort/Reset** → BLACKOUT (all off)
- **Open Results** → BLACKOUT (all off)

**Lighting Operator Control (QLC+ → CCRS):**
- Can trigger **cautions** (YELLOW, RED) during active race
- Can change flags (GREEN ↔ YELLOW) for caution periods
- **Cannot** start race (GREEN blocked during PRE/COUNTDOWN)
- **Cannot** end race prematurely (CHECKERED blocked unless racing)

### Safety Features

The system prevents lighting operators from accidentally controlling race timing:
- ✅ **Can** call cautions during race
- ✅ **Can** switch between racing flags (GREEN/YELLOW/RED/BLUE)
- ❌ **Cannot** start the race from QLC+
- ❌ **Cannot** end the race from QLC+

**Example:** If lighting operator clicks GREEN during pre-race, the system logs a warning and ignores it. Race Control must click "Start Race" to begin.

### Troubleshooting Lighting

**Lights not responding to flag changes?**
- Check that QLC+ is running and listening on the configured port
- Verify network connectivity between CCRS and QLC+ computers
- Check server logs for OSC errors
- Lighting failures never break race control - timing continues normally

**Lighting triggering unwanted flag changes?**
- Verify OSC feedback is configured correctly in QLC+
- Check threshold_on setting (default 0.5)
- Confirm QLC+ output port matches CCRS osc_in port

**No blackout on reset?**
- Check that blackout address is configured in both CCRS and QLC+
- Verify blackout button/function exists in QLC+ workspace

---

## 9. Choosing and Using Timing Hardware

Your race timing system can work with several different hardware decoders. You only need one active at a time, and this is controlled in your configuration file.

**How to Pick the Right Decoder:**

1. **Open your config file**  
   Go to the `config/` folder and open `config.yaml`

2. **Find the `scanner.source` section**  
   It'll look something like this:  
   ```yaml
   scanner:
     source: ilap.serial
     decoder: ilap_serial
   ```

3. **Choose your hardware type**  
   Change the `source:` line to match what you've got:  
   - `ilap.serial` - I-Lap USB/serial (this is the default and what most PRS events use)  
   - `ambrc.serial` - AMB/MyLaps hardware (older RC-style timing)  
   - `trackmate.serial` - Trackmate IR timing loop  
   - `ilap.udp` - I-Lap over network (for multiple readers)  
   - `mock` - Test mode (no hardware needed, great for testing)  

4. **Set up the decoder details**  
   Under `app.hardware.decoders`, find your decoder type and adjust the settings:
   ```yaml
   app:
     hardware:
       decoders:
         ilap_serial:
           port: COM3        # Windows: COM3, COM7, etc. | Linux: /dev/ttyUSB0
           baudrate: 9600
           init_7digit: true
   ```

5. **For USB/serial connections**  
   - Set the right `port` - Windows uses things like `COM3`, Linux uses `/dev/ttyUSB0`  
   - Baudrate is typically 9600 for I-Lap, 19200 for AMBrc

6. **For network-based decoders**  
   - Set up the UDP section:
   ```yaml
   scanner:
     udp:
       host: 0.0.0.0
       port: 5000
   ```

7. **Restart and check**  
   After saving the file, restart the backend so it picks up the changes.  
   You can check the decoder status at `/decoder/status` to make sure everything connected properly.

---

## 10. Managing Transponder Tags (2025-10-04)

### Why This Matters

You need to assign transponder tags to drivers so the system knows who's who. ChronoCore makes sure that each tag only belongs to **one active driver at a time**. Disabled drivers can keep their old tags in the system for record-keeping, but those tags won't interfere with active assignments.

### How Tag Assignment Works
- If you assign the **same** tag to the **same** driver, nothing happens - that's fine! The system just says "okay, already done" and keeps going. (This is called "idempotent" in tech speak.)
- A tag can only belong to **one active driver at a time**. If you try to give a tag to someone else who already has it active, you'll get a **409 Conflict** error and nothing will change.
- To clear a tag, just save it as blank or with only spaces - the system will set it to **No Tag**.

---

## Common Tag Workflows

### 1) Assigning or Updating a Tag
1. Pick the driver in the Operator UI
2. Enter their transponder number (like `1234567`) and save
3. Even if the tag was already correct, you'll see a success message - no harm done!

### 2) Fixing a Tag Conflict (409 Error)
When you try to assign a tag that's already active on another driver:
1. You'll see a **409 Conflict** error message (something like "Tag already assigned to another enabled entrant")
2. To fix it, you have three options:
   - **Clear** the tag from the other driver, **or**
   - **Disable** the other driver (if they're not racing), **or**
   - **Pick a different tag** for the current driver
3. Once you've made the change, try the assignment again

### 3) Reloading After Making Changes
If you use the admin tools to bulk-edit drivers or import rosters, make sure to **reload the session** so the live race engine sees your changes.

---

## Health Checks

You can quickly check if the system is working properly:

- **Is it running?** Go to `/healthz` - you should see `{ "status": "ok" }`
- **Is the database ready?** Go to `/readyz` - this confirms the database file exists and the schema is set up correctly. Great for troubleshooting startup problems.

---

## Common Problems and Solutions

**"Entrant X not found" when assigning a tag**
The driver doesn't exist in the database yet. Add them using the Entrants Admin page or roster import tool, then reload the session.

**409 Conflict when enabling a driver or assigning a tag**
Another active driver has that tag. Clear their tag, change to a different tag, or disable the other driver if they're not racing.

**400 error on `/engine/load`**  
Your roster data has a problem (maybe a missing or invalid ID). Fix the roster file and try loading again.

---

## Where the Database Lives

By default, ChronoCore stores everything in `backend/db/laps.sqlite`. If you need to change this, edit your config file:

```yaml
app:
  engine:
    persistence:
      db_path: backend/db/laps.sqlite
```

---

## Quick Reference for Common Tasks

| What You're Doing | What Happens |
|---|---|
| Assign same tag to same driver | **200 OK** - nothing changes, system just confirms it's already set |
| Assign tag used by *enabled* entrant | **409 Conflict** (nothing changes) |
| Clear a tag (send empty/whitespace) | **200 OK**, tag cleared |
| Reload session after admin edits | Engine mirrors DB; changes take effect |

---

## 11. Connecting to the Race Engine (2025 Update)

The Operator UI needs to know where your race engine is running. There are a few different setups:

- **Same computer** (the UI and engine are both on one machine - great for testing)
- **Fixed network address** (the engine runs on a specific server or Raspberry Pi at a known IP address)
- **Automatic detection** (the system figures it out based on how you opened the page)

### How to Configure This

The engine connection settings are in your `config/app.yaml` file under `app.client.engine`:

```yaml
app:
  client:
    engine:
      mode: fixed
      fixed_host: "10.77.0.10:8000"
      prefer_same_origin: true
      allow_client_override: false
```

### What the Settings Mean

- **mode** - Pick how the system finds the engine:
  - `localhost` - Always use your own computer at `127.0.0.1:8000` (great for development)
  - `fixed` - Always use the `fixed_host` address you specify (great for race-day deployment)
  - `auto` - Try to be smart: use the same server that served the page, or fall back to the fixed host, or use localhost

- **fixed_host** - The IP address and port of your engine server (like `"10.77.0.10:8000"`). You need this if you picked `fixed` mode.

- **prefer_same_origin** - When turned on, pages opened through the engine's web server will automatically connect to that same server.

- **allow_client_override** - When turned on, operators can manually set a custom engine address in the Settings page. Turn this off for locked-down race-day kiosks.

### Checking Your Connection

Look at the footer of the Operator UI - it always shows the **Effective Engine** address it's talking to:

- `Engine: 127.0.0.1:8000` means it's using your local computer
- `Engine: 10.77.0.10:8000` means it's using a fixed network address
- `Engine: same-origin` means it's using the same server that loaded the pageIf you see "Disconnected - retrying..." in the footer, double-check your engine host setting - that's usually the culprit!

---


## 12. Page Tour / Operator Workflow (2025 Update)

- **Entrants & Tags** - roster editing, tag assignment, "Entrant Enabled" + Status.
- **Race Setup** - choose a heat on the left, grid preview on the right; "Grid: frozen" = locked starting grid (separate from results Freeze).
- **Race Control** - flag buttons, big race clock in page body, standings viewport ~16 rows before scroll.
- **Diagnostics / Live Sensors** - live SSE stream of passes; pause/resume; clear; RSSI toggle; optional beep; bounded to 500 rows.
- **Results & Exports** - view **Live Preview** (running) or **Frozen Standings** (after checkered); export CSVs.
- **Settings** - engine host policy, UI prefs.

## 13. Race Control - Clarifications

- **Green starts the official clock.** Countdown shows `PRE` until start actually goes green.
- **Red**: laps still count (discipline via marshals).
- **Yellow**: no special engine logic; scoring continues.
- **Checkered**: when the **leader** next crosses, standings and clock **freeze** (“Frozen” state).

### Freeze vs Frozen
- **Freeze (button)** - takes a local snapshot for reviewing/exporting while a race may still be live.
- **Frozen (state)** - official classification lock after checkered+leader-cross; additional passes do not change order.

### Demote (presentation aid)
Temporarily move an out-of-place entrant down the order for the display while you fix data (e.g., tag merge). Raw pass history remains intact.

## 13.1 Qualifying Races and Grid Freezing (2025-11-03)

ChronoCore supports a complete qualifying workflow where you can set the starting grid for races based on qualifying session results.

### Running a Qualifying Session

1. **Set up the race** - In Race Setup, select race type "qualifying"
2. **Set brake test flags** - During or after qualifying, use the Race Control page to mark each driver's brake test status (pass/fail) using the brake flag button
3. **Run the session** - Let drivers complete their qualifying laps
4. **Throw checkered** - When time expires, hit the checkered flag
5. **Freeze the grid** - An orange "Freeze Grid Standings" button appears with a breathing animation

### Freezing the Grid

When you click "Freeze Grid Standings":

1. The system captures:
   - Each driver's best lap time
   - Their brake test status (pass/fail/null)
   - Their qualifying position

2. You choose a **brake test failure policy**:
   - **demote** - Drivers who failed (or have no brake test result) move to the back of the grid, sorted by best lap time
   - **warn** - Show brake test badge but don't affect grid position (display only)

3. The grid is saved to the event config and persists across sessions

### Applying the Frozen Grid

Once frozen, the grid automatically applies to subsequent races:

- **Race Control** - Standings and Seen tables show drivers in grid order
- **Grid order priority**: Drivers with passing brake tests appear first (by qualifying position), then non-qualified drivers, then drivers who failed brake tests (sorted by best lap)
- **Race Setup page** displays "Grid: frozen" indicator
- **Results page** shows each driver's grid position and brake test status with Pass/Fail badges

### Grid Sorting Rules

When a qualifying grid is active and policy is "demote":

1. **Enabled status** - Enabled drivers always sort before disabled
2. **Passing brake test** - Drivers with passing brake tests appear first, sorted by qualifying position (1, 2, 3...)
3. **Non-qualified drivers** - Drivers who didn't participate in qualifying appear next
4. **Failed brake test** - Drivers who failed (or have null results) appear at the back, sorted by best lap time (fastest first)

Note: Null/missing brake test results are treated as failures per the policy setting.

### Resetting or Updating the Grid

You have three options:

1. **Run another qualifying session** - Freezing new results completely replaces the old grid
2. **Delete the qualifying race** - In Results & Exports, delete the qualifying heat. The system automatically clears the frozen grid from the event config
3. **Manual config edit** - Advanced users can edit `events.config_json` in the database directly

### Persistence and Display

- **Database**: Grid data persists in `result_standings` table with `grid_index` and `brake_valid` columns
- **Event config**: Grid metadata stored in `events.config_json` as `{"qualifying": {"grid": [...]}}`
- **CSV exports**: Include `grid_index` and `brake_valid` columns in standings exports
- **Live display**: Both Standings and Seen tables on Race Control show proper grid order

### Troubleshooting

**Grid not applying to new race:**
- Check that you're loading a race in the same event as the qualifying session
- Verify the "Grid: frozen" indicator appears on Race Setup page
- Look for grid application log messages in server output

**Brake test badges not showing:**
- Make sure you set brake flags during/after qualifying using the brake button
- Check the Results page - it should show Pass/Fail badges for each driver
- Brake test data persists with the frozen results

**Grid positions show as blank:**
- This happens for results frozen before the grid feature was added
- Re-run qualifying and freeze again to populate grid_index properly

## 14. Diagnostics / Live Sensors

- Subscribes to `/diagnostics/stream` (Server-Sent Events).
- Controls: **Pause/Resume**, **Clear Log**, optional **Beep per detection**, and **RSSI** toggle.
- Buffer auto-trims to the last ~500 events.
- Use this view to confirm the reader is posting and to debug duplicate/min_lap filters.

## 15. Results & Exports

### View Modes
- **Live Preview** - Shows current race state while the heat is running; not for official publication. Updates in real-time.
- **Frozen Standings** - Official classification snapshot taken after checkered flag when leader crosses.
- **Toggle Pills** - Use the "Frozen" and "Live" pill buttons at the top of the page to switch between views. The system automatically shows which modes are available.

### CSV Exports
- **Laps CSV** - per-lap table for each entrant.
- **Events/Passes CSV** - raw journal export (visible only if journaling is enabled).
- File naming: `CCRS_<event>_<heat>_<YYYYmmdd-HHMMSS>.csv`

### Deleting Races
- Click the delete button for any frozen heat
- Requires confirmation: `?confirm=heat-{id}`
- **Auto-clears qualifying grids**: If you delete a qualifying race that was used to freeze a grid, the frozen grid is automatically cleared from the event config

### Copy/Download
Buttons provide clipboard copy or file download for quick posting.

---

## 16. Moxie Board Scoring (2025-11-13)

The Moxie Board is a fun crowd engagement feature at Power Racing Series events. It's a physical display board that shows which teams are getting the most "moxie points" from spectators and officials based on button presses.

### What is Moxie Scoring?

Unlike race results which are based on laps and times, moxie scoring is **pure button-press counting**. Spectators and officials press buttons on wireless controllers to give moxie points to their favorite teams. The more button presses a team gets, the higher their moxie score.

**Important:** Moxie scoring is completely separate from race results. It's about showmanship, creativity, and crowd appeal - not racing performance!

### How It Works

- A fixed pool of points (typically 300) is distributed among all active teams
- Each team's share is proportional to how many button presses they received
- The physical moxie board displays the top teams (usually 18-24 positions depending on the board)
- Scores update in real-time as people press buttons

**Example:**
If Team A gets 50 button presses and Team B gets 30 presses out of 100 total:
- Team A gets: (50 ÷ 100) × 300 = 150 moxie points
- Team B gets: (30 ÷ 100) × 300 = 90 moxie points

### Enabling Moxie Board

To turn on moxie board features:

1. **Open your config file** (`config/config.yaml`)
2. **Find the moxie section** under `app.engine.scoring`:
   ```yaml
   app:
     engine:
       scoring:
         moxie:
           enabled: true              # Turn this on!
           auto_update: true          # Real-time updates
           total_points: 300          # Point pool to distribute
           board_positions: 20        # How many teams fit on your board
   ```
3. **Save and restart** the backend server

### Using the Moxie Board Page

Once enabled, you'll see a **Moxie Board** button on the main operator page (between "Setup & Devices" and "Open Spectator View").

**Current Features:**
- View moxie scores for all active teams
- See which teams are displayed on the physical board
- Monitor button press counts in real-time

**Coming Soon:**
- Manual score adjustments
- Export moxie scores with race results
- Historical moxie scoring data

### Configuration Options

**`total_points`** - The point pool to distribute (typically 300)
- Smaller events might use 100 or 200
- Larger events might use 500 or more
- This is just for display - the actual button press counts are what matter

**`board_positions`** - How many teams your physical board can show
- 18 positions: Smaller/compact boards
- 20 positions: Standard PRS setup
- 24 positions: Larger events

Only the top N teams (by button presses) get displayed on the physical board, but the operator UI shows everyone's scores.

### Tips for Running Moxie

- Announce moxie scoring during driver introductions so people know to vote
- Remind spectators to keep voting throughout the event
- Watch for technical creativity, paint jobs, and driver showmanship - that's what moxie is about!
- Moxie awards are typically separate from race trophies

### Troubleshooting

**Moxie Board button doesn't appear:**
- Check that `moxie.enabled: true` in your config
- Restart the backend after changing config
- Verify at `/config/ui_features` that moxie_board.enabled returns true

**Scores not updating:**
- Confirm `auto_update: true` in config
- Check that button press hardware is connected and communicating
- Look for errors in server logs

---

## 17. Appendix - Versioned Settings Summary

Relevant keys (see `config/config.yaml`):
```yaml
app:
  engine:
    default_min_lap_s: 10          # global minimum lap threshold
    unknown_tags:
      allow: true                   # create provisional "Unknown" entrants
    persistence:
      enabled: true
      sqlite_path: backend/db/laps.sqlite
      journal_passes: true
      checkpoint_s: 15              # snapshot interval

scanner:
  source: ilap.serial               # ilap.serial | mock | ilap.udp
  decoder: ilap_serial              # references hardware decoder config
  min_tag_len: 7                    # reject shorter tags
  duplicate_window_sec: 0.5         # suppress duplicate reads
  rate_limit_per_sec: 20            # max passes per second

publisher:
  mode: http                        # how scanner publishes to backend

app:
  hardware:
    decoders:
      ilap_serial:
        port: COM3
        baudrate: 9600
        init_7digit: true
    
    pits:
      enabled: false
      receivers:
        pit_in: []
        pit_out: []

ui:
  theme: default-dark
  visible_rows: 16                  # standings viewport height
```


_Last updated: 2025-11-13_
