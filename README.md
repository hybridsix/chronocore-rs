# PRS Starter

## 1) Create & activate venv (Windows 11 PowerShell)
```
cd backend
python -m venv ..\.venv
..\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

## 2) Smoke-test the I-Lap decoder
Find your COM port in Windows Device Manager (e.g., COM7), then:
```
python ilap_smoketest.py COM7
```
You should see an ACK shortly after init and PASS lines when a tag crosses.

## 3) Run the server (for later UI/API work)
```
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```
Check http://localhost:8000/health → {"ok": true}
