# ChronoCore Race Software

**Professional race timing and management platform for lap-based racing events.**

Designed for Power Racing Series (PRS) and adaptable to karts, RC cars, boats, and other timing applications. Features real-time lap scoring, multiple timing modes, pit timing, qualifying sessions, and comprehensive operator tools.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

---

## Quick Start

**Windows (Recommended):**
```powershell
git clone https://github.com/hybridsix/chronocore-rs
cd chronocore-rs
python -m venv .venv
.\.venv\Scripts\pip install -r backend/requirements.txt

# Browser-based (multi-device access)
.\scripts\Run-Server.ps1

# Desktop app (single operator station)
.\scripts\Run-Operator.ps1
```

**Linux/Mac:**
```bash
git clone https://github.com/hybridsix/chronocore-rs
cd chronocore-rs
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m uvicorn backend.server:app --reload --port 8000
```

**Access the UIs:**
- Operator Console: `http://localhost:8000/ui/operator/`
- Spectator Display: `http://localhost:8000/ui/spectator/`
- Health Check: `http://localhost:8000/healthz`

---

## Key Features

### Race Management
- **Multiple Race Modes:** Sprint (time-limited), endurance (lap-limited), qualifying sessions
- **Soft-End Support:** Race continues after time/lap limit until all drivers complete their lap
- **Flag System:** Pre, Green, Yellow, Red, Blue, White, Checkered with automatic white/checkered flags
- **Live Standings:** Real-time position tracking, gap calculations, lap times
- **Pit Timing:** Track pit stops with in/out timing (optional)

### Timing & Scoring
- **Intelligent Lap Validation:** Minimum lap time, duplicate detection, brake test validation
- **Provisional Entrants:** Auto-creates "Unknown ####" entries for unregistered transponders
- **Best/Last/Pace:** Tracks best lap, last lap, and 5-lap moving average
- **Qualifying Grid:** Freeze qualifying results to set starting order for subsequent races

### Hardware Support
- **iLap Serial Decoder** (default PRS timing system)
- **AMBrc Serial** (MyLaps/AMB legacy protocol)
- **Trackmate Serial** (IR timing)
- **CANO TCP** (network-based decoders)
- **Mock Decoder** (testing without hardware)

### Operator Tools
- **Entrant Management:** Enable/disable, status tracking (ACTIVE, DNS, DNF, DQ)
- **Tag Assignment:** Flexible transponder-to-driver mapping
- **Live Diagnostics:** Real-time sensor feed with beep notifications
- **Results Export:** Per-lap CSV and event CSV downloads
- **Race Control:** Flag management, freeze/unfreeze, session control

### Display Options
- **Browser-Based:** Multi-device access via web browser
- **Desktop Application:** Native Windows app with splash screen (pywebview)
- **Remote Spectator:** Fullscreen Chrome display for separate screens (Windows/Linux)

### Integration
- **OSC Output:** Real-time Open Sound Control messages for lighting systems (QLC+, etc.)
- **Flag Events:** Broadcast flag changes for automated lighting cues
- **Lap Events:** Send lap completion and position updates

---

## Documentation

