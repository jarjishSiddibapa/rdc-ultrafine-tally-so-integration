# Stops the app server (any process running "app.cli serve" from this folder).
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'app\.cli serve' -and $_.CommandLine -like "*$root*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "Stopped."
