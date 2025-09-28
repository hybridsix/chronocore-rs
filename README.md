# ChronoCore Race Software

Open-source race timing and management platform.  
Supports Power Racing Series (PRS) and general lap-based racing (karts, RC cars, boats, etc.).

---

## Requirements

Python 3.11+ (Windows, Linux, macOS)  
Git (to clone the repo)  

Python dependencies (install with `pip`):

```
fastapi
uvicorn[standard]
pyserial
aiosqlite
pyyaml
pandas
openpyxl
```

> These are also listed in `requirements.txt` for convenience.

---

## 1) Create & Activate Virtual Environment (Windows 11 PowerShell)

```powershell
cd backend
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

(Linux/macOS users: adjust the venv activation path as needed.)

---

## 2) Smoke-Test the I-Lap Decoder

Find your COM port in Windows Device Manager (e.g., COM7), then:

```powershell
python ilap_smoketest.py COM7
```

You should see an **ACK** shortly after init and **PASS** lines when a tag crosses.

---

## 3) Run the Server

Launch the FastAPI backend:

```powershell
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Health check:

<http://localhost:8000/health> → `{"ok": true}`

---

## Next Steps

- Load dummy lap data: `python tools/load_dummy_from_xlsx.py path/to/scoresheet.xlsx`
- Explore API routes at <http://localhost:8000/docs>
- Open spectator UI: <http://localhost:8000/ui/spectator.html>
- Operator & entrants admin UIs coming soon
