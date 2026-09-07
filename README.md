# CogniFlow OS ⚡

An intelligent, multi-agent academic and workday operating system featuring cascading LLM orchestration and instant local failsafe intelligence.

![CogniFlow](static/cogniflow_logo.png)

---

## 🌟 Key Capabilities

### 1. Cascading Multi-Agent AI Orchestration
CogniFlow automatically routes requests across a 4-tier model hierarchy to optimize for quality, speed, and zero downtime:
- **Priority 1: AWS Bedrock**: Anthropic Claude 3.5 / 4.5 Sonnet for deep schedule optimization, deadline synthesis, and multi-agent reasoning.
- **Priority 2: Groq Cloud**: Ultra-fast (~300 tokens/sec) open-weights fallback (`qwen/qwen3.8-27b` / `llama-3.3-70b`).
- **Priority 3: Google Gemini**: High-context (1M tokens) secondary cloud failover (`gemini-3.6-flash`).
- **Priority 4: Local Specialist Engine**: Instant deterministic SQLite course dossier & document search (<10ms, 0 cloud cost, 100% offline).

### 2. Real-Time AI Token & Quota Usage Monitor
- Live token tracking modal accessible directly via the top navbar **AI** pill badge.
- Per-model daily free quotas, remaining allowances, inference latencies, and real-time diagnostic test pings.

### 3. Course-Sorted Local Intelligence Hub
- When operating offline or without cloud tokens, CogniFlow renders an interactive multi-module dossier instead of a chat mode.
- Interactive course tabs (`BS1016`, `SC2001`, `MH2802`, etc.) grouping:
  - Direct clickable PDF links to syllabus schedules, active tutorials, and past exam mock papers.
  - Prioritized assessment milestones and task deadlines with urgency scores.
  - Official Blackboard / NTULearn announcements.

### 4. Dynamic Time-Blocking & Timetable Engine
- Visual weekly class schedule and calendar planner with exam dates and venue mapping.
- Algorithmic prioritization engine for deadlines and daily study blocks.

---

## 🚀 Quickstart (Windows)

1. Open PowerShell in the project directory:
   ```powershell
   .\run.ps1
   ```
   *This automatically creates the Python virtual environment (`.venv`), installs dependencies from `requirements.txt`, and launches the dashboard at `http://127.0.0.1:8765`.*

---

## 🛠 Manual Installation

1. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   
   # Windows (PowerShell):
   .\.venv\Scripts\Activate.ps1
   
   # macOS / Linux:
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Configure environment variables (optional for cloud models):
   ```bash
   cp .env.example .env
   ```

4. Launch the application:
   ```bash
   python app.py
   ```
   Open your browser to: **`http://127.0.0.1:8765`**

---

## 📂 Project Architecture

```
cogniflow/
├── app.py                 # FastAPI Backend & REST Endpoints
├── run.ps1                # 1-Click Launch Script (Windows)
├── run.sh                 # 1-Click Launch Script (macOS/Linux)
├── requirements.txt       # Core Dependencies
├── core/
│   ├── config.py          # Environment, Paths & Model Configuration
│   └── database.py        # SQLite Storage & Telemetry Layer
├── services/
│   ├── aws_bedrock.py     # AWS Bedrock Client
│   ├── llm_provider.py    # Resilient Multi-Model Cascade (Bedrock -> Groq -> Gemini)
│   ├── agents.py          # Multi-Agent Reasoning & Course Dossier Hub
│   ├── document_agent.py  # Syllabus & Course Material Parser
│   ├── ntulearn.py        # Blackboard Ultra Ingestion Service
│   ├── timetable_service.py # Timetable & Exam Schedule Ingestion
│   └── planner.py         # Priority Scoring & Time-Blocking Engine
└── static/
    ├── index.html         # High-Contrast Glassmorphic Dashboard
    ├── style.css          # Core Design System
    └── app.js             # Frontend Controller & Telemetry Client
```

---

## 📄 License
MIT License. Created for students and professionals.
