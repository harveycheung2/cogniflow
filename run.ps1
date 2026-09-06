$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# 1. Virtual environment setup
$python = ''
if (Test-Path '.venv\Scripts\python.exe') {
    $python = '.venv\Scripts\python.exe'
} elseif (Test-Path '..\agentic_system\.venv\Scripts\python.exe') {
    $python = '..\agentic_system\.venv\Scripts\python.exe'
} else {
    Write-Host '[Agentic OS v2] Creating virtual environment (.venv)...' -ForegroundColor Cyan
    python -m venv .venv
    $python = '.venv\Scripts\python.exe'
    Write-Host '[Agentic OS v2] Installing dependencies...' -ForegroundColor Cyan
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
    & $python -m playwright install chromium
}

# 2. Check .env
if (-not (Test-Path '.env')) {
    if (Test-Path '.env.example') {
        Copy-Item '.env.example' '.env'
        Write-Host '[Agentic OS v2] Created .env from .env.example. Please add AWS credentials if needed.' -ForegroundColor Yellow
    }
}

Write-Host "Starting Agentic Workday OS v2 with Python: $python" -ForegroundColor Green
Start-Process 'http://127.0.0.1:8765'
& $python app.py
