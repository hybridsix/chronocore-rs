# ChronoCore Technical Reference Guide
This document provides a comprehensive reference to the design, architecture, and operation of the ChronoCore Race Software.  
Lightweight summaries are included for quick readers, while detailed flows and appendices support deeper study.

---

## 1. System Overview
ChronoCore is a real‑time race timing and classification system designed for club and maker events.
It ingests timing passes from multiple decoder types, scores laps with duplicate/min‑lap filtering,
tracks flags and the race clock, and exposes live state to operator and spectator UIs. The engine is
authoritative in RAM with an optional SQLite event journal (replayable via checkpoints). Results can be
exported as per‑lap and raw‑event CSVs.

---

## 2. Architecture
The system comprises:
- **Backend Engine (FastAPI/Starlette)** - race state machine, pass ingestion, standings, journaling.
- **Decoder workers** - serial/TCP adapters transforming vendor lines into `ingest_pass()` calls.
- **SQLite persistence (optional)** - append‑only journal of events plus periodic `checkpoint` snapshots for crash recovery.
- **Operator & Spectator UIs** - static web clients polling `/race/state`; operator screens include control surfaces and exports.
- **Diagnostics SSE** - `/diagnostics/stream` publishes raw pass events for live debugging.

---

## 3. Decoder Subsystems

All hardware decoders present timing passes into the engine via a uniform interface:

```python
ENGINE.ingest_pass(tag=<str>, device_id=<optional str>, source="track")
```

The backend implements several decoder classes in `backend/decoder.py`. Each runs in its own thread, parses hardware output, and emits standardized passes.

### 3.1 Supported Decoder Types (2025)

| Mode              | Class                   | Transport  | Notes |
|-------------------|-------------------------|------------|-------|
| `ilap_serial`     | `ILapSerialDecoder`     | USB/serial | Default PRS decoder. SOH-framed lines beginning with `@`. Optional `init_7digit` command resets decoder clock. |
| `ambrc_serial`    | `AMBRcSerialDecoder`    | USB/serial | AMB/MyLaps legacy protocol. Accepts CSV, key=val, or regex-defined formats. |
| `trackmate_serial`| `TrackmateSerialDecoder`| USB/serial | Trackmate IR system. Typically `<tag>` or `<decoder>,<tag>`. |
| `cano_tcp`        | `CANOTcpDecoder`        | TCP line   | Covers CANO protocol (used by some DIY and Impinj Speedway bridges). One line per pass. |
| `tcp_line`        | `TCPLineDecoder`        | TCP line   | Alias for CANO TCP, kept for backward compatibility. |
| `mock`            | `MockDecoder`           | internal   | Emits a fake tag on a timer; useful for testing without hardware. |

### 3.2 Configuration (`config/config.yaml`)

Example:

```yaml
app:
  hardware:
    decoders:
      ilap_serial:
        port: COM3
        baudrate: 9600
        init_7digit: true

scanner:
  source: ilap.serial
  decoder: ilap_serial
  role: track
  min_tag_len: 7
  duplicate_window_sec: 0.5
  rate_limit_per_sec: 20
  
  serial:
    port: COM3
    baud: 9600
  
  udp:
    host: 0.0.0.0
    port: 5000

publisher:
  mode: http
  http:
    base_url: http://127.0.0.1:8000
    timeout_ms: 500
```

- **Switching decoders**: Change `scanner.source` to the desired type and set `scanner.decoder` to reference the appropriate `app.hardware.decoders` key.  
- **Serial devices**: Confirm `port` is correct for the host system (Windows: `COM3`, Linux: `/dev/ttyUSB0`).  
- **TCP/UDP devices**: Configure `host` and `port` under scanner.udp or publisher.http sections.  
- **Regex hook**: For AMBrc variants, add `line_regex` under the decoder configuration with named groups `(?P<tag>...)` and optionally `(?P<decoder>...)`.  
- **Pit routing**: Configure `app.hardware.pits.receivers.pit_in_receivers` and `pit_out_receivers` with device_id lists to auto-classify passes.

---

## 4. Race Engine

### 4.1 Logical Flow
The following describes the authoritative race loop and how it processes events in real time.

**ChronoCore Race Engine - Logical Flow**

1. **Initialization (`engine.load`)**  
   The operator creates a new race with a unique `race_id` and roster of entrants.  
   Each entrant has: `entrant_id`, enabled/disabled flag, status (`ACTIVE`, `DISABLED`, `DNF`, `DQ`),  
   a car number, a name, and optionally a tag UID.  
   Only enabled entrants are eligible to score laps. The engine builds a race-local tag → entrant map.

2. **Flags and Clock (`engine.set_flag`)**  
   Race control sets flags (pre, green, yellow, red, white, checkered).  
   - First transition to green starts the race clock (monotonic timer).  
   - **First lap fix (2025-10-31)**: When green flag is set, all entrant crossing timestamps are cleared (`_last_hit_ms = None`) to prevent artificially short first laps caused by pre-race parade lap crossings.  
   - Red flag: laps still count, but marshals manage discipline.  
   - Yellow flag: no special logic; cars may pass if needed.  
   - Checkered: when the leader next crosses, the race clock and standings freeze.

3. **Pass Ingestion (`engine.ingest_pass`)**  
   A pass arrives with `{ tag, ts_ns?, source, device_id? }`.  
   - If the tag belongs to an enabled entrant, it is processed.  
   - If unknown and `auto_provisional = true`, a new provisional entrant is created as `"Unknown ####"`.  
   - If the entrant is disabled, the pass is ignored.  
   - **Track passes**: compute lap time, apply filters (`min_lap_s`, `min_lap_dup`), update `laps/last/best/pace_5`.  
   
   **Lap Crediting Logic (2025-10-31 race weekend fix):**
   - **First crossing after GREEN**: Sets the start mark (`_last_hit_ms`). No lap credited yet - this is the "arming" pass.
   - **Second and subsequent crossings**: Calculate delta time since last crossing.
     - If delta < `min_lap_dup` (default 1.0s): Rejected as duplicate, no lap credited
     - If delta < `min_lap_s` (default 5.0s): Rejected as too fast, no lap credited  
     - If delta >= `min_lap_s`: **Lap credited**, increment lap counter, update last/best/pace times
   - **Best lap tracking**: If credited lap time < current best (or best is null), update `best_s`
   - **Pre-race crossings**: Any passes during PRE/COUNTDOWN are ignored for lap counting but visible in diagnostics
   - **Green flag reset**: When transitioning to GREEN, all `_last_hit_ms` timestamps are cleared to ensure first racing lap has accurate timing
   
   - **Pit passes** (if pit_timing enabled): `pit_in` starts a pit window; `pit_out` closes the window, computes pit time, increments `pit_count`.

4. **Standings Calculation (`engine.snapshot`)**  
   Called for `/race/state` endpoint.  
   - Updates clock (unless frozen).  
   - Entrants are ordered: `laps desc → best asc → last asc → entrant_id asc`.  
   - Each entrant snapshot includes: `laps`, `last`, `best`, `pace_5`, `gap_s`, `lap_deficit`, `pit_count`, `last_pit_s`.  
   - Snapshot also reports global state (`flag`, `race_id`, `clock`, `running`, `last_update_utc`).

5. **Event Logging (Journal)**  
   If persistence is enabled, every pass, flag change, and roster change is appended to SQLite as an event row.  
   - Every *N* seconds (`checkpoint_s`), a full snapshot is written as a checkpoint.  
   - On crash recovery: load last checkpoint, replay events to rebuild state.

6. **Roster Management**  
   Entrants can be enabled/disabled mid-race (`enabled = false` → passes ignored).  
   - Status (`ACTIVE`, `DISABLED`, `DNF`, `DQ`) can be updated mid-race for classification.  
   - Tags can be reassigned mid-race (useful for swapping transponders).

7. **Post-Race Analysis**  
   `/race/state` gives the live, authoritative view.  
   The SQLite journal provides historical detail for exports, replay, and audits.  
   Operator can merge or reassign provisionals to real entrants without losing data.

**Summary**  
The RaceEngine keeps live scoring fast and authoritative in RAM, while optional persistence ensures data durability.  
Standings are always consistent, flag changes are logged, and provisional entrants capture surprises.  
This design balances speed, safety, and flexibility for real-world race control.

---

## 5. Persistence and Recovery

ChronoCore uses an SQLite event journal to ensure data durability and enable crash recovery. Every significant event (pass, flag change, roster modification) is logged as an event row in the `race_events` table.

### 5.1 Journal Tables

**`race_events`**: Append-only log of all race events
- `race_id` - associates event with a specific race session
- `ts_utc` - UTC epoch milliseconds when event occurred
- `clock_ms` - race clock position (milliseconds since GREEN)
- `type` - event type (`pass`, `flag_change`, `entrant_enable`, `assign_tag`)
- `payload_json` - event-specific data (tag, flag value, etc.)

