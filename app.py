import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, BackgroundTasks, HTTPException, Query, File, UploadFile, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from core.config import (
    APP_NAME, APP_VERSION, HOST, PORT, BASE_DIR
)
import core.database as db
from services.aws_bedrock import bedrock_client
from services.llm_provider import llm_provider
from services.ntulearn import ntulearn_service
from services.planner import generate_day_schedule, extract_tasks_from_announcements
from services.agents import execute_chat_query
from services.document_agent import document_agent
from services.timetable_service import timetable_service
from services.telegram_bot import telegram_bot_service

# Initialize DB
db.init_db()
timetable_service.seed_default_if_needed()

app = FastAPI(title=APP_NAME, version=APP_VERSION)
STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Models
class ChatRequest(BaseModel):
    message: str
    term: Optional[str] = "26S1"
    session_id: Optional[str] = "default"

class TaskCreate(BaseModel):
    title: str
    course_code: Optional[str] = "General"
    term: Optional[str] = "26S1"
    due_date: Optional[str] = ""
    estimated_minutes: int = 60
    priority_score: float = 5.0

class DownloadRequest(BaseModel):
    mat_id: str
    course_code: str
    title: str
    download_url: str
    file_name: Optional[str] = ""

# In-memory background task tracker
sync_job = {"status": "idle", "message": ""}

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/api/status")
def get_status(term: str = Query("26S1")):
    llm_info = llm_provider.get_status()
    bedrock_info = bedrock_client.get_account_identity()
    ntulearn_auth = ntulearn_service.is_authenticated()
    courses = db.get_all_courses(term=term)
    tasks = db.get_tasks(term=term)
    available_terms = db.get_available_terms()
    materials = db.get_all_materials()
    downloaded = [m for m in materials if m.get("downloaded")]
    schedules = db.get_course_schedules()

    return {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "active_term": term,
        "available_terms": available_terms,
        "llm": llm_info,
        "bedrock": bedrock_info,
        "ntulearn_authenticated": ntulearn_auth,
        "course_count": len(courses),
        "task_count": len(tasks),
        "material_count": len(materials),
        "downloaded_count": len(downloaded),
        "schedule_count": len(schedules),
        "sync_job": sync_job,
    }

@app.get("/api/terms")
def get_terms():
    terms = db.get_available_terms()
    if "26S1" not in terms:
        terms.insert(0, "26S1")
    return terms

@app.post("/api/login/ntulearn")
def login_ntulearn(bg: BackgroundTasks):
    if sync_job["status"] == "running":
        return {"error": "A task is already running"}

    def _login():
        sync_job["status"] = "running"
        sync_job["message"] = "Waiting for NTU SSO & 2FA login in browser window..."
        success = ntulearn_service.trigger_login_portal()
        if success:
            sync_job["status"] = "completed"
            sync_job["message"] = "Logged in successfully! Reusable session saved."
        else:
            sync_job["status"] = "error"
            sync_job["message"] = "Login timed out or was cancelled."

    bg.add_task(_login)
    return {"message": "Login browser opened."}

@app.post("/api/sync")
def sync_ntulearn(bg: BackgroundTasks, term: str = Query("26S1")):
    if sync_job["status"] == "running":
        return {"error": "Sync already in progress"}

    def _sync():
        sync_job["status"] = "running"
        sync_job["message"] = "Syncing enrolled courses & announcements from Blackboard Ultra..."
        res = ntulearn_service.sync_courses_and_announcements()
        if res.get("success"):
            extract_tasks_from_announcements(term=term)
            # Also crawl materials for all courses
            courses = db.get_all_courses(term=term)
            for c in courses:
                ntulearn_service.crawl_course_materials(c["id"], c["course_code"])
            sync_job["status"] = "completed"
            sync_job["message"] = f"Synced {res.get('courses_synced')} courses, announcements, and materials."
        else:
            sync_job["status"] = "error"
            sync_job["message"] = res.get("error", "Sync failed")

    bg.add_task(_sync)
    return {"message": "Sync started"}

