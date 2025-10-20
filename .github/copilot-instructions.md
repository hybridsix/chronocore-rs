## Purpose

Short, actionable guidance for an AI coding agent working on ChronoCoreRS.
Focus on discoverable, implementation-level details so an agent can make safe, useful
changes without asking for basic facts.

## Quick start (developer workflow)

- Install and activate a venv, then install deps:

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt
```

- Run the backend (or use `scripts/Run-Server.ps1`):

```powershell
uvicorn backend.server:app --reload --port 8000
# or
./scripts/Run-Server.ps1 -Port 8000
```

Health/readiness endpoints: `/healthz`, `/readyz`.

## Big-picture architecture

- FastAPI backend: `backend/server.py` mounts the UI and exposes the public HTTP API.
- Core timing model: `backend/race_engine.py` implements the in-memory RaceEngine used
  by backends and frontends. This file contains the business rules for flags, lap
  acceptance, provisional entrants, pit timing, and snapshots used by UIs.
- Persistence & schema: `backend/db_schema.py` owns DB DDL and `ensure_schema()`.
  Important: it creates a partial UNIQUE index to enforce "tag unique among ENABLED entrants".
- Config: `backend/config_loader.py` loads a single unified YAML (`config/config.yaml`) and
  exposes helpers like `get_db_path()` and `get_server_bind()`; the code expects a valid
  config on import (it raises with clear messages when missing).
- UI assets are served from the repo `ui/` directory under `/ui` by `server.py`.

Data flow summary:
- Scanner processes (embedded or external) publish detections to the backend using
  HTTP `POST /ilap/inject` or the backend exposes an SSE endpoint `/ilap/stream` for live UI.
- The backend mirrors runtime state (RaceEngine) in-memory and journals to SQLite when
  persistence is enabled. UIs poll or subscribe to Engine snapshots at `/race/state`.

## Important, project-specific conventions

- Forward-only unified config: edits should be made to `config/config.yaml` — `config_loader`
  will fail loudly if required sections are missing. Prefer updating YAML over scattering
  ad-hoc config.
- Entrant id semantics (see `backend/server.py` / `EntrantIn` model):
  - `id` missing or `<= 0` means CREATE (SQLite assigns new primary key).
  - `id > 0` means UPDATE (upsert by primary key).
- Tag handling:
  - Tags are normalized by trimming whitespace; empty strings become NULL.
  - A partial UNIQUE index enforces that a tag is unique only among `enabled=1` entrants
    (see `backend/db_schema.py`, `idx_entrants_tag_enabled_unique`).
  - When assigning a tag via `/engine/entrant/assign_tag` the API performs a conflict
    check considering only ENABLED entrants and excludes the incumbent row.
- Boolean coercions: endpoints coerce `enabled` from ints/strings/booleans (see Pydantic
  validators in `backend/server.py`). Follow the same coercion logic when producing tests.

## Key HTTP endpoints to reference in changes

- Runtime engine: `/engine/load` (load roster), `/engine/entrant/assign_tag` (idempotent tag assign),
  `/engine/flag` (set flag), `/engine/pass` or `/ilap/inject` (push passes).
- Admin authorship: `/admin/entrants` (read/write authoritative roster). Important: upserts
  are performed inside a single DB transaction and return `409` on tag conflicts.
- UI streaming: `/ilap/stream` (SSE), `/diagnostics/stream` (SSE); `/ilap/peek` is a simple last-tag
  polling endpoint.

## Typical quick examples the agent may need to generate

- Load a simple race (example payload used in README):

```json
{"race_id":1,"race_type":"sprint","entrants":[{"entrant_id":1,"enabled":true,"status":"ACTIVE","tag":"3000123","number":"101","name":"Team A"}]}
```

- Assign a tag idempotently (normalize whitespace -> NULL clears tag):

POST `/engine/entrant/assign_tag` body: `{ "entrant_id": 5, "tag": "3000123" }`

If `tag` equals the current value, the endpoint returns 200 and keeps the Engine in sync.

## Common pitfalls and where to look

- Don’t assume DB path: use `backend/config_loader.get_db_path()`; default is
  `backend/db/laps.sqlite` if config omits it.
- The app expects schema created at boot; `backend/server.py` calls `ensure_schema()` on startup.
  If you change table shapes, update `backend/db_schema.py` and bump migration logic.
- Follow the existing error codes: APIs use 400/404/409/412/500 intentionally; keep those
  semantics when modifying endpoints.

## Where to add tests or small changes

- Small behavioral tests can mock HTTP endpoints (FastAPI TestClient) and target:
  - `/engine/load` (shape + coercion),
  - `/engine/entrant/assign_tag` (conflict/idempotence),
  - `/ilap/inject` -> engine.ingest_pass flows.

## Files to inspect for deeper changes

- `backend/server.py` — main HTTP surface and entrainment with Engine.
- `backend/race_engine.py` — core business logic; the single source of truth for lap rules.
- `backend/db_schema.py` — DDL, partial-unique index, and helper `tag_conflicts`.
- `backend/config_loader.py` — unified config expectations and helpers.
- `scripts/Run-Server.ps1` and `README.md` — canonical developer commands.

If anything here is unclear or you want more examples (unit tests, common refactors,
or a policy for schema migrations), tell me which area to expand and I’ll iterate.
