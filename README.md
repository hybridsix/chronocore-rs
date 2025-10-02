# ChronoCore Race Software

Open-source race timing and management platform.  
Designed for Power Racing Series (PRS) and adaptable to other lap-based racing (karts, RC cars, boats, etc.).

---

## Features
- **RaceEngine** core: in-memory state store with JSON snapshots
- FastAPI backend with routes:
  - `/engine/load`, `/engine/flag`, `/engine/pass`
  - `/race/state` → authoritative standings
- Supports multiple flag states (`pre`, `green`, `yellow`, `red`, `white`, `checkered`)
- Entrant management with enable/disable, provisional “Unknown ####” creation
- Lap scoring, best/last/pace (5-lap moving average), gap calculation
- Pit timing (optional, configurable)
- SQLite persistence layer (toggleable)
- Operator and Spectator UIs

---

## Requirements
- Python 3.11+ (Windows, Linux, macOS)
- Git

Dependencies (install via `requirements.txt`):
```bash
pip install -r requirements.txt
```

---

## Quick Start

### 1. Setup
```powershell
git clone https://github.com/hybridsix/chronocore-rs
cd chronocore-rs
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Run the Server
From the repo root:
```powershell
uvicorn backend.server:app --reload --port 8000
```

Health check:  
[http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

### 3. Load a Race
```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/engine/load `
  -ContentType application/json `
  -Body '{ "race_id":1, "race_type":"sprint", "entrants":[{"entrant_id":1,"enabled":true,"status":"ACTIVE","tag":"3000123","car_number":"101","name":"Team A"}] }'
```

### 4. Flip Flags
```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/engine/flag `
  -ContentType application/json -Body '{ "flag":"green" }'
```

### 5. Post Passes
```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/engine/pass `
  -ContentType application/json -Body '{ "tag":"3000123","source":"track" }'
```

View state:  
[http://127.0.0.1:8000/race/state](http://127.0.0.1:8000/race/state)

---

## UIs
- **Spectator UI:** [http://127.0.0.1:8000/ui/spectator/spectator.html](http://127.0.0.1:8000/ui/spectator/spectator.html)
- **Operator UI:** [http://127.0.0.1:8000/ui/operator/index.html](http://127.0.0.1:8000/ui/operator/index.html)

---

## Next Steps
- Polish Operator Console (flag controls, DNF/DQ toggles)
- Expand Spectator UI integration
- Add event logging and export tooling
- Extend YAML config for features like pit timing

---

## License
MIT