**`race_checkpoints`**: Periodic full snapshots
- `race_id` - race session identifier
- `ts_utc` - when checkpoint was written
- `clock_ms` - race clock at checkpoint time
- `snapshot_json` - complete engine state (entrants, standings, flags, etc.)

### 5.2 Checkpoint Strategy

- **Frequency**: Every N seconds (default: 15s, configurable via `app.engine.persistence.checkpoint_s`)
- **Trigger**: Automatic background task in the engine
- **Content**: Full race state including all entrant lap histories, times, and current flag

### 5.3 Recovery Process

On restart after a crash:
1. Load the most recent checkpoint from `race_checkpoints`
2. Replay all events from `race_events` that occurred after the checkpoint timestamp
3. Reconstruct exact race state as if the crash never happened

This ensures zero data loss as long as SQLite WAL (Write-Ahead Logging) is enabled.

### 5.4 Batch Writing

To minimize I/O overhead, events are batched:
- **Time window**: Default 200ms (`batch_ms`)
- **Count limit**: Default 50 events (`batch_max`)
- Events are flushed when either limit is reached

**Configuration:**
```yaml
app:
  engine:
    persistence:
      batch_ms: 200
      batch_max: 50
      fsync: true  # force filesystem sync on each batch
```

This persistence layer balances speed and reliability, providing a durable record for audits and post-race analysis.

---

## 6. Results Semantics

- **Freeze (operator action)**: take a point-in-time snapshot for review/exports while a race may still be live.
- **Frozen (engine state)**: after **checkered**, the next leader crossing locks classification and clock.
- **Publication**: Only **Frozen Standings** are official; **Live Preview** is labeled as such.
- **View Mode Toggle (2025-11-02)**: Results page supports switching between frozen (official) and live (preview) views via pill buttons. The UI probes availability of both modes and enables pills accordingly. Frozen view fetches from `/results/{id}` and `/results/{id}/laps`; live view normalizes `/race/state` to match the frozen format for consistent rendering.

### 6.1 Qualifying Workflow and Grid Freezing (2025-11-03)

ChronoCore supports a complete qualifying workflow where grid position is determined by best lap time and persisted for subsequent races in the same event.

**Workflow:**
1. Set up a race with `race_type: "qualifying"` in Race Setup
2. Run the qualifying session normally (drivers post laps, best times are tracked)
3. Optionally set brake test flags for each entrant during/after qualifying
4. When checkered flag is thrown, a "Freeze Grid Standings" button appears on Race Control
5. Operator clicks "Freeze Grid" and chooses a brake test policy
6. Grid order is frozen and saved to `events.config_json`

**Data Model:**

Grid data is stored in two places:

1. **Event Config** (`events.config_json`):
```json
{
  "qualifying": {
    "grid": [
      {
        "entrant_id": 23,
        "order": 1,
        "best_ms": 4210,
        "brake_ok": true
      },
      ...
    ]
  }
}
```

2. **Result Tables** (`result_standings`, `result_laps`):
- `result_standings.grid_index` (INTEGER) - qualifying position (1, 2, 3...)
- `result_standings.brake_valid` (INTEGER) - brake test result (1=pass, 0=fail, NULL=no test)

**Grid Application:**

When loading a subsequent race in the same event:

1. Backend reads qualifying grid from `events.config_json`
2. For each entrant in the roster:
   - Adds `grid_index` field with their qualifying position
   - Adds `brake_valid` field with their brake test result
3. Applies brake test policy and sorts entrants:
   - **Policy: "demote"** (default):
     - Passing brake test: sort by grid_index ascending
     - No grid position: sort by entrant_id
     - Failed/null brake test: demoted to back, sorted by best_ms ascending
   - **Policy: "warn"**: Grid order preserved, brake status shown as badge only
4. Sorted entrant list is passed to `ENGINE.load()`
5. Engine stores `grid_index` and `brake_valid` on each Entrant object

**UI Display:**

- **Race Control**: Standings and Seen tables sort by grid_index when present
- **Race Setup**: Shows "Grid: frozen" indicator when qualifying grid is active
- **Results Page**: Displays grid_index column and brake test badges (Pass/Fail)
- **CSV Exports**: Include grid_index and brake_valid columns

**Grid Persistence Flow:**

```
Qualifying Race
   ↓
Checkered Flag
   ↓
Freeze Grid (operator action)
   ↓
Save to result_standings (grid_index, brake_valid)
   ↓
Save to events.config_json (qualifying.grid array)
   ↓
Load Next Race
   ↓
Read grid from config_json
   ↓
Apply grid_index + brake_valid to entrants
   ↓
Sort by policy
   ↓
ENGINE.load(sorted_entrants)
   ↓
Engine.snapshot() includes grid_index/brake_valid
   ↓
UI displays in grid order
```

**Grid Reset:**

Three ways to clear a frozen grid:
1. Run another qualifying session and freeze new results (overwrites)
2. Delete the qualifying race from Results page (auto-clears grid)
3. Manual edit of `events.config_json` (advanced)

**Important Implementation Details:**

- `Entrant.__slots__` includes `grid_index` and `brake_valid` to allow storage
- `Entrant.__init__()` accepts these fields as optional parameters
- `Entrant.as_snapshot()` includes these fields in the returned dict
- `_state_seen_block()` includes grid metadata in seen.rows for frontend sorting
- Null brake test results are treated as failures per the "demote" policy
- Grid sorting happens before ENGINE.load(), not in the engine itself

**Configuration:**

```yaml
app:
  engine:
    qualifying:
      brake_test_policy: demote  # demote | warn
```

---

## 7. Database Schema

The core race data is stored in an SQLite database managed by `backend/db_schema.py`. The schema supports both real-time race execution and post-race analysis.

### 7.1 Core Roster Table

**`entrants`**: Central roster table
- `entrant_id` (PK) - unique identifier
- `number` (TEXT) - car number (supports formats like "004", "A12")
- `name` (TEXT, NOT NULL) - team/driver name
- `tag` (TEXT) - transponder UID (nullable when unassigned)
- `enabled` (INTEGER, DEFAULT 1) - 1=active, 0=disabled
- `status` (TEXT, DEFAULT 'ACTIVE') - ACTIVE | DISABLED | DNF | DQ
- `organization`, `spoken_name`, `color`, `logo` - optional metadata
- `updated_at` (INTEGER) - last modification timestamp

**Unique Constraint**: Partial UNIQUE index ensures tags are unique among **enabled** entrants only:
```sql
CREATE UNIQUE INDEX idx_entrants_tag_enabled_unique
ON entrants(tag)
WHERE enabled = 1 AND tag IS NOT NULL;
```

This allows disabled entrants to retain historical tags while preventing conflicts among active roster.

### 7.2 Timing Infrastructure

**`locations`**: Logical timing points
- `location_id` (PK, TEXT) - short ID ('SF', 'PIT_IN', 'X1')
- `label` (TEXT) - human-readable label ('Start/Finish', 'Pit In')

**`sources`**: Physical decoder bindings
- `source_id` (PK) - auto-increment ID
- `computer_id` (TEXT) - host identifier
- `decoder_id` (TEXT) - reader device ID
- `port` (TEXT) - communication port ('COM7', 'udp://0.0.0.0:5001')
- `location_id` (FK) - references locations(location_id)
- UNIQUE constraint on (computer_id, decoder_id, port)

**`passes`**: Raw detection journal (when journaling enabled)
- `pass_id` (PK) - auto-increment
- `ts_ms` (INTEGER) - host epoch milliseconds
- `tag` (TEXT) - transponder ID
- `t_secs` (REAL) - optional decoder timestamp
- `source_id` (FK) - references sources(source_id)
- `raw` (TEXT) - original packet for forensics
- `meta_json` (TEXT) - optional metadata (channel, RSSI, etc.)

### 7.3 Race Model

**`events`**: Event/weekend container
- `event_id` (PK)
- `name`, `date_utc` - event identity
- `config_json` (TEXT) - event-wide settings (qualifying rules, etc.)

**`heats`**: Individual race sessions
- `heat_id` (PK)
- `event_id` (FK) - parent event
- `name` - heat identifier ("Heat 1", "Feature", etc.)
- `order_index` - display ordering
- `config_json` (TEXT) - heat-specific rules (duration, min_lap_ms)

**`lap_events`**: Authoritative lap records
- `lap_id` (PK)
- `heat_id` (FK) - which race
- `entrant_id` (FK) - which driver
- `lap_num` (INTEGER) - 1-based lap number
- `ts_ms` (INTEGER) - when lap was credited
- `source_id` (FK) - where it was detected
- `inferred` (INTEGER) - 0=real, 1=predicted/inferred
- `meta_json` (TEXT) - inference metadata

