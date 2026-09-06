import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from core.config import DB_PATH

def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id TEXT PRIMARY KEY,
            course_code TEXT,
            title TEXT,
            term TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS course_materials (
            id TEXT PRIMARY KEY,
            course_id TEXT,
            title TEXT,
            content_type TEXT,
            url TEXT,
            download_url TEXT,
            parent_id TEXT,
            local_path TEXT,
            downloaded INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (course_id) REFERENCES courses (id)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id TEXT PRIMARY KEY,
            course_id TEXT,
            course_code TEXT,
            title TEXT,
            body TEXT,
            posted_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT DEFAULT 'manual', -- 'ntulearn', 'email', 'manual'
            course_code TEXT,
            due_date TEXT,
            estimated_minutes INTEGER DEFAULT 60,
            priority_score REAL DEFAULT 5.0,
            status TEXT DEFAULT 'pending', -- 'pending', 'in_progress', 'completed'
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT DEFAULT 'default',
            role TEXT NOT NULL, -- 'user', 'assistant', 'system'
            agent_name TEXT DEFAULT 'Lead Orchestrator',
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.commit()

def upsert_course(course_id: str, course_code: str, title: str, term: str):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO courses (id, course_code, title, term, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            course_code=excluded.course_code,
            title=excluded.title,
            term=excluded.term,
            updated_at=CURRENT_TIMESTAMP
        """, (course_id, course_code, title, term))
        conn.commit()

def upsert_announcement(ann_id: str, course_id: str, course_code: str, title: str, body: str, posted_at: str):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO announcements (id, course_id, course_code, title, body, posted_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            body=excluded.body,
            posted_at=excluded.posted_at
        """, (ann_id, course_id, course_code, title, body, posted_at))
        conn.commit()

def upsert_material(mat_id: str, course_id: str, title: str, content_type: str, url: str, download_url: str, parent_id: str = ""):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO course_materials (id, course_id, title, content_type, url, download_url, parent_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            content_type=excluded.content_type,
            download_url=excluded.download_url
        """, (mat_id, course_id, title, content_type, url, download_url, parent_id))
        conn.commit()

def upsert_task(task_id: str, title: str, source: str, course_code: str, due_date: str, estimated_minutes: int, priority_score: float, notes: str = ""):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO tasks (id, title, source, course_code, due_date, estimated_minutes, priority_score, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            due_date=excluded.due_date,
            estimated_minutes=excluded.estimated_minutes,
            priority_score=excluded.priority_score,
            notes=excluded.notes,
            updated_at=CURRENT_TIMESTAMP
        """, (task_id, title, source, course_code, due_date, estimated_minutes, priority_score, notes))
        conn.commit()

def get_tasks(status: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        if status:
            rows = conn.execute("SELECT * FROM tasks WHERE status = ? ORDER BY priority_score DESC, due_date ASC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY status ASC, priority_score DESC, due_date ASC").fetchall()
        return [dict(r) for r in rows]

def set_task_status(task_id: str, new_status: str):
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_status, task_id))
        conn.commit()

def get_all_courses() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM courses ORDER BY term DESC, course_code ASC").fetchall()
        return [dict(r) for r in rows]

def get_announcements(limit: int = 20) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM announcements ORDER BY posted_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def get_course_materials(course_id: str) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM course_materials WHERE course_id = ? ORDER BY title ASC", (course_id,)).fetchall()
        return [dict(r) for r in rows]

def save_chat_message(role: str, content: str, agent_name: str = "Lead Orchestrator", session_id: str = "default"):
    with get_connection() as conn:
        conn.execute("INSERT INTO chat_messages (session_id, role, agent_name, content) VALUES (?, ?, ?, ?)",
                     (session_id, role, agent_name, content))
        conn.commit()

def get_chat_history(session_id: str = "default", limit: int = 50) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id ASC LIMIT ?", (session_id, limit)).fetchall()
        return [dict(r) for r in rows]
