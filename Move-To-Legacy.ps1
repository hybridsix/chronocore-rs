<#  Move-To-Legacy.ps1
    Mirrors your repo structure under .\legacy\ and moves selected legacy files.
    - Dry-run by default; add -Execute to perform changes.
    - DB handling defaults to Delete. Override with: -DbAction Move|Delete|Ignore
    - Skips venv/.venv/.git/node_modules/__pycache__/.pytest_cache/dist/build
#>

param(
  [ValidateSet('Move','Delete','Ignore')]
  [string]$DbAction = 'Delete',
  [switch]$Execute
)

$ErrorActionPreference = 'Stop'

# -------- Settings --------
$LegacyRoot = Join-Path (Get-Location) 'legacy'
$SkipDirs   = @('legacy','venv','.venv','.git','node_modules','__pycache__','.pytest_cache','dist','build')

# Legacy candidates (relative to repo root). Only moves those that exist.
$LegacyList = @(
  # Working artifacts / exports
  'repo_tree.txt','structure.txt','structure_after.txt','tree.txt',
  '*.ods','*.xlsx','*.7z','*.zip',

  # Old backend entry points / tools superseded by server.py flow
  'app.py','backend/app.py',
  'check_db.py','backend/check_db.py',
  'ilap_logger.py','backend/ilap_logger.py',
  'ilap_smoketest.py','backend/ilap_smoketest.py',
  'race_session.py','backend/race_session.py',
  'backend/migrate_to_0_2_0.py',
  'backend/static.7z',
  'backend/tools/schema_probe.py',

  # Explicitly “old” UI bits
  'ui/operator/entrants.old','ui/operator/entrants.testing',
  'ui/operator/mock_showcase.html',

  # PDFs and planning docs that shouldn’t live at root
  'prs_roadmap_checklist.pdf'
)

# SQLite DBs we may Move/Delete. Handles common locations.
$DbCandidates = @('laps.sqlite','backend/laps.sqlite','data/laps.sqlite')

# -------- Helpers --------
function Should-Skip([string]$fullPath) {
  foreach ($d in $SkipDirs) {
    if ($fullPath -match '(^|[\\/])' + [regex]::Escape($d) + '([\\/]|$)') { return $true }
  }
  return $false
}

function New-Dir([string]$path) {
  if (-not (Test-Path $path)) {
    New-Item -ItemType Directory -Path $path | Out-Null
  }
}

# -------- 1) Build empty mirror under legacy --------
New-Dir $LegacyRoot

$allDirs = Get-ChildItem -Recurse -Directory | Where-Object { -not (Should-Skip $_.FullName) }
foreach ($d in $allDirs) {
  $rel = (Resolve-Path $d.FullName -Relative) -replace '^\.[\\/]', ''
  if ($rel -like 'legacy*') { continue }
  $target = Join-Path $LegacyRoot $rel
  if (-not (Test-Path $target)) {
    if ($Execute) {
      New-Item -ItemType Directory -Path $target | Out-Null
      Write-Host ('Created: ' + $target)
    } else {
      New-Item -ItemType Directory -Path $target -WhatIf | Out-Null
    }
  }
}

# -------- 2) Expand Legacy patterns -> concrete files that exist --------
$toMove = New-Object System.Collections.Generic.List[System.IO.FileInfo]
foreach ($pattern in $LegacyList) {
  $matches = Get-ChildItem -Recurse -File -Force -ErrorAction SilentlyContinue $pattern |
             Where-Object { -not (Should-Skip $_.FullName) }
  foreach ($m in $matches) { [void]$toMove.Add($m) }
}

# -------- 3) Handle DB per your choice --------
$existingDbs = New-Object System.Collections.Generic.List[System.IO.FileInfo]
foreach ($dbpath in $DbCandidates) {
  $matches = Get-ChildItem -Recurse -File -Force -ErrorAction SilentlyContinue $dbpath |
             Where-Object { -not (Should-Skip $_.FullName) }
  foreach ($m in $matches) { [void]$existingDbs.Add($m) }
}

Write-Host ('Planned file moves (legacy list): ' + $toMove.Count)
Write-Host ('Detected DB files:                 ' + $existingDbs.Count + ' — action: ' + $DbAction)

# -------- 4) Ensure legacy destinations exist & move files --------
foreach ($f in $toMove) {
  $rel  = (Resolve-Path $f.FullName -Relative) -replace '^\.[\\/]', ''
  $dest = Join-Path $LegacyRoot $rel
  $destDir = Split-Path $dest -Parent
  New-Dir $destDir
  if ($Execute) {
    Move-Item -LiteralPath $f.FullName -Destination $dest -Force
    Write-Host ('Moved: ' + $rel)
  } else {
    Move-Item -LiteralPath $f.FullName -Destination $dest -Force -WhatIf
  }
}

# -------- 5) DB action --------
switch ($DbAction) {
  'Move' {
    foreach ($db in $existingDbs) {
      $rel  = (Resolve-Path $db.FullName -Relative) -replace '^\.[\\/]', ''
      $dest = Join-Path $LegacyRoot $rel
      New-Dir (Split-Path $dest -Parent)
      if ($Execute) {
        Move-Item -LiteralPath $db.FullName -Destination $dest -Force
        Write-Host ('DB moved: ' + $rel)
      } else {
        Move-Item -LiteralPath $db.FullName -Destination $dest -Force -WhatIf
      }
    }
  }
  'Delete' {
    foreach ($db in $existingDbs) {
      $rel = (Resolve-Path $db.FullName -Relative) -replace '^\.[\\/]', ''
      if ($Execute) {
        Remove-Item -LiteralPath $db.FullName -Force
        Write-Host ('DB deleted: ' + $rel)
      } else {
        Remove-Item -LiteralPath $db.FullName -Force -WhatIf
      }
    }
  }
  'Ignore' {
    Write-Host 'DB files left in place.'
  }
}

# -------- 6) Clean bytecode junk (safe to delete) --------
$bytecode = Get-ChildItem -Recurse -Force -File |
            Where-Object { $_.Name -match '\.pyc$' -or $_.Name -match '\.pyo$' -or $_.Name -match '\.pyd$' }

Write-Host ('Python bytecode files found: ' + $bytecode.Count)
foreach ($b in $bytecode) {
  if ($Execute) {
    Remove-Item -LiteralPath $b.FullName -Force
  } else {
    Remove-Item -LiteralPath $b.FullName -Force -WhatIf
  }
}

# Also remove stray __pycache__ dirs
$pycacheDirs = Get-ChildItem -Recurse -Directory -Force | Where-Object { $_.Name -eq '__pycache__' }
Write-Host ('__pycache__ dirs found: ' + $pycacheDirs.Count)
foreach ($d in $pycacheDirs) {
  if ($Execute) {
    Remove-Item -LiteralPath $d.FullName -Recurse -Force
  } else {
    Remove-Item -LiteralPath $d.FullName -Recurse -Force -WhatIf
  }
}

Write-Host ''
Write-Host 'Done. Dry-run by default.'
Write-Host 'Preview: .\Move-To-Legacy.ps1'
Write-Host 'Run it:  .\Move-To-Legacy.ps1 -Execute'