**`flags`**: Race control state log
- `flag_id` (PK)
- `heat_id` (FK)
- `state` (TEXT) - GREEN | YELLOW | RED | WHITE | CHECKERED | BLUE
- `ts_ms` (INTEGER) - when flag changed
- `actor`, `note` - who/why

### 7.4 Frozen Results

**`result_standings`**: Final classification
- `race_id`, `position` (PK composite)
- `entrant_id`, `number`, `name`, `tag`
- `laps`, `last_ms`, `best_ms`, `gap_ms`, `lap_deficit`
- `pit_count`, `status`
- `grid_index` (INTEGER, nullable) - qualifying position (1, 2, 3...) when applicable
- `brake_valid` (INTEGER, nullable) - brake test result (1=pass, 0=fail, NULL=no test)

**`result_laps`**: Lap-by-lap history
- `race_id`, `entrant_id`, `lap_no` (PK composite)
- `lap_ms` - lap duration in milliseconds
- `pass_ts_ns` - optional original pass timestamp

**`result_meta`**: Results metadata
- `race_id` (PK)
- `race_type` - sprint | endurance | qualifying
- `frozen_utc` - ISO8601 timestamp when results froze
- `duration_ms` - total race duration
- `clock_ms_frozen`, `event_label`, `session_label`, `race_mode` - extended metadata

**Schema Evolution:**
- `grid_index` and `brake_valid` columns added via migration (2025-11-03)
- Migration script: `backend/migrations/add_qualifying_columns.py`
- Older frozen results will have NULL values for these columns
- Re-running qualifying and freezing will populate them correctly

### 7.5 Convenience Views

**`v_passes_enriched`**: Passes with location labels
**`v_lap_events_enriched`**: Laps with team names and locations  
**`v_heats_summary`**: Heats with aggregated counts and timing

These views simplify UI queries by pre-joining common lookups.

### 7.6 Schema Management

Schema is created/validated at startup via `backend/db_schema.ensure_schema()`:
- Idempotent table creation (IF NOT EXISTS)
- Index creation with conflict resolution
- Foreign key definitions (requires `PRAGMA foreign_keys=ON` at runtime)
- User version tracking for migrations (`PRAGMA user_version`)

**Important:** The schema is forward-only. Legacy multi-file configs are not supported.

---

## 8. API Endpoints

ChronoCore exposes a set of REST endpoints via FastAPI. Below is a detailed reference of each endpoint, including methods, parameters, responses, and key notes.

### 8.1 API Reference Table

| Endpoint          | Method | Params / Body                                  | Response                                            | Notes                                                                 |
|-------------------|--------|------------------------------------------------|-----------------------------------------------------|-----------------------------------------------------------------------|
| `/race/state`     | GET    | None                                           | `{ race_id, race_type, flag, running, clock_ms, ... }` | Returns the authoritative snapshot of current race state.            |
|                   |        |                                                | `standings: [ { entrant_id, tag, number, ... } ]`| UIs poll this at ~3 Hz for live updates.                              |
|                   |        |                                                | `last_update_utc, features`                         |                                                                       |
| `/engine/flag`    | POST   | `{ "flag": "pre" | "green" | "yellow" ... }` | `{ "ok": true }`                                     | Sets the current race flag. `green` starts the clock; `checkered` freezes it. `blue` is informational only. |
| `/engine/pass`    | POST   | `{ tag, ts_ns?, source, device_id? }`          | `{ ok, entrant_id, lap_added, lap_time_s, reason }` | Ingests a timing pass. Adds a lap if Δt ≥ min_lap_s (default ~5.0s).  |
| `/engine/load`    | POST   | `{ race_id, entrants: [ ... ] }`               | `{ "ok": true }`                                     | Initializes a new race session with a given roster.                   |
| `/engine/snapshot`| GET    | None                                           | Same as `/race/state` response                      | Alias for `/race/state`.                                              |

### 8.2 Spectator UI Contract
- **Single source of truth:** `/race/state` only.
- **Flag classes:** `.flag.flag--{lowercase_color}` plus modifiers:
  - `.flag.is-pulsing` for attention states (yellow, red, blue, checkered).
  - `.flag.flag--green.flash` one-shot on entering green.
- **Accessibility:** Banner label shows `"Color - Meaning"` (e.g., *“White - Final Lap”*, *“Blue - Driver Swap”*).

### 8.3 Static Pathing
- FastAPI mounts UI at `/ui`.
- UI assets live under `ui/` with operator pages at `/ui/operator/*.html`.

---

## Enabled-Only Tag Uniqueness & API Contracts (2025-10-04)

## Summary of changes
- **Enabled‑only uniqueness:** Transponder tags are unique among **enabled** entrants (`enabled=1 AND tag IS NOT NULL`), so disabled entrants can keep historical tags.
- **Self‑healing schema:** On startup, `ensure_schema()` drops any legacy non‑unique tag index and recreates the correct **UNIQUE partial index**.
- **Idempotent tag assignment:** `/engine/entrant/assign_tag` returns **200** if the value is unchanged; otherwise writes through to SQLite after conflict checks.
- **Hardened loader:** `/engine/load` rejects malformed payloads with **400** and explanatory messages.
- **Config path precedence:** DB defaults to `backend/db/laps.sqlite`, overridable in `config/app.yaml`.
- **Ops probes:** `/healthz` (liveness) and `/readyz` (DB readiness).

---

## Database - schema and constraints

### Partial UNIQUE index (enabled‑only)
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_entrants_tag_enabled_unique
ON entrants(tag)
WHERE enabled = 1 AND tag IS NOT NULL;
```

### Bootstrap behavior
`ensure_schema(db_path)` (idempotent):
1. Creates tables if missing.
2. **Drops** any old `idx_entrants_tag_enabled_unique`.
3. **Recreates** the UNIQUE partial index above.
4. Updates `PRAGMA user_version` for light migrations.

---

## DB path resolution
Order of precedence:
1. `config/app.yaml` → `app.engine.persistence.db_path` (absolute or relative).
2. Legacy default: `backend/db/laps.sqlite`.

Relative paths resolve against repo root unless `app.paths.root_base` is provided.

---

## API contracts

### `POST /engine/load`
**Purpose:** Load the runtime session roster (engine mirror).  
**Request:**
```json
{
  "race_id": 1,
  "entrants": [
    { "id": 34, "name": "Circuit Breakers", "number": "7", "tag": "1234567", "enabled": true },
    { "id": 12, "name": "Thunder Lizards",  "number": "42", "tag": null,      "enabled": false }
  ]
}
```
**Validation & normalization:**
- `race_id` required and int‑able.
- `entrants` must be a list of objects with int‑able `id`.
- Tags normalize: empty/whitespace → `null`.

**Responses:**
- `200` `{ "ok": true, "entrants": [ids...] }`
- `400` on malformed payload (e.g., missing/invalid `id`).

---

### `POST /engine/entrant/assign_tag`
**Purpose:** Assign or clear a tag for a single entrant (DB write‑through + engine mirror).  
**Request:** `{ "entrant_id": 34, "tag": "1234567" }` (use `""` to clear).  
**Semantics:**
- **Idempotent**: same tag to same entrant → `200`, engine kept in sync.
- **Conflicts**: checks **enabled** entrants and **excludes the incumbent** row.

**Responses:**
- `200` on success or no‑op.
- `404` if entrant id not present in DB.
- `412` if runtime session not loaded with this entrant.
- `409` if another enabled entrant holds the tag.

---

### `GET /admin/entrants`
List authoritative DB entrants.  
**Response:** array of `{ id, number, name, tag, enabled, status }`.

### `POST /admin/entrants`
Upsert entrants into the DB with conflict enforcement.  
**Request:**
```json
{
  "entrants": [
    { "id": 34, "number": "7", "name": "Circuit Breakers", "tag": "1234567", "enabled": true,  "status": "ACTIVE" },
    { "id": 12, "number": "42","name": "Thunder Lizards",  "tag": null,      "enabled": false, "status": "ACTIVE" }
  ]
}
```
**Rules:**
- App‑level conflict check: enabled‑only uniqueness (excluding the same `id`).
- DB layer enforces the same with the UNIQUE partial index.

**Responses:**
- `200` `{ "ok": true, "count": N }`
- `400` on shape/validation issues.
- `409` on tag conflict (another enabled entrant has the tag).
- `500` on transactional failure.

---

## Conflict detection (reference SQL)
When assigning `:tag` to entrant `:id`:
```sql
SELECT 1
FROM entrants
WHERE enabled = 1
  AND tag = :tag
  AND entrant_id != :id