@app.post("/api/materials/crawl")
def crawl_materials(bg: BackgroundTasks, term: str = Query("26S1")):
    def _crawl():
        sync_job["status"] = "running"
        courses = db.get_all_courses(term=term)
        total = 0
        for c in courses:
            sync_job["message"] = f"Crawling materials for {c['course_code']}..."
            items = ntulearn_service.crawl_course_materials(c["id"], c["course_code"])
            total += len(items)
        sync_job["status"] = "completed"
        sync_job["message"] = f"Discovered {total} materials across {len(courses)} courses."

    bg.add_task(_crawl)
    return {"message": "Crawling materials..."}

@app.post("/api/materials/download-all")
def download_all_materials(bg: BackgroundTasks, term: str = Query("26S1")):
    def _dl():
        sync_job["status"] = "running"
        def _prog(msg):
            sync_job["message"] = msg
        res = ntulearn_service.download_all_documents_for_term(term=term, progress_callback=_prog)
        sync_job["status"] = "completed"
        sync_job["message"] = f"Downloaded {res.get('total_downloaded')} documents ({res.get('failed')} failed/skipped)."

    bg.add_task(_dl)
    return {"message": "Download job queued."}

@app.post("/api/materials/parse-all")
def parse_all_materials(bg: BackgroundTasks):
    def _parse():
        sync_job["status"] = "running"
        sync_job["message"] = "Running AWS Bedrock / Document Specialist Agent to classify and extract schedules..."
        res = document_agent.analyze_all_downloaded_documents()
        sync_job["status"] = "completed"
        sync_job["message"] = f"Parsed {res.get('documents_analyzed')} documents, extracted {res.get('schedules_extracted')} schedule items."

    bg.add_task(_parse)
    return {"message": "Document parsing job started."}

@app.get("/api/materials/documents")
def get_documents(course_code: Optional[str] = None, doc_type: Optional[str] = None):
    return db.get_all_materials(course_code=course_code)

@app.get("/api/materials/schedule")
def get_schedules(course_code: Optional[str] = None):
    return db.get_course_schedules(course_code=course_code)

@app.get("/api/materials/file/{mat_id}")
def get_material_file(mat_id: str):
    mat = db.get_material_by_id(mat_id)
    if not mat or not mat.get("local_path"):
        raise HTTPException(status_code=404, detail="File not downloaded or not found")

    file_path = Path(mat["local_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Local file missing on disk")

    media_type = "application/pdf" if file_path.suffix.lower() == ".pdf" else "application/octet-stream"
    return FileResponse(
        str(file_path),
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename=\"{file_path.name}\""}
    )

@app.get("/api/courses")
def get_courses(term: str = Query("26S1")):
    return db.get_all_courses(term=term)

@app.get("/api/courses/{course_id}/materials")
def get_course_materials(course_id: str):
    materials = db.get_course_materials(course_id)
    if not materials:
        materials = ntulearn_service.crawl_course_materials(course_id)
    return materials

@app.get("/api/announcements")
def get_announcements(term: str = Query("26S1"), limit: int = 30):
    return db.get_announcements(term=term, limit=limit)

@app.get("/api/tasks")
def get_tasks(term: str = Query("26S1"), status: Optional[str] = None):
    return db.get_tasks(term=term, status=status)

@app.post("/api/tasks")
def create_task(req: TaskCreate):
    import time
    task_id = f"task_{int(time.time())}"
    db.upsert_task(
        task_id=task_id,
        title=req.title,
        source="manual",
        course_code=req.course_code or "General",
        term=req.term or "26S1",
        due_date=req.due_date or "",
        estimated_minutes=req.estimated_minutes,
        priority_score=req.priority_score,
    )
    return {"success": True, "task_id": task_id}

