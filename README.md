# CogniFlow OS ⚡

A high-performance, multi-agent academic and workday orchestration system for university students and professionals.

## Key Features
- **Multi-Agent Orchestration via Amazon Bedrock**:
  - **Lead Orchestrator**: Uses Claude 3.5 / 4.5 Sonnet to synthesize priorities and schedule days.
  - **NTULearn Specialist**: Direct integration with Blackboard Ultra REST API to ingest enrolled modules, lecture notes, deadlines, and announcements.
  - **Planner Engine**: Algorithmic urgency scoring & dynamic time-blocking.
- **Glassmorphic Interactive UI**: Ultra-responsive FastAPI web dashboard with dark-mode styling, live sync status, and real-time AI copilot.
- **Blackboard Ultra Automation**: Persistent SSO/2FA sessions, announcements scraper, and multi-file slide downloader.

## Quickstart (Windows)

1. Open PowerShell and navigate to `agentic_os_v2`:
   ```powershell
   cd agentic_os_v2
   ```
2. Ensure dependencies are installed:
   ```powershell
   ..\agentic_system\.venv\Scripts\pip install -r requirements.txt
   ```
3. Run the application:
   ```powershell
   .\run.ps1
   ```
   The dashboard will automatically open at `http://127.0.0.1:8765`.

## Architecture
```
agentic_os_v2/
├── app.py                 # FastAPI Web Server & REST Endpoints
├── run.ps1                # 1-Click Launch Script
├── core/
│   ├── config.py          # Environment, Paths & Model Settings
│   └── database.py        # SQLite Storage Layer
├── services/
│   ├── aws_bedrock.py     # Amazon Bedrock Converse API Client
│   ├── ntulearn.py        # Blackboard Ultra API & Playwright Auth
│   ├── planner.py         # Priority Scoring & Time Blocking
│   └── agents.py          # Multi-Agent Reasoning Engine
└── static/
    ├── index.html         # Dashboard Interface
    ├── style.css          # Glassmorphic Dark-Mode Design
    └── app.js             # Client Controller
```