- **[Operator's Guide](docs/operators_guide.md)** - Setup, race day operations, troubleshooting
- **[Technical Reference](docs/technical_reference.md)** - Architecture, API, configuration
- **[Documentation Index](docs/index.md)** - Browse all documentation

**GitHub Pages:** [https://hybridsix.github.io/chronocore-rs/](https://hybridsix.github.io/chronocore-rs/)

---

## Startup Options

### Option 1: Browser-Based Server (Recommended for Multi-Display)
```powershell
.\scripts\Run-Server.ps1
```
- Auto-configures Windows Firewall
- Launches lap logger in separate window
- Accessible from any browser on the network
- Best for: Multiple operators, remote spectator displays

### Option 2: Desktop Application (Single Operator Station)
```powershell
.\scripts\Run-Operator.ps1          # Normal mode
.\scripts\Run-Operator.ps1 -Debug   # With DevTools
```
- Native window with splash screen
- Auto-starts backend and lap logger
- Closes cleanly with window
- Best for: Standalone operator workstation

### Option 3: Remote Spectator Display
```powershell
# Windows
.\scripts\Run-Spectator.ps1 -Server 192.168.1.100

# Linux (Debian/Ubuntu)
./scripts/Run-Spectator.sh 192.168.1.100
```
- Opens fullscreen Chrome display
- Tests connectivity before launch
- Best for: Dedicated display screens, scoreboards

---

## Configuration

Edit `config/config.yaml` to configure:
- **Database:** SQLite path and persistence settings
- **Timing Hardware:** Decoder type, serial port, network settings
- **Race Modes:** Time/lap limits, soft-end behavior, scoring rules
- **Features:** Pit timing, auto-provisional, minimum lap times
- **OSC Integration:** Lighting control output

**Example decoder setup:**
```yaml
scanner:
  source: ilap.serial
  serial:
    port: COM3
    baud: 9600
```

See [Technical Reference](docs/technical_reference.md#3-decoder-subsystems) for complete configuration options.

---

## API Examples

### Load a Race
```powershell
Invoke-RestMethod -Method Post http://localhost:8000/engine/load `
  -ContentType application/json `
  -Body '{
    "race_id": 1,
    "race_type": "sprint",
    "entrants": [
      {
        "entrant_id": 1,
        "enabled": true,
        "status": "ACTIVE",
        "tag": "3000123",
        "number": "101",
        "name": "Team Alpha"
      }
    ]
  }'
```

### Set Flag
```powershell
Invoke-RestMethod -Method Post http://localhost:8000/engine/flag `
  -ContentType application/json `
  -Body '{ "flag": "green" }'
```

### Inject Pass (Manual/Testing)
```powershell
Invoke-RestMethod -Method Post http://localhost:8000/ilap/inject `
  -ContentType application/json `
  -Body '{ "tag": "3000123" }'
```

### Get Live State
```powershell
Invoke-RestMethod http://localhost:8000/race/state
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Timing Hardware                        │
│  (iLap, AMBrc, Trackmate, CANO, etc.)                   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
        ┌────────────────────────────┐
        │   Lap Logger Process       │
        │   (backend/lap_logger.py)  │
        └────────────┬───────────────┘
                     │ HTTP POST
                     ▼
        ┌────────────────────────────┐
        │   FastAPI Backend          │
        │   (backend/server.py)      │
        │                            │
        │  ┌──────────────────────┐  │
        │  │   Race Engine        │  │
        │  │   (race_engine.py)   │  │
        │  └──────────────────────┘  │
        │           │                │
        │           ▼                │
        │  ┌──────────────────────┐  │
        │  │  SQLite Persistence  │  │
        │  └──────────────────────┘  │
        └────────────┬───────────────┘
                     │ HTTP/SSE
        ┌────────────┴───────────────┐
        │                            │
        ▼                            ▼
┌───────────────┐          ┌─────────────────┐
│  Operator UI  │          │  Spectator UI   │
│  (Browser or  │          │   (Browser)     │
│   pywebview)  │          │                 │
└───────────────┘          └─────────────────┘
```

**Key Components:**
- **Race Engine:** Authoritative in-memory state with real-time lap scoring
- **Lap Logger:** Hardware interface process (separate from backend)
- **FastAPI Backend:** HTTP API, SSE streaming, static file serving
- **SQLite Journal:** Append-only event log with periodic snapshots
- **Operator UI:** Full race control, entrant management, diagnostics
- **Spectator UI:** Live standings, timing displays

---

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## Acknowledgments

- Built for the Power Racing Series community
- Supports iLap timing hardware (PRS standard)
- Future compatibility with AMB/MyLaps, Trackmate, and other timing systems