LIMIT 1;
-- Any row → conflict
```

---

## Test matrix (must‑pass)
1. **Idempotence:** Same tag to same entrant twice → `200` / `200`.
2. **Conflict (enabled‑only):** Enable a different entrant with same tag → `409`.
3. **Loader robustness:** Missing `id` or non‑int `id` → `400` with explicit message.
4. **Seatbelt:** Two enabled entrants with same tag at SQL level → UNIQUE partial index rejects.

---

## Notes for UI integration
- Map `409` to a clear user message that identifies the conflicting entrant when possible.
- Map `412` to “Load roster first” guidance with a one-click reload action.
- Surface DB path from `/readyz` in an “About / Diagnostics” panel to speed up support.

### 8.4 Flag State Machine & Countdown (2025-10-21)

The race controller maintains both a **phase** (coarse lifecycle) and a **flag** (operator-visible color). API consumers and UIs must respect the state machine below to avoid illegal transitions and to keep the race clock aligned with race control decisions.

### Phase overview
- `pre`: grid-up / warm-up. Clock stopped. Operator may jump directly to `green`.
- `countdown`: optional arming period. Clock stopped, UI shows an arming timer. Only `pre` is accepted during this phase; the scheduler promotes to `green` automatically when the timer expires.
- `green`: main racing phase. Clock running. All field flags are legal, and calling `green` again is idempotent.
- `white`: final lap window. Semantics mirror `green`, but UI may highlight the banner.
- `checkered`: race frozen. Clock and classification lock until the session resets or a new race loads.

### 8.4.1 Soft-End Mode (2026-01-24)

Soft-end mode decouples the **visual finish** (CHECKERED flag) from **race completion** (final freeze). This allows drivers to complete their current lap after the time/lap limit is reached, creating more natural race finishes and accurate lap counts.

**Key Concepts:**

- **WHITE Flag** = Warning indicator at traditional times (T-60s for time races, lap N-1 for lap races)
- **CHECKERED Flag** = Visual finish marker at limit (T=0 or lap N), triggers lights/sounds
- **Soft-End Window** = Configurable timeout (default 30s) after CHECKERED where lap counting continues
- **finish_order** = Sequential counter tracking crossing order after limit reached
- **Race Freeze** = Automatic closure after soft_end_timeout_s expires

**Behavior Comparison:**

| Aspect | Hard-End (soft_end: false) | Soft-End (soft_end: true) |
|--------|---------------------------|---------------------------|
| WHITE flag timing | T-60s / lap N-1 | T-60s / lap N-1 (same) |
| CHECKERED flag timing | T=0 / lap N | T=0 / lap N (same) |
| Lap counting after CHECKERED | Stops immediately | Continues for timeout period |
| Race freeze | Immediate on CHECKERED | After soft_end_timeout_s expires |
| Finish order tracking | Not used | Sequential crossing order |
| Standings sort | Laps → best → last | Laps → finish_order → best → last |

**Configuration (config/race_modes.yaml):**

```yaml
sprint_10_laps:
  label: "10 Lap Sprint"
  min_lap_s: 5.0
  limit:
    type: laps
    value: 10
    soft_end: true              # Enable soft-end mode
    soft_end_timeout_s: 30      # Timeout in seconds (default: 30)
  scoring:
    method: position

endurance_30min:
  label: "30 Minute Endurance"
  min_lap_s: 8.0
  limit:
    type: time
    value_s: 1800               # 30 minutes
    soft_end: true
    soft_end_timeout_s: 45      # Longer timeout for endurance
  scoring:
    method: laps_then_time
```

**Engine State Tracking:**

```python
# Added to RaceEngine.reset():
self.soft_end: bool = False                          # Mode flag from config
self.soft_end_timeout_s: int = 30                    # Configurable timeout
self._checkered_flag_start_ms: Optional[int] = None  # Track CHECKERED start

# Added to Entrant:
self.finish_order: Optional[int] = None              # Crossing position after limit
self.soft_end_completed: bool = False                # Has entrant finished final lap?
```

**Automatic Flag Behavior:**

1. **WHITE Flag (Warning)**:
   - Time races: Thrown automatically at T-60s (if race ≥ 60s total)
   - Lap races: Thrown automatically when leader reaches lap N-1
   - Behavior identical for both soft-end and hard-end modes
   - Operator can manually throw WHITE at any time

2. **CHECKERED Flag (Finish)**:
   - Time races: Thrown automatically at T=0 (clock reaches limit)
   - Lap races: Thrown automatically when leader crosses at lap N
   - Triggers lights/sounds via OSC integration
   - Records `_checkered_flag_start_ms` for timeout tracking

**Lap Counting Logic After CHECKERED:**

```python
# In ingest_pass():
if self.flag == "checkered" and not self.soft_end:
    return {"ok": True, "reason": "checkered_freeze"}  # Hard-end: stop immediately

if self.soft_end and self.flag == "checkered" and ent.soft_end_completed:
    return {"ok": True, "reason": "soft_end_completed"}  # Entrant already finished

# Count lap and track finish order:
ent.laps += 1
if self.flag == "checkered" and ent.finish_order is None:
    self._finish_order_counter += 1
    ent.finish_order = self._finish_order_counter
    if self.soft_end:
        ent.soft_end_completed = True  # Block future laps for this entrant
```

**Timeout Enforcement:**

```python
# In _update_clock():
if (self.flag == "checkered"
    and self.soft_end
    and self._checkered_flag_start_ms is not None
    and self.soft_end_timeout_s > 0):
    
    checkered_duration_ms = self.clock_ms - self._checkered_flag_start_ms
    timeout_ms = self.soft_end_timeout_s * 1000
    
    if checkered_duration_ms >= timeout_ms:
        # Freeze race after timeout expires
        self.running = False
        self.clock_start_monotonic = None
        self.clock_ms_frozen = self.clock_ms
```

**Standings Sort Order:**

```python
def sort_key(e: Entrant):
    best = e.best_s if e.best_s is not None else 9e9
    last = e.last_s if e.last_s is not None else 9e9
    finish = e.finish_order if e.finish_order is not None else 9e9
    
    if self.soft_end:
        # Soft-end: use finish_order as primary tiebreaker after laps
        return (-e.laps, finish, best, last, e.entrant_id)
    else:
        # Hard-end: traditional sort (no finish order)
        return (-e.laps, best, last, e.entrant_id)
```

**Race Timeline Example (10-lap race, 30s timeout):**

```
Lap 9:  WHITE flag thrown (leader reaches lap 9)
Lap 10: Leader crosses → CHECKERED flag thrown
        - Leader assigned finish_order = 1
        - Timer starts: _checkered_flag_start_ms = 600000
        - Race continues running (running = True)
+5s:    P2 crosses at lap 10 → finish_order = 2, soft_end_completed = True
+8s:    P3 crosses at lap 9 → finish_order = 3, soft_end_completed = True
+12s:   P4 crosses at lap 9 → finish_order = 4, soft_end_completed = True
+30s:   Timeout expires → Race freezes (running = False)
        - Final standings: sorted by (laps desc, finish_order asc)