@app.post("/api/tasks/{task_id}/toggle")
def toggle_task(task_id: str):
    tasks = db.get_tasks(term="All")
    found = next((t for t in tasks if t["id"] == task_id), None)
    if not found:
        raise HTTPException(status_code=404, detail="Task not found")
    new_status = "completed" if found["status"] != "completed" else "pending"
    db.set_task_status(task_id, new_status)
    return {"success": True, "new_status": new_status}


@app.delete("/api/tasks/{task_id}")
@app.post("/api/tasks/{task_id}/delete")
def delete_task_endpoint(task_id: str):
    db.delete_task(task_id)
    return {"success": True, "deleted_id": task_id}

@app.get("/api/schedule")
def get_schedule(term: str = Query("26S1")):
    return generate_day_schedule(term=term, available_hours=6.0)

@app.post("/api/chat")
def chat(req: ChatRequest):
    return execute_chat_query(req.message, term=req.term or "26S1", session_id=req.session_id or "default")

@app.get("/api/chat/history")
def chat_history(session_id: str = Query("default")):
    return db.get_chat_history(session_id=session_id)

@app.post("/api/download/material")
def download_material(req: DownloadRequest, bg: BackgroundTasks):
    def _dl():
        ntulearn_service.download_material_file(req.mat_id, req.course_code, req.title, req.download_url, req.file_name or "")
    bg.add_task(_dl)
    return {"message": f"Downloading {req.title}..."}


# Timetable Intelligence & Extraction Endpoints
@app.get("/api/timetable")
def get_timetable(term: str = Query("26S1")):
    tt = timetable_service.get_active_timetable(term=term)
    if not tt:
        tt = timetable_service.seed_default_if_needed(term=term)
    if not tt:
        return {"has_timetable": False, "timetable": None}
    return {
        "has_timetable": True,
        "timetable": tt
    }

@app.post("/api/timetable/upload")
async def upload_timetable(file: UploadFile = File(...), term: str = Form("26S1")):
    if not file.filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
        raise HTTPException(status_code=400, detail="Only PDF and image timetable documents are supported.")

    save_path = timetable_service.timetable_dir / f"uploaded_{file.filename}"
    with open(save_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)

    try:
        parsed = timetable_service.parse_timetable_pdf(save_path)
        saved = timetable_service.save_timetable(parsed, save_path, file.filename)
        return {
            "success": True,
            "message": f"Successfully extracted timetable for {parsed.get('student_name', 'Student')}",
            "data": parsed
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract timetable: {str(e)}")

@app.get("/api/timetable/file")
def get_timetable_file(term: str = Query("26S1")):
    tt = timetable_service.get_active_timetable(term=term)
    if not tt or not tt.get("file_path"):
        raise HTTPException(status_code=404, detail="No timetable document uploaded.")
    fpath = Path(tt["file_path"])
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Timetable document missing on disk.")
    media_type = "application/pdf" if fpath.suffix.lower() == ".pdf" else "application/octet-stream"
    return FileResponse(
        str(fpath),
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename=\"{fpath.name}\""}
    )


if __name__ == "__main__":
    print(f"Starting {APP_NAME} on http://{HOST}:{PORT}")
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)


