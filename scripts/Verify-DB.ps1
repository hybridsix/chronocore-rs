# Verify-DB.ps1
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$DB = Join-Path $Root "laps.sqlite"
Get-Item $DB | Select-Object FullName, LastWriteTime, Length
& "$Root\.venv\Scripts\python.exe" -c @"
import sqlite3, sys
db = sqlite3.connect(r'$DB')
c = db.cursor()
def cols(t):
  try:
    c.execute(f'PRAGMA table_info({t})'); return [r[1] for r in c.fetchall()]
  except Exception as e:
    return ['ERR', str(e)]
print('DB:', r'$DB')
print('passes:', cols('passes'))
print('transponders:', cols('transponders'))
print('race_state:', cols('race_state'))
"@