```

**Persistence:**

- finish_order values are **not** persisted to database (runtime-only for UI sorting)
- soft_end configuration comes from race_modes.yaml and session config
- Final frozen results reflect lap counts and times as of timeout expiration
- Journal includes all lap events during soft-end window for post-race analysis

**Session Config Override:**

```python
# In /engine/load endpoint:
session_config = {
    "limit": {
        "type": "time",
        "value_s": 1200,       # 20 minutes
        "soft_end": False      # Override mode's soft_end setting
    }
}
# If soft_end key present in session_config, it overrides race mode config
# Otherwise, race mode config value is preserved
```

**UI Implications:**

- Standings continue updating during soft-end window
- Clock continues ticking until timeout expires
- finish_order field can be displayed in standings tables (if desired)
- "Race Running" indicator stays active until freeze
- OSC lighting: CHECKERED lights triggered at limit, not at timeout

### Allowed flag transitions

| Current phase | Accepted flags | Notes |
| --- | --- | --- |
| `pre` | `pre`, `green` | Countdown may be scheduled from `pre`; repeated `pre` calls are no-ops. |
| `countdown` | `pre` | UI uses this to abort a start. Timer fires `green` automatically; manual `green` calls are acknowledged but ignored. |
| `green` | `green`, `yellow`, `red`, `blue`, `white`, `checkered` | Returning to `green` is always legal so marshals can clear incidents quickly. |
| `white` | `green`, `yellow`, `red`, `blue`, `white`, `checkered` | Identical to `green` apart from banner styling. |
| `checkered` | `checkered` | Engine refuses other colors until the session resets; repeated `checkered` is idempotent. |

Duplicate submissions return **200 OK** with `flag` unchanged so callers can treat `/engine/flag` as idempotent.

### API contract (`POST /engine/flag`)
```json
{ "flag": "green" }
```

- `flag` is case-insensitive on input but normalized to uppercase in `/race/state` snapshots.
- Optional countdown: `{ "flag": "green", "countdown_s": 5 }` enters phase `countdown` and returns the projected start time in `countdown_target_utc`.
- Responses
  - `200` `{ "ok": true, "flag": "GREEN", "phase": "green" }`
  - `400` invalid flag token
  - `409` attempting to exit `checkered` without resetting the session
- Manual `green` requests during countdown acknowledge with `200` but the scheduler still controls the actual transition.

### Countdown semantics
- Timer runs server-side via `time.monotonic_ns`; backend restarts cancel the countdown and drop the phase back to `pre`.
- `/race/state` exposes `phase`, `flag`, `countdown_remaining_ms`, and `green_at_utc` while armed.
- If the countdown expires while operator latency is high, the engine still promotes to `green` and begins scoring laps immediately.

### UI contract
- Operator UI polls `/race/state` at ~250 ms for two seconds after a flag change to keep the banner responsive.
- Disable buttons whose actions would violate the table above; server validation remains authoritative, but preventing illegal presses avoids confusing end users.
- Spectator UI maps `flag` to `.flag.flag--{color}` classes, with `.flag.is-pulsing` for `yellow`, `red`, `blue`, and `checkered`.

### Error semantics & observability
- `409 Conflict`: only emitted when a request attempts to leave `checkered`.
- `412 Precondition Failed`: runtime session not loaded; load entrants first.
- Every accepted change appends an event with `event_type="flag"` in the journal (`backend/lap_logger.py`). Include phase, flag, UTC timestamp, and operator ID if available.
- Metrics: `engine_flag_active{flag="GREEN"}=1` when green; `engine_flag_transitions_total` increments on each accepted change.

### Verification checklist
1. From `pre`, request `green` with a countdown; confirm `phase=countdown`, then `green` fires automatically.
2. Abort the countdown via `pre`; verify the timer cancels and `green_at_utc` disappears from `/race/state`.
3. From `green`, send `yellow` → `green`; ensure standings continue updating and responses stay `200`.
4. After `checkered`, attempt `green`; expect `409` with `phase="checkered"`.
5. Restart the backend mid-countdown; confirm phase resets to `pre` and no stale countdown remains.

---

### 8.5 Qualifying and Grid Freezing (2025-11-02)

ChronoCore supports qualifying sessions where best lap times determine starting grid order for subsequent races. The frozen grid is persisted in the event's config JSON and applies to all heats in that event.

**Endpoints:**

| Endpoint | Method | Body | Response | Notes |
|----------|--------|------|----------|-------|
| `/event/{event_id}/qual/freeze` | POST | `{ source_heat_id: int, policy: "demote"\|"use_next_valid"\|"exclude" }` | `{ event_id, qualifying: { source_heat_id, policy, grid: [...] } }` | Freezes grid from qualifying heat results |
| `/event/{event_id}/qual` | GET | None | `{ event_id, qualifying: {...} }` or `{ event_id, qualifying: null }` | Retrieves frozen grid for an event |
| `/results/{race_id}` | DELETE | `?confirm=heat-{race_id}` | `{ race_id, laps_deleted, standings_deleted, meta_deleted, cleared_qualifying_grid?: bool }` | Deletes frozen results; auto-clears qualifying grid if this was the source |

**Freeze Grid Logic:**

1. **Collect lap durations** - Extract all lap times from `lap_events` for the qualifying heat
2. **Load brake test verdicts** - Manual pass/fail flags stored in heat config JSON
3. **Calculate best lap per entrant:**
   - `brake_ok=true`: Use fastest lap
   - `brake_ok=false`:
     - `policy="use_next_valid"`: Use second-fastest lap
     - `policy="demote"`: Use fastest but sort to back
     - `policy="exclude"`: Remove from grid entirely
   - `brake_ok=null`: Use fastest lap (no penalty)
4. **Rank entrants** - Sort by `(excluded, demoted, best_ms)`
5. **Assign 1-based order** - Grid positions for each entrant
6. **Persist to event config** - Stored in `events.config_json` under `qualifying` key

**Event Config Structure:**
```json
{
  "qualifying": {
    "source_heat_id": 42,
    "policy": "demote",
    "grid": [
      {
        "entrant_id": 12,
        "order": 1,
        "best_ms": 23456,
        "brake_ok": true
      },
      {
        "entrant_id": 7,
        "order": 2,
        "best_ms": 23789,
        "brake_ok": true
      }
    ]
  }
}
```

**Grid Application:**

When loading a race (`/engine/load`), if `heats.event_id` has a frozen qualifying grid:
- Entrants in PRE/COUNTDOWN phases are sorted by `grid[].order`
- `grid_index` and `brake_valid` fields are included in `/race/state` standings
- During GREEN/CHECKERED, order follows actual race position (laps + time)

**Auto-Clear on Delete:**

When deleting frozen results via `DELETE /results/{race_id}`:
1. Backend checks if `race_id` matches any event's `qualifying.source_heat_id`
2. If match found, sets `qualifying: null` in event config
3. Response includes `"cleared_qualifying_grid": true`
4. Prevents orphaned qualifying data from deleted heats

**UI Integration:**

- **Race Control**: After checkered flag on qualifying races, "Freeze Grid Standings" button appears with breathing animation
- **Results Page**: View toggle pills (Frozen/Live) allow switching between official results and live preview
- **Race Setup**: Shows "Grid: frozen" indicator when qualifying order is active

**Notes:**
- Brake test verdicts are optional; if not set, entrant uses fastest lap with no penalty
- Re-freezing from a different qualifying heat overwrites the previous grid completely
- Frozen grids persist across backend restarts (stored in SQLite `events.config_json`)
- Grid order only affects PRE/COUNTDOWN sorting; race results are always based on actual performance

---

## 10. Frontend Clients

### 10.1 Polling Strategy

The Operator and Spectator UIs are static HTML/CSS/JS clients that poll the `/race/state` endpoint for live updates.

**Standard polling rate:** ~3 Hz (every ~333ms) during normal operation.

**Adaptive polling:** After a flag change, the Operator UI polls at ~250ms for 2 seconds to ensure the banner updates appear immediately.

**Connection status:** The UI footer displays connection health:
- "OK" - receiving valid responses
- "Connecting..." - initial startup
- "Disconnected - retrying..." - network error or server unreachable

### 10.2 State Synchronization

Both UIs consume the same `/race/state` snapshot which includes:
- `flag` - current race flag (PRE, GREEN, YELLOW, RED, BLUE, WHITE, CHECKERED)
- `phase` - lifecycle phase (pre, countdown, green, white, checkered)
- `clock_ms` - race clock in milliseconds (negative during countdown)
- `countdown_remaining_s` - seconds until auto-green (during countdown phase)
- `standings` - ordered array of entrant objects with laps, times, gaps
- `running` - boolean indicating if race clock is actively ticking
- `features` - capability flags (e.g., `pit_timing`)
- `limit` - race limit configuration (type: time|laps, value, remaining_ms)

### 10.3 Flag Banner Logic

The flag banner uses CSS classes derived from the snapshot:

```css
.flag.flag--{color}        /* base color (green, yellow, red, white, checkered, blue) */
.flag.is-pulsing          /* animated attention state */
.flag.flag--green.flash   /* one-shot flash when entering green */
```

**Pulsing states:** YELLOW, RED, BLUE, CHECKERED (continuous attention)
**Flash state:** GREEN (single animation on green entry, then stable)

**Accessibility:** Banner includes `aria-label` with format: `"{Color} - {Meaning}"` (e.g., "White - Final Lap", "Blue - Driver Swap")

### 10.4 Leaderboard Updates

Standings are rendered directly from the `/race/state` response:
- **PRE/COUNTDOWN**: Sorted by qualifying grid order (frozen grid from event config)
- **GREEN/WHITE/CHECKERED**: Sorted by race position (laps desc, best lap asc, pace asc)

Each standing row includes:
- `position` - 1-based finishing position (calculated by engine)
- `entrant_id`, `number`, `name` - identity fields
- `laps` - total laps completed
- `lap_deficit` - laps behind leader
- `last`, `best`, `pace_5` - lap times in seconds (null if unavailable)
- `gap_s` - time gap to leader (0 if not on same lap)
- `enabled`, `status` - roster state
- `grid_index`, `brake_valid` - qualifying metadata

**Viewport:** Operator UI shows ~16 rows before scrolling begins (configurable via `app.ui.visible_rows`)

---

## 9. OSC Lighting Integration

The system provides bidirectional Open Sound Control (OSC) integration with QLC+ lighting software, enabling synchronized race flag lighting and operator-assisted flag controls.

### 9.1 Architecture Overview

**Protocol:** UDP-based OSC (Open Sound Control)  
**Direction:** Bidirectional (CCRS ↔ QLC+)  
**Threading Model:** OSC receiver runs on dedicated thread, callbacks marshaled to FastAPI event loop via `asyncio.call_soon_threadsafe`  
**Safety Mechanism:** Phase-based guards prevent lighting operator from controlling critical timing events (race start/end)

**Dependencies:**
- `pythonosc.udp_client.SimpleUDPClient` - Outbound OSC messages
- `pythonosc.osc_server.ThreadingOSCUDPServer` - Inbound OSC listener
- `pythonosc.dispatcher.Dispatcher` - Message routing

**Modules:**
- `backend/osc_out.py` - Sends flag/blackout commands to QLC+
- `backend/osc_in.py` - Receives flag/blackout signals from QLC+ operator buttons
- `backend/server.py` - Integration points and lifecycle management

### 9.2 OSC Output (osc_out.py)

**Purpose:** Send real-time lighting commands from CCRS to QLC+ based on race state changes.

**Key Components:**
```python
class OscLightingOut:
    def __init__(self, cfg: dict)
    def send_flag(self, flag: str)          # GREEN, YELLOW, RED, etc.
    def send_blackout(self, active: bool)   # True=off, False=on
    def cleanup()