@app.get("/api/tokens/usage")
def get_tokens_usage():
    summary = db.get_token_usage_summary()
    llm_status = llm_provider.get_status()
    
    XKIRO_DAILY_LIMIT = int(os.getenv("XKIRO_DAILY_TOKEN_LIMIT", 5000000))
    GROQ_DAILY_LIMIT = int(os.getenv("GROQ_DAILY_TOKEN_LIMIT", 500000))
    GEMINI_DAILY_LIMIT = int(os.getenv("GEMINI_DAILY_TOKEN_LIMIT", 1000000))
    
    xkiro_used_today = 0
    groq_used_today = 0
    gemini_used_today = 0
    for m in summary.get("by_model", []):
        prov = m.get("provider", "").lower()
        if "xkiro" in prov or "deepseek" in prov:
            xkiro_used_today += m.get("tokens_today", 0)
        elif "groq" in prov:
            groq_used_today += m.get("tokens_today", 0)
        elif "gemini" in prov:
            gemini_used_today += m.get("tokens_today", 0)
            
    xkiro_remaining = max(0, XKIRO_DAILY_LIMIT - xkiro_used_today)
    groq_remaining = max(0, GROQ_DAILY_LIMIT - groq_used_today)
    gemini_remaining = max(0, GEMINI_DAILY_LIMIT - gemini_used_today)
    
    return {
        "status": "ok",
        "llm_status": llm_status,
        "summary": summary,
        "limits": {
            "bedrock": {
                "name": "AWS Bedrock",
                "model": "us.anthropic.claude-sonnet-4-5",
                "daily_limit": "AWS Enterprise / Pay-As-You-Go",
                "used_today": sum([m.get("tokens_today", 0) for m in summary.get("by_model", []) if "bedrock" in (m.get("provider") or "").lower()]),
                "remaining_today": "Active" if llm_status.get("bedrock_active", False) else "Disabled",
                "percent_used": 0,
                "is_active": llm_status.get("bedrock_active", False)
            },
            "xkiro": {
                "name": "XKiro (DeepSeek V4 Pro)",
                "model": os.getenv("XKIRO_MODEL", "deepseek/deepseek-v4-pro"),
                "daily_limit": XKIRO_DAILY_LIMIT,
                "used_today": xkiro_used_today,
                "remaining_today": xkiro_remaining,
                "percent_used": round((xkiro_used_today / XKIRO_DAILY_LIMIT) * 100, 2) if XKIRO_DAILY_LIMIT else 0,
                "is_active": llm_status.get("xkiro_active", False)
            },
            "groq": {
                "name": "Groq Cloud",
                "model": os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
                "daily_limit": GROQ_DAILY_LIMIT,
                "used_today": groq_used_today,
                "remaining_today": groq_remaining,
                "percent_used": round((groq_used_today / GROQ_DAILY_LIMIT) * 100, 2) if GROQ_DAILY_LIMIT else 0,
                "is_active": llm_status.get("groq_active", False)
            },
            "gemini": {
                "name": "Google Gemini",
                "model": os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
                "daily_limit": GEMINI_DAILY_LIMIT,
                "used_today": gemini_used_today,
                "remaining_today": gemini_remaining,
                "percent_used": round((gemini_used_today / GEMINI_DAILY_LIMIT) * 100, 2) if GEMINI_DAILY_LIMIT else 0,
                "is_active": llm_status.get("gemini_active", False)
            },
            "local": {
                "name": "Specialist Local Engine",
                "model": "Rule & SQLite Subagents",
                "daily_limit": "Unlimited",
                "used_today": 0,
                "remaining_today": "Unlimited",
                "percent_used": 0,
                "is_active": True
            }
        }
    }

@app.post("/api/tokens/test-ping")
def test_token_ping():
    """Executes a 1-sentence live diagnostic test ping to verify tokens and latency."""
    msg = [{"role": "user", "content": "Ping test from CogniFlow token monitor. Respond with 'PONG' and your model name in under 6 words."}]
    res_msg, provider = llm_provider.chat_completion(msg, max_tokens=25, temperature=0.1)
    reply = res_msg.content if res_msg and hasattr(res_msg, "content") else "No response"
    return {
        "status": "ok",
        "provider_used": provider,
        "reply": reply
    }

@app.post("/api/tokens/reset")
def reset_tokens():
    db.clear_token_usage()
    return {"status": "ok", "message": "Token usage history cleared"}


# Auto-start Telegram Bot in background thread if configured
@app.on_event("startup")
def startup_telegram_bot():
    from core.config import TELEGRAM_BOT_TOKEN
    if TELEGRAM_BOT_TOKEN:
        try:
            telegram_bot_service.start_polling(daemon=True)
            print(f"[CogniFlow] Telegram Bot polling initialized in background thread.")
        except Exception as e:
            print(f"[CogniFlow] Warning: Could not auto-start Telegram bot: {e}")
