# Builds one sendable file: dist-installer\ILTC WEEKLY SCHEDULE Setup.exe
#
# Runs build.ps1 (unless -SkipBuild), zips dist\ILTC WEEKLY SCHEDULE with a
# clean starter schedule_config.json (times, zone and creator codes only -
# no account list, no logs), and embeds the zip in installer.cs, compiled
# with the C# compiler that ships with Windows. Running the Setup unpacks
# to %LOCALAPPDATA%\Programs\ILTC WEEKLY SCHEDULE, adds a Start menu
# shortcut and opens the app. Updating keeps the user's settings and log.
#
#     powershell -ExecutionPolicy Bypass -File build-installer.ps1 [-SkipBuild]
param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { throw "Can't find the C# compiler at $csc" }

if (-not $SkipBuild) {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like 'ILTC WEEKLY SCHEDULE*' } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    & powershell -ExecutionPolicy Bypass -File build.ps1
    if ($LASTEXITCODE) { throw 'build failed' }
}

$app = 'dist\ILTC WEEKLY SCHEDULE'
if (-not (Test-Path "$app\worker")) { throw "Missing $app - run without -SkipBuild" }

Write-Host 'Staging...'
$stage = 'build\installer-payload\ILTC WEEKLY SCHEDULE'
if (Test-Path 'build\installer-payload') { Remove-Item 'build\installer-payload' -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item "$app\*" $stage -Recurse -Force

# Nothing personal ships: drop logs and the saved account list/picks.
foreach ($leak in 'schedule_gui.log', 'schedule_gui.log.1', 'schedule_panel_log.jsonl', '@AutomationLog.txt') {
    Get-ChildItem -LiteralPath $stage -Recurse -Filter $leak -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item $_.FullName -Force }
}
& '.venv\Scripts\python.exe' -c @"
import json, sys
p = sys.argv[1]
try: d = json.load(open(p, encoding='utf-8'))
except FileNotFoundError: sys.exit()
d = {k: d[k] for k in ('slots', 'codes', 'timezone') if k in d}
json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
"@ "$stage\schedule_config.json"

Write-Host 'Zipping...'
$zip = 'build\Payload.zip'
if (Test-Path $zip) { Remove-Item $zip -Force }
& '.venv\Scripts\python.exe' -c "import shutil; shutil.make_archive(r'build\Payload', 'zip', r'build\installer-payload', 'ILTC WEEKLY SCHEDULE')"
if ($LASTEXITCODE) { throw 'zip failed' }

New-Item -ItemType Directory -Force dist-installer | Out-Null
$out = 'dist-installer\ILTC WEEKLY SCHEDULE Setup.exe'
Write-Host 'Compiling the installer...'
& $csc /nologo /target:winexe /optimize+ /out:$out /resource:$zip,Payload.zip `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
    /reference:System.IO.Compression.dll /reference:System.IO.Compression.FileSystem.dll `
    installer.cs
if ($LASTEXITCODE) { throw 'Installer build failed' }
$size = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "Built $out ($size MB)"