```

**Message Format:**
- Flag messages: `/{address}/{flag_name}` → `1` (integer)
- Blackout messages: `/{address}` → `1` (on) or `0` (off)

**UDP Reliability Strategy:**
- Each message sent **3 times** with 5ms delay between repeats (configurable via `send_repeat`)
- Compensates for UDP packet loss without requiring ACK protocol
- Low latency impact (~10ms total per command)

**Configuration Reference:**
```yaml
integrations:
  lighting:
    osc_out:
      enabled: true
      host: "192.168.1.101"    # QLC+ listening IP
      port: 9000               # QLC+ listening port
      send_repeat: 3           # UDP redundancy count
      addresses:
        flags: "/ccrs/flag"    # Base path for flag messages
        blackout: "/ccrs/blackout"
```

**Trigger Points:**
- Auto-green countdown completion → `GREEN`
- Manual start race → `GREEN`
- Flag changes via `/race/control/flag` → respective color
- End race → `CHECKERED`
- Abort/reset → `PRE` + blackout
- Race setup screen → blackout
- Open results screen → blackout

### 9.3 OSC Input (osc_in.py)

**Purpose:** Receive flag and blackout commands from QLC+ operator buttons, enabling lighting operator to assist with flag changes without controlling race timing.

**Key Components:**
```python
class OscInbound:
    def __init__(self, cfg: dict, on_flag: Callable, on_blackout: Callable)
    def start()                                    # Spawns listener thread
    def stop()                                     # Graceful shutdown
    def _handle_default(self, addr, *values)       # Processes flag messages
    def _handle_blackout(self, addr, *values)      # Processes blackout messages
```

**Threading Model:**
- `ThreadingOSCUDPServer` runs on dedicated background thread
- Callbacks execute on OSC thread, NOT main event loop
- Server integration uses `asyncio.call_soon_threadsafe()` to marshal callbacks safely

**Message Processing:**
- **Flag messages**: Expects path like `/ccrs/flag/YELLOW`, extracts flag name from last segment
- **Blackout messages**: Expects path `/ccrs/blackout` with integer value (1=on, 0=off)
- **Debouncing**: Blackout-off messages debounced by 500ms to prevent flicker (configurable via `debounce_off_ms`)
- **Threshold**: Flag/blackout-on messages require value ≥0.7 (configurable via `threshold_on`)

**Configuration Reference:**
```yaml
integrations:
  lighting:
    osc_in:
      enabled: true
      host: "0.0.0.0"          # Bind to all interfaces
      port: 9010               # CCRS listening port
      paths:
        flags: "/ccrs/flag/*"  # Wildcard pattern for flag messages
        blackout: "/ccrs/blackout"
      threshold_on: 0.7        # Minimum value to trigger
      debounce_off_ms: 500     # Blackout-off debounce delay
```

### 9.4 Server Integration Points

**Lifecycle Management (server.py):**

```python
# Startup handler (line ~3410)
@app.on_event("startup")
async def start_osc_lighting():
    # Initializes _osc_out and _osc_in globals
    # Starts OSC receiver thread via _osc_in.start()
    # Registers async callbacks for flag/blackout handling

# Shutdown handler (line ~3528)
@app.on_event("shutdown")
async def stop_osc_lighting():
    # Gracefully stops receiver thread
    # Cleans up UDP sockets
```

**Helper Functions:**
- `_send_flag_to_lighting(flag: str)` - Send flag change to QLC+ (wrapped in try/except)
- `_send_blackout_to_lighting(active: bool)` - Send blackout state to QLC+
- `_send_countdown_to_lighting()` - Send RED flag during countdown staging
- `_handle_flag_from_qlc(flag: str)` - Process incoming flag from QLC+, includes safety guards
- `_handle_blackout_from_qlc(active: bool)` - Process incoming blackout from QLC+

**Integration Points:**
- Line ~370: `_auto_go_green()` - Countdown completion → GREEN lighting
- Line ~2541: `/race/control/start_race` - Manual start → GREEN lighting
- Line ~2564: `/race/control/end_race` - Race end → CHECKERED lighting
- Line ~2582: `/race/control/abort_reset` - Abort → PRE lighting + blackout
- Line ~2048: `/race/setup` - Setup screen → blackout
- Lines ~2648-2658: `/race/control/open_results` - Results screen → blackout
- Line ~2456: `/race/control/flag` - Manual flag changes → respective lighting

**Frontend Integration:**
- `ui/js/race_control.js` (~1324-1338): Results button calls `/race/control/open_results` before navigation to ensure blackout triggers before page transition

### 9.5 Safety Mechanisms

**Phase-Based Guards (in `_handle_flag_from_qlc`):**

```python
# Prevent lighting operator from starting race
if flag == "GREEN" and engine.phase in ("pre", "countdown"):
    logger.warning("Lighting operator cannot start race (phase=%s)", engine.phase)
    return  # Silently reject

# Prevent lighting operator from ending race
if flag == "CHECKERED" and engine.phase not in ("green", "white"):
    logger.warning("Lighting operator cannot end race during phase=%s", engine.phase)
    return  # Silently reject
```

**Rationale:**
- Race timing integrity requires server-controlled start/end (precise timestamps)
- Lighting operator can assist with YELLOW/RED/BLUE/WHITE flags (safety/procedure)
- Guards ensure lighting hardware failures never corrupt race results
- Violations logged but not surfaced to operator (prevent confusion)

**Non-Critical Failure Handling:**
- All `_send_*_to_lighting()` calls wrapped in try/except
- OSC failures never propagate to race control endpoints
- Lighting becomes "best-effort" if QLC+ unreachable
- Diagnostics SSE streams report OSC errors for troubleshooting

### 9.6 Configuration Reference

**Complete YAML Block:**
```yaml
integrations:
  lighting:
    # === OSC OUTPUT (CCRS → QLC+) ===
    osc_out:
      enabled: true
      host: "192.168.1.101"         # QLC+ OSC input IP
      port: 9000                    # QLC+ OSC input port
      send_repeat: 3                # Send each message N times (UDP reliability)
      addresses:
        flags: "/ccrs/flag"         # Base OSC path for flag messages
        blackout: "/ccrs/blackout"  # OSC path for blackout control
    
    # === OSC INPUT (QLC+ → CCRS) ===
    osc_in:
      enabled: true
      host: "0.0.0.0"               # Bind address (0.0.0.0 = all interfaces)
      port: 9010                    # CCRS listening port
      paths:
        flags: "/ccrs/flag/*"       # Wildcard pattern for incoming flags
        blackout: "/ccrs/blackout"  # Path for incoming blackout
      threshold_on: 0.7             # Minimum value to trigger (0.0-1.0)
      debounce_off_ms: 500          # Debounce blackout-off messages
