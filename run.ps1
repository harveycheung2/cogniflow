$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$python = ""
if (Test-Path ".venv\Scripts\python.exe") {
    $python = ".venv\Scripts\python.exe"
} elseif (Test-Path "..\agentic_system\.venv\Scripts\python.exe") {
    $python = "..\agentic_system\.venv\Scripts\python.exe"
} else {
    $python = "python"
}

Write-Host "Starting Agentic Workday OS v2 with Python: $python" -ForegroundColor Cyan
Start-Process "http://127.0.0.1:8765"
& $python app.py
