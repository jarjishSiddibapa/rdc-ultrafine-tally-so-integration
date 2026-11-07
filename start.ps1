# Starts the app detached from this terminal (hidden window). Stop with: .\stop.ps1
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
Start-Process -FilePath "$root\venv\Scripts\python.exe" -ArgumentList "-m","app.cli","serve" `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput "$root\logs\server.log" -RedirectStandardError "$root\logs\server.err.log"
Write-Host "Started. Logs: logs\server.log"
