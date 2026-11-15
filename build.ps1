# Builds dist\ILTC WEEKLY SCHEDULE\ILTC WEEKLY SCHEDULE.exe (with the
# worker in its worker\ subfolder). Run after changing any .py or gui.html:
#     powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = '.venv\Scripts\python.exe'
$app = 'dist\ILTC WEEKLY SCHEDULE'
# PyInstaller empties dist\ on every build; keep the user's settings and log.
$keep = Join-Path $env:TEMP 'iltc-weekly-schedule-keep'
New-Item -ItemType Directory -Force $keep | Out-Null
foreach ($f in 'schedule_config.json', 'schedule_gui.log', 'schedule_panel_log.jsonl') {
    if (Test-Path "$app\$f") { Copy-Item "$app\$f" $keep -Force }
}
& $py -m PyInstaller 'ILTC WEEKLY SCHEDULE WORKER.spec' --noconfirm --distpath build\worker-dist
if ($LASTEXITCODE) { throw 'worker build failed' }
& $py -m PyInstaller 'ILTC WEEKLY SCHEDULE.spec' --noconfirm
if ($LASTEXITCODE) { throw 'app build failed' }
Remove-Item "$app\worker" -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item 'build\worker-dist\ILTC WEEKLY SCHEDULE WORKER' "$app\worker" -Recurse
foreach ($f in 'schedule_config.json', 'schedule_gui.log', 'schedule_panel_log.jsonl') {
    if (Test-Path "$keep\$f") { Copy-Item "$keep\$f" $app -Force }
}
Write-Host "Built $app\ILTC WEEKLY SCHEDULE.exe"