```

**Network Requirements:**
- CCRS and QLC+ must be on same network or have routed UDP connectivity
- Firewall rules must allow outbound UDP to QLC+ port (9000) and inbound UDP on CCRS port (9010)
- QLC+ virtual console widgets must be configured with matching OSC paths and feedback channels

**QLC+ Configuration:**
- Create OSC output profile pointing to CCRS IP:9010
- Create OSC input profile listening on 0.0.0.0:9000
- Button widgets send to `/ccrs/flag/{COLOR}` with value 1
- Button feedback channels listen to `/ccrs/flag/{COLOR}` for state sync
- Blackout widget sends to `/ccrs/blackout` with values 0/1
- See operators guide section 8.2 for complete QLC+ setup instructions

### 9.7 Message Timing and Performance

**Latency Characteristics:**
- OSC output: <5ms per command (blocking UDP send × repeat count)
- OSC input: <10ms callback latency (thread → event loop marshaling)
- Total round-trip (CCRS → QLC+ → CCRS): ~15-30ms depending on network

**Impact on Race Timing:**
- Flag changes remain server-authoritative (lighting never blocks endpoints)
- Countdown auto-green timing unaffected (lighting called after state transition)
- Manual flag changes may show UI update before lighting completes (acceptable UX trade-off)

**Diagnostic Tools:**
- `/diagnostics/stream` SSE endpoint includes OSC events
- Server logs show all OSC send/receive activity at INFO level
- Lighting failures logged at WARNING level with exception details

---

## 10. Moxie Board Scoring Integration (2025-11-13)

ChronoCore includes integration support for wireless Moxie Board scoring systems used at Power Racing Series events. Moxie scoring is **purely button-press based** - it tracks crowd votes via wireless button presses on the physical moxie board, independent of race performance metrics.

### 10.1 Overview

The Moxie Board is a physical display showing entrant positions and scores based on button presses from spectators and officials. The scoring is a simple count - each button press adds to an entrant's moxie score. The system distributes a fixed total number of points (typically 300) among all active entrants based on their button press counts.

**Key Characteristics:**
- **Pure count system**: Moxie scores = number of button presses received
- **Not calculated**: Unlike lap-based scoring, moxie has no relation to lap times, positions, or race performance
- **Configurable positions**: Boards typically support 18, 20, or 24 display positions
- **Fixed point pool**: Total points (typically 300) are distributed proportionally among entrants

### 10.2 Configuration

Moxie Board integration is controlled via `config/config.yaml`:

```yaml
app:
  engine:
    scoring:
      break_ties_by_best_lap: true
      include_pit_time_in_total: true
      
      # Moxie Board integration
      moxie:
        enabled: true                   # Enable Moxie Board scoring integration
        auto_update: true               # Automatically update moxie scores on button press
        total_points: 300               # Total points available for distribution (typically 300)
        board_positions: 20             # Number of positions on the moxie board (18, 20, or 24 typically)
```

**Configuration Parameters:**
- `enabled` (boolean): Controls whether moxie board features appear in the UI
- `auto_update` (boolean): When true, moxie scores update in real-time as button presses are received
- `total_points` (integer): The pool of points to be distributed among entrants based on button press ratios
- `board_positions` (integer): How many top entrants can be displayed on the physical moxie board

### 10.3 API Endpoint

The backend exposes moxie configuration to frontends via:

**`GET /config/ui_features`**

Response:
```json
{
  "moxie_board": {
    "enabled": true,
    "auto_update": true,
    "total_points": 300,
    "board_positions": 20
  }
}
```

This endpoint is polled by the operator UI on startup to determine whether to show the moxie board navigation button.

### 10.4 UI Integration

When `moxie.enabled = true`:
- Operator index page displays a "Moxie Board" button in the main navigation
- Button appears between "Setup & Devices" and "Open Spectator View"
- Clicking the button navigates to `/ui/operator/moxie_board.html`

The moxie board page (currently in development) will provide:
- Real-time display of button press counts per entrant
- Calculated point distribution based on `total_points` configuration
- Top-N display showing which entrants appear on the physical board (based on `board_positions`)
- Manual score adjustment controls for operator overrides

### 10.5 Scoring Algorithm

The moxie score for each entrant is calculated as:

```
entrant_moxie_points = (entrant_button_presses / total_button_presses) * total_points
```

Where:
- `entrant_button_presses` = count of button presses received by this entrant
- `total_button_presses` = sum of all button presses across all entrants
- `total_points` = configured point pool (default 300)

**Example:**
With `total_points: 300` and 3 entrants:
- Entrant A: 50 presses → (50/100) × 300 = 150 points
- Entrant B: 30 presses → (30/100) × 300 = 90 points
- Entrant C: 20 presses → (20/100) × 300 = 60 points

### 10.6 Physical Board Display

The `board_positions` parameter determines how many entrants can be shown on the physical moxie board hardware. Common values:
- **18 positions**: Smaller events or compact boards
- **20 positions**: Standard PRS configuration
- **24 positions**: Larger events with extended grids

Only the top N entrants (by moxie score) are sent to the physical display hardware. The operator UI shows all entrants with their scores and indicates which ones are currently displayed on the board.

### 10.7 Implementation Status

**Currently Available (v0.1.1):**
- Configuration framework in `config.yaml`
- UI feature flag endpoint (`/config/ui_features`)
- Conditional navigation button on operator index page
- Placeholder moxie board page

**Planned Features:**
- WebSocket or SSE stream for real-time button press events
- Backend endpoint for recording button presses
- Score calculation and distribution engine
- Physical board hardware communication protocol
- Historical moxie scoring data persistence
- Export moxie scores to CSV alongside race results

---

## 11. Configuration (YAML keys of interest)

The system uses a single unified configuration file: `config/config.yaml`

### 11.1 Core Structure

```yaml
app:
  name: ChronoCore Race Software
  version: 0.9.0-dev
  environment: development              # development | production
  
  engine:
    event:
      name: Event Name                  # displayed in UI headers
      date: '2025-11-08'               # ISO date
      location: City, State
    
    default_min_lap_s: 10              # global minimum lap time threshold
    
    persistence:
      enabled: true
      sqlite_path: backend/db/laps.sqlite  # REQUIRED: database location
      journal_passes: true              # enable raw pass journaling
      snapshot_on_checkered: true       # freeze results on CHECKERED
      checkpoint_s: 15                  # engine snapshot interval
      batch_ms: 200                     # journal batch window
      batch_max: 50                     # journal batch size
      fsync: true                       # force sync on writes
      recreate_on_boot: false           # WARNING: destroys existing data
    
    unknown_tags:
      allow: true                       # create provisional entrants
      auto_create_name: Unknown         # prefix for provisional names
    
    diagnostics:
      enabled: true
      buffer_size: 500                  # max events in diagnostics stream
      beep:
        max_per_sec: 5                  # rate limit for beep feature
  
  client:
    engine:
      mode: localhost                   # localhost | fixed | auto
      fixed_host: 127.0.0.1:8000        # used when mode=fixed
      prefer_same_origin: false         # prefer browser's origin
      allow_client_override: true       # allow UI host override
  
  hardware:
    decoders:
      ilap_serial:
        port: COM3                      # Windows: COMx, Linux: /dev/ttyUSBx
        baudrate: 9600
        init_7digit: true               # reset decoder on startup
        min_lap_s: 10
      ambrc_serial:
        port: COM4
        baudrate: 19200
      trackmate_serial:
        port: COM5
        baudrate: 9600
    
    pits:
      enabled: false
      receivers:
        pit_in: []                      # device_id list for pit entry
        pit_out: []                     # device_id list for pit exit

scanner:
  source: ilap.serial                   # ilap.serial | ilap.udp | mock
  decoder: ilap_serial                  # references app.hardware.decoders key
  role: track                           # track | pit_in | pit_out
  min_tag_len: 7                        # reject tags shorter than this
  duplicate_window_sec: 0.5             # suppress duplicate reads within window
  rate_limit_per_sec: 20                # global pass throughput limit
  
  serial:
    port: COM3                          # override decoder port if needed
    baud: 9600
  
  udp:
    host: 0.0.0.0                       # bind address for UDP listener
    port: 5000

publisher:
  mode: http                            # http | inprocess
  http:
    base_url: http://127.0.0.1:8000
    timeout_ms: 500                     # request timeout

log:
  level: info                           # debug | info | warning | error

sounds:
  volume:
    master: 1.0                         # 0.0 - 1.0
    horns: 1.0
    beeps: 1.0
  files:
    lap_indication: lap_beep.wav
    countdown: countdown_beep.wav
    start: start_horn.wav
    white_flag: white_flag.wav
    checkered: checkered_flag.wav

journaling:
  enabled: false                        # legacy passes table journaling
  table: passes_journal                 # table name for raw pass log
```

### 11.2 Path Resolution

- Relative paths resolve against repo root
- `sqlite_path` is **required** - engine will not start without it
- Sound files searched in: `config/sounds/` then `assets/sounds/` (fallback)
- Static UI assets served from: `ui/` directory

### 11.3 Important Defaults

- **Minimum lap time**: 10s (rejects faster laps as duplicates or errors)
- **Duplicate window**: 0.5s (same tag ignored if seen within 500ms)
- **Checkpoint interval**: 15s (engine writes recovery snapshot every 15s)
- **Journal batch**: 200ms or 50 events (whichever comes first)
- **Diagnostics buffer**: 500 events (auto-trims older)

---

## 12. Simulation Tools

### 12.1 Mock Decoder

The system includes a built-in mock decoder for testing without hardware:

```yaml
scanner:
  source: mock
  mock_tag: "3000999"
  mock_period_s: 6.0
