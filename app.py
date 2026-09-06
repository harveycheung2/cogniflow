import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from core.config import (
    APP_NAME, APP_VERSION, HOST, PORT, BASE_DIR
)
import core.database as db
from services.aws_bedrock import bedrock_client
from services.ntulearn import ntulearn_service
from services.planner import generate_day_schedule, extract_tasks_from_announcements
from services.agents import execute_chat_query

# Initialize DB
db.init_db()

app = FastAPI(title=APP_NAME, version=APP_VERSION)
STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Models
class ChatRequest(BaseModel):
    message: str

class TaskCreate(BaseModel):
    title: str
    course_code: Optional[str] = "General"
    due_date: Optional[str] = ""
    estimated_minutes: int = 60
    priority_score: float = 5.0

class DownloadRequest(BaseModel):
    course_code: str
    title: str
    download_url: str

# In-memory background task tracker
sync_job = {"status": "idle", "message": ""}

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/api/status")
def get_status():
    bedrock_info = bedrock_client.get_account_identity()
    ntulearn_auth = ntulearn_service.is_authenticated()
    courses = db.get_all_courses()
    tasks = db.get_tasks()

    return {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "bedrock": bedrock_info,
        "ntulearn_authenticated": ntulearn_auth,
        "course_count": len(courses),
        "task_count": len(tasks),
        "sync_job": sync_job,
    }

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
def sync_ntulearn(bg: BackgroundTasks):
    if sync_job["status"] == "running":
        return {"error": "Sync already in progress"}

    def _sync():
        sync_job["status"] = "running"
        sync_job["message"] = "Syncing enrolled courses & announcements from Blackboard Ultra..."
        res = ntulearn_service.sync_courses_and_announcements()
        if res.get("success"):
            extract_tasks_from_announcements()
            sync_job["status"] = "completed"
            sync_job["message"] = f"Synced {res.get('courses_synced')} courses and {res.get('announcements_synced')} announcements."
        else:
            sync_job["status"] = "error"
            sync_job["message"] = res.get("error", "Sync failed")

    bg.add_task(_sync)
    return {"message": "Sync started"}

@app.get("/api/courses")
def get_courses():
    return db.get_all_courses()

@app.get("/api/courses/{course_id}/materials")
def get_course_materials(course_id: str):
    materials = db.get_course_materials(course_id)
    if not materials:
        materials = ntulearn_service.fetch_course_contents(course_id)
    return materials

@app.get("/api/announcements")
def get_announcements():
    return db.get_announcements(limit=30)

@app.get("/api/tasks")
def get_tasks(status: Optional[str] = None):
    return db.get_tasks(status=status)

@app.post("/api/tasks")
def create_task(req: TaskCreate):
    task_id = f"task_{int(sys.time.time())}" if hasattr(sys, 'time') else f"task_{os.urandom(4).hex()}"
    db.upsert_task(
        task_id=task_id,
        title=req.title,
        source="manual",
        course_code=req.course_code or "General",
        due_date=req.due_date or "",
        estimated_minutes=req.estimated_minutes,
        priority_score=req.priority_score,
    )
    return {"success": True, "task_id": task_id}

@app.post("/api/tasks/{task_id}/toggle")
def toggle_task(task_id: str):
    tasks = db.get_tasks()
    found = next((t for t in tasks if t["id"] == task_id), None)
    if not found:
        raise HTTPException(status_code=404, detail="Task not found")
    new_status = "completed" if found["status"] != "completed" else "pending"
    db.set_task_status(task_id, new_status)
    return {"success": True, "new_status": new_status}

@app.get("/api/schedule")
def get_schedule():
    return generate_day_schedule(available_hours=6.0)

@app.post("/api/chat")
def chat(req: ChatRequest):
    return execute_chat_query(req.message)

@app.get("/api/chat/history")
def chat_history():
    return db.get_chat_history()

@app.post("/api/download/material")
def download_material(req: DownloadRequest, bg: BackgroundTasks):
    def _dl():
        ntulearn_service.download_material_file(req.course_code, req.title, req.download_url)
    bg.add_task(_dl)
    return {"message": f"Downloading {req.title}..."}

if __name__ == "__main__":
    print(f"Starting {APP_NAME} on http://{HOST}:{PORT}")
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