```

The mock decoder emits a fixed tag at regular intervals, useful for:
- UI development and testing
- Operator training without hardware
- CI/CD integration testing
- Demo modes at events

### 11.2 Simulator Scripts

**Sprint Simulator** (`scripts/Run-SimSprint.ps1`):
- Simulates a time-limited sprint race
- Generates realistic lap times with variance
- Demonstrates full race flow (PRE → GREEN → WHITE → CHECKERED)

**Endurance Simulator** (`scripts/Run-SimEndurance.ps1`):
- Simulates longer races with pit stops
- Tests pit timing features
- Demonstrates multi-hour race scenarios

### 11.3 Dummy Data Loader

**`backend/tools/load_dummy_from_xlsx.py`**:
- Imports entrant rosters from Excel spreadsheets
- Useful for seeding test databases
- Supports bulk entrant creation with tags

**Typical workflow:**
```bash
python backend/tools/load_dummy_from_xlsx.py roster.xlsx
```

### 11.4 Feed Simulator

**`backend/tools/sim_feed.py`**:
- Generates synthetic timing passes
- Configurable lap time distributions
- Can simulate multiple concurrent entrants
- Posts to `/sensors/inject` or direct engine calls

**Usage:**
```bash
python backend/tools/sim_feed.py --entrants 10 --duration 300 --mean-lap 45
```

### 11.5 Testing Strategy

**Unit Tests**: Test individual components (engine, decoders, parsers)

**Integration Tests**: Test full flow from decoder → engine → persistence

**UI Tests**: Use mock decoder with known sequences to verify UI behavior

**Performance Tests**: Use sim_feed to generate high-volume pass streams

---

## 13. Engine Host Discovery

## Background

Older builds assumed the Operator UI always talked to `localhost:8000`. This created confusion when running the UI remotely or in the field.

The system now defines **engine host resolution policy** in `app.yaml` as the single source of truth. UIs follow the same precedence rules everywhere.

## Location

Defaults live in:

- `config/app.yaml` → `app.client.engine`
- `config/config.yaml` → (deprecated; may mirror for legacy tools)

## Precedence Order

1. **Same-origin** (if `prefer_same_origin: true` and UI is served by the engine)  
2. **Device override** (if `allow_client_override: true` and set in localStorage `cc.engine_host`)  
3. **Policy fallback** (from YAML):  
   - `mode: fixed` → `fixed_host`  
   - `mode: localhost` → `127.0.0.1:8000`  
   - `mode: auto` → try same-origin, then fixed_host, then localhost  
4. **Last resort**: same-origin again if available.

## Example Configurations

### Development laptop

```yaml
app:
  client:
    engine:
      mode: localhost
      allow_client_override: true
```

### Field deployment (fixed IP)

```yaml
app:
  client:
    engine:
      mode: fixed
      fixed_host: "10.77.0.10:8000"
      allow_client_override: false
```

### Mixed environment

```yaml
app:
  client:
    engine:
      mode: auto
      fixed_host: "10.77.0.10:8000"
      prefer_same_origin: true
      allow_client_override: true
```

## Operator UI Behavior

- **Settings page** shows the configured host and whether overrides are allowed.  
- **Footer status pill** always displays the *effective engine host* and connection status.  
- **Race Control and Setup** pages always use the resolved host; they do not hardcode `localhost`.

## Notes for Developers

- The launcher (desktop, `file://`) must bootstrap the YAML defaults, since same-origin is not possible there.  
- Browser-served UIs can skip host strings entirely when `prefer_same_origin` is true.


---

## 14. Requirements & Runtime

- **Python**: 3.12
- **Core deps**: fastapi, starlette, uvicorn[standard], httpx, aiosqlite, pyyaml, pyserial, pandas, openpyxl
- Launch: `python -m uvicorn backend.server:app --reload --port 8000`

---

## 16. Troubleshooting Lap Crediting Issues (2025-10-31)

During race weekend testing, several scenarios were identified where transponder reads appeared in diagnostics but laps weren't being credited. This section documents common gating conditions and diagnostic procedures.

### 16.1 Common Gating Conditions

**Laps will NOT be credited if:**

1. **Phase is not GREEN/WHITE**
   - During PRE/COUNTDOWN: Passes are logged but don't count as laps
   - After CHECKERED: No new laps are credited (race frozen)
   - Check `/race/state` → `flag` and `phase` fields

2. **Minimum lap time not met** (`min_lap_s`)
   - Default: 10 seconds (configurable in `config.yaml`)
   - Passes faster than this threshold are rejected as errors or duplicates
   - Common during bench testing with rapid manual tag presentations
   - Check: Diagnostics page shows rejection reason "min_lap"

3. **Duplicate window filter** (`min_lap_dup`)
   - Default: 1.0 seconds
   - Same tag seen twice within this window = duplicate, second read ignored
   - Check: Diagnostics page shows rejection reason "dup"

4. **Source role mismatch**
   - Only `source="track"` passes credit laps for Start/Finish
   - Pit passes (`pit_in`/`pit_out`) use explicit roles and don't credit laps
   - Check: Diagnostics SSE stream shows `source` field for each pass

5. **Entrant not enabled or not ACTIVE**
   - Disabled entrants: passes logged but ignored for scoring
   - Status must be `ACTIVE` (not `DNF`, `DQ`, `DISABLED`)
   - Check: Entrants page, verify "Entrant Enabled" toggle is ON

6. **First crossing after GREEN**
   - The first pass after green flag **arms** the lap timer but doesn't credit a lap
   - Second pass (if >= `min_lap_s`) credits Lap 1
   - This is expected behavior - not a bug

### 16.2 Diagnostic Procedures

**When laps aren't counting but diagnostics shows passes:**

1. **Check race phase**
   ```
   GET /race/state
   Verify: "flag": "green" and "running": true
   ```

2. **Verify minimum lap time**
   ```
   Check config.yaml → app.engine.default_min_lap_s
   Typical racing: 10-30 seconds
   Bench testing: reduce to 2-5 seconds
   ```

3. **Review detection vs lap count**
   - Race Control → Seen table shows `reads` (detection count)
   - Standings table shows `laps` (credited laps)
   - `reads > 0` but `laps = 0` indicates gating condition is active

4. **Enable detailed logging**
   ```yaml
   log:
     level: debug  # Shows per-detection decision reasons
   ```

5. **Check persistence path**
   - Verify `lap_events` table is being written
   - Standings won't update if persistence is failing silently
   - Check server logs for database errors

### 16.3 Race Weekend Fix (October 31, 2025)

**Problem identified:**
- Pre-race parade laps set crossing timestamps (`_last_hit_ms`)
- When green flag dropped, first racing pass calculated delta from parade lap
- Result: Artificially short "first lap" (e.g., 2-3 seconds instead of 45 seconds)
- These short laps were correctly rejected by `min_lap_s` filter
- Drivers appeared to complete first lap but lap counter stayed at 0

**Solution implemented:**
- When `set_flag("green")` is called, all `_last_hit_ms` timestamps are cleared
- First pass after green sets fresh timestamp (arming pass)
- Second pass calculates accurate lap time from green flag start
- No more phantom short first laps from pre-race activity

**Code location:** `backend/race_engine.py` line ~430
```python
if f_lower == "green":
    if not self.running:
        # Clear any pre-race crossing timestamps to prevent short first laps
        for ent in self.entrants.values():
            ent._last_hit_ms = None
```

### 16.4 Bench Testing Recommendations

When testing timing hardware without actual racing:

1. **Reduce minimum lap time**
   ```yaml
   app:
     engine:
       default_min_lap_s: 2  # Allow fast manual tag presentations
   ```

2. **Watch diagnostics stream**
   - Navigate to Diagnostics / Live Sensors page
   - Enable beep for audio feedback
   - Look for rejection reasons in real-time

3. **Use mock decoder for UI testing**
   ```yaml
   scanner:
     source: mock
     mock_tag: "3000999"
     mock_period_s: 6.0
   ```

4. **Check effective configuration**
   ```
   GET /race/state
   Verify min_lap_s matches your expectations
   ```

---

## 17. Appendices

- Migration scripts (e.g., `migrate_add_car_num.py`)  
- Dummy loaders (`load_dummy_from_xlsx.py`)  
- Example database exports  
- Future feature roadmap  

---

_Last updated: 2025-11-13_
