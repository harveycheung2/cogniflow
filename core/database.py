import re
import sqlite3
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from core.config import DB_PATH

def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
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
            course_code TEXT,
            title TEXT,
            content_type TEXT,
            url TEXT,
            download_url TEXT,
            parent_id TEXT,
            local_path TEXT,
            downloaded INTEGER DEFAULT 0,
            file_size INTEGER DEFAULT 0,
            file_name TEXT,
            doc_type TEXT DEFAULT 'UNKNOWN',
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
            source TEXT DEFAULT 'manual',
            course_code TEXT,
            term TEXT DEFAULT '26S1',
            due_date TEXT,
            estimated_minutes INTEGER DEFAULT 60,
            priority_score REAL DEFAULT 5.0,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            latency_ms REAL DEFAULT 0.0,
            status TEXT DEFAULT 'success',
            endpoint TEXT DEFAULT 'chat',
            error_message TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage(created_at)
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT DEFAULT 'default',
            role TEXT NOT NULL,
            agent_name TEXT DEFAULT 'Lead Orchestrator',
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS parsed_documents (
            id TEXT PRIMARY KEY,
            course_id TEXT,
            course_code TEXT,
            title TEXT,
            file_name TEXT,
            doc_type TEXT DEFAULT 'UNKNOWN',
            week_number INTEGER DEFAULT 0,
            topic TEXT,
            summary TEXT,
            local_path TEXT,
            schedule_data_json TEXT,
            raw_text_excerpt TEXT,
            extracted_questions TEXT,
            is_solution INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS course_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_code TEXT NOT NULL,
            week_number INTEGER DEFAULT 0,
            event_type TEXT DEFAULT 'lecture',
            title TEXT NOT NULL,
            event_date TEXT,
            source_doc_id TEXT,
            source_doc_title TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS timetable_documents (
            id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            student_name TEXT,
            term TEXT DEFAULT '26S1',
            total_courses INTEGER DEFAULT 0,
            total_aus INTEGER DEFAULT 0,
            data_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)


        # Migrations for existing tables
        columns_to_add = [
            ('course_materials', 'course_code', 'TEXT'),
            ('course_materials', 'file_size', 'INTEGER DEFAULT 0'),
            ('course_materials', 'file_name', 'TEXT'),
            ('course_materials', 'doc_type', "TEXT DEFAULT 'UNKNOWN'"),
            ('tasks', 'term', "TEXT DEFAULT '26S1'"),
            ('parsed_documents', 'raw_text_excerpt', 'TEXT'),
            ('parsed_documents', 'extracted_questions', 'TEXT'),
            ('parsed_documents', 'is_solution', 'INTEGER DEFAULT 0'),
        ]
        for tbl, col, col_type in columns_to_add:
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_type}")
            except Exception:
                pass

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

def upsert_material(mat_id: str, course_id: str, title: str, content_type: str, url: str,
                    download_url: str, parent_id: str = "", course_code: str = "",
                    local_path: str = "", downloaded: int = 0, file_size: int = 0,
                    file_name: str = "", doc_type: str = "UNKNOWN"):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO course_materials (id, course_id, course_code, title, content_type, url, download_url, parent_id, local_path, downloaded, file_size, file_name, doc_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            course_code=COALESCE(NULLIF(excluded.course_code, ''), course_materials.course_code),
            content_type=excluded.content_type,
            download_url=excluded.download_url,
            parent_id=excluded.parent_id,
            local_path=CASE WHEN excluded.local_path != '' THEN excluded.local_path ELSE course_materials.local_path END,
            downloaded=MAX(course_materials.downloaded, excluded.downloaded),
            file_size=CASE WHEN excluded.file_size > 0 THEN excluded.file_size ELSE course_materials.file_size END,
            file_name=COALESCE(NULLIF(excluded.file_name, ''), course_materials.file_name),
            doc_type=CASE WHEN excluded.doc_type != 'UNKNOWN' THEN excluded.doc_type ELSE course_materials.doc_type END
        """, (mat_id, course_id, course_code, title, content_type, url, download_url, parent_id, local_path, downloaded, file_size, file_name, doc_type))
        conn.commit()

def mark_material_downloaded(mat_id: str, local_path: str, file_size: int, file_name: str, doc_type: str = "UNKNOWN"):
    with get_connection() as conn:
        conn.execute("""
        UPDATE course_materials
        SET local_path = ?, downloaded = 1, file_size = ?, file_name = ?,
            doc_type = CASE WHEN ? != 'UNKNOWN' THEN ? ELSE doc_type END
        WHERE id = ?
        """, (local_path, file_size, file_name, doc_type, doc_type, mat_id))
        conn.commit()

def upsert_parsed_document(doc_id: str, course_id: str, course_code: str, title: str,
                           file_name: str, doc_type: str, week_number: int, topic: str,
                           summary: str, local_path: str, schedule_data_json: str,
                           raw_text_excerpt: str = "", extracted_questions: str = "[]", is_solution: int = 0):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO parsed_documents (id, course_id, course_code, title, file_name, doc_type, week_number, topic, summary, local_path, schedule_data_json, raw_text_excerpt, extracted_questions, is_solution, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            course_code=excluded.course_code,
            title=excluded.title,
            file_name=excluded.file_name,
            doc_type=excluded.doc_type,
            week_number=excluded.week_number,
            topic=excluded.topic,
            summary=excluded.summary,
            local_path=excluded.local_path,
            schedule_data_json=excluded.schedule_data_json,
            raw_text_excerpt=excluded.raw_text_excerpt,
            extracted_questions=excluded.extracted_questions,
            is_solution=excluded.is_solution,
            updated_at=CURRENT_TIMESTAMP
        """, (doc_id, course_id, course_code, title, file_name, doc_type, week_number, topic, summary, local_path, schedule_data_json, raw_text_excerpt, extracted_questions, is_solution))

        conn.execute("""
        UPDATE course_materials
        SET doc_type = ?
        WHERE id = ?
        """, (doc_type, doc_id))
        conn.commit()

def upsert_course_schedule(course_code: str, week_number: int, event_type: str, title: str,
                           event_date: str = "", source_doc_id: str = "", source_doc_title: str = "", notes: str = ""):
    with get_connection() as conn:
        existing = conn.execute("""
        SELECT id FROM course_schedules
        WHERE course_code = ? AND week_number = ? AND title = ? AND event_type = ?
        """, (course_code, week_number, title, event_type)).fetchone()

        if existing:
            conn.execute("""
            UPDATE course_schedules
            SET event_date = ?, source_doc_id = ?, source_doc_title = ?, notes = ?
            WHERE id = ?
            """, (event_date, source_doc_id, source_doc_title, notes, existing["id"]))
        else:
            conn.execute("""
            INSERT INTO course_schedules (course_code, week_number, event_type, title, event_date, source_doc_id, source_doc_title, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (course_code, week_number, event_type, title, event_date, source_doc_id, source_doc_title, notes))
        conn.commit()

def get_course_schedules(course_code: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        if course_code and course_code.upper() != "ALL":
            rows = conn.execute("""
            SELECT * FROM course_schedules
            WHERE course_code LIKE ?
            ORDER BY week_number ASC, id ASC
            """, (f"%{course_code.upper()}%",)).fetchall()
        else:
            rows = conn.execute("""
            SELECT * FROM course_schedules
            ORDER BY course_code ASC, week_number ASC, id ASC
            """).fetchall()
        return [dict(r) for r in rows]

def get_parsed_documents(course_code: Optional[str] = None, doc_type: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        query = "SELECT * FROM parsed_documents WHERE 1=1"
        params = []
        if course_code and course_code.upper() != "ALL":
            query += " AND course_code LIKE ?"
            params.append(f"%{course_code.upper()}%")
        if doc_type and doc_type.upper() != "ALL":
            query += " AND doc_type = ?"
            params.append(doc_type.upper())
        query += " ORDER BY course_code ASC, week_number ASC, title ASC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def get_material_by_id(mat_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM course_materials WHERE id = ?", (mat_id,)).fetchone()
        return dict(row) if row else None

def get_all_materials(course_code: Optional[str] = None, downloaded_only: bool = False) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        query = "SELECT * FROM course_materials WHERE 1=1"
        params = []
        if course_code and course_code.upper() != "ALL":
            query += " AND (course_code LIKE ? OR course_id IN (SELECT id FROM courses WHERE course_code LIKE ?))"
            params.append(f"%{course_code.upper()}%")
            params.append(f"%{course_code.upper()}%")
        if downloaded_only:
            query += " AND downloaded = 1"
        query += " ORDER BY course_code ASC, title ASC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def search_documents_by_content(query: str, course_code: Optional[str] = None, solution_only: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Deep full-text search across document title, extracted text, and questions with multi-keyword support."""
    with get_connection() as conn:
        # Split query into meaningful words (e.g. "MS3082", "Lab", "Grouping" -> "groups", "grouping")
        clean_words = [w.strip() for w in re.split(r'\s+', query) if len(w.strip()) >= 3]
        if not clean_words:
            clean_words = [query]

        conditions = []
        params = []
        for word in clean_words:
            w_wild = f"%{word}%"
            conditions.append("""(
                m.title LIKE ? OR p.topic LIKE ? OR p.summary LIKE ? OR p.raw_text_excerpt LIKE ? OR p.extracted_questions LIKE ?
            )""")
            params.extend([w_wild, w_wild, w_wild, w_wild, w_wild])

        # If "grouping" or "group" in query, also match "groups"
        if any("group" in w.lower() for w in clean_words):
            conditions.append("(m.title LIKE '%group%' OR m.title LIKE '%groups%' OR p.raw_text_excerpt LIKE '%group%')")

        where_clause = " OR ".join(conditions) if conditions else "1=1"
        sql = f"""
        SELECT m.id, m.course_code, m.title, m.doc_type, m.local_path, m.downloaded, m.file_size,
               p.topic, p.summary, p.week_number, p.raw_text_excerpt, p.extracted_questions, p.is_solution
        FROM course_materials m
        LEFT JOIN parsed_documents p ON m.id = p.id
        WHERE ({where_clause})
        """

        if course_code and course_code.upper() != "ALL":
            sql += " AND (m.course_code LIKE ? OR p.course_code LIKE ?)"
            params.append(f"%{course_code.upper()}%")
            params.append(f"%{course_code.upper()}%")

        if solution_only is True:
            sql += " AND (p.is_solution = 1 OR m.title LIKE '%sol%' OR m.title LIKE '%answer%')"
        elif solution_only is False:
            sql += " AND (p.is_solution = 0 AND m.title NOT LIKE '%sol%' AND m.title NOT LIKE '%answer%')"

        sql += " ORDER BY (CASE WHEN lower(m.title) LIKE '%group%' OR lower(m.title) LIKE '%roster%' THEN 0 ELSE 1 END), m.downloaded DESC, p.week_number ASC, m.title ASC LIMIT 15"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

def find_matching_tutorials(course_code: Optional[str] = None, query: str = "") -> List[Dict[str, Any]]:
    """Finds tutorials with their question and solution pairs."""
    with get_connection() as conn:
        sql = """
        SELECT m.*, p.summary, p.topic, p.week_number, p.is_solution, p.raw_text_excerpt, p.extracted_questions
        FROM course_materials m
        LEFT JOIN parsed_documents p ON m.id = p.id
        WHERE (
            m.title LIKE '%tutorial%' OR m.title LIKE '% tut %' OR m.title LIKE '% t1%' OR m.title LIKE '% t2%' OR m.title LIKE '% t3%'
            OR m.title LIKE '%edx%' OR m.title LIKE '%ir %' OR m.title LIKE '%xrd%' OR m.title LIKE '%xrf%' OR m.title LIKE '%xps%'
            OR m.doc_type LIKE '%TUTORIAL%' OR (p.doc_type IS NOT NULL AND p.doc_type LIKE '%TUTORIAL%')
        )
        """
        params = []
        if course_code and course_code.upper() != "ALL":
            sql += " AND (m.course_code LIKE ? OR m.course_id IN (SELECT id FROM courses WHERE course_code LIKE ?))"
            params.append(f"%{course_code.upper()}%")
            params.append(f"%{course_code.upper()}%")
        if query:
            sql += " AND (m.title LIKE ? OR p.summary LIKE ? OR p.topic LIKE ? OR p.raw_text_excerpt LIKE ?)"
            params.append(f"%{query}%")
            params.append(f"%{query}%")
            params.append(f"%{query}%")
            params.append(f"%{query}%")
        sql += " ORDER BY m.course_code ASC, p.is_solution ASC, m.title ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

def upsert_task(task_id: str, title: str, source: str, course_code: str, due_date: str, estimated_minutes: int, priority_score: float, notes: str = "", term: str = "26S1"):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO tasks (id, title, source, course_code, term, due_date, estimated_minutes, priority_score, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            term=excluded.term,
            due_date=excluded.due_date,
            estimated_minutes=excluded.estimated_minutes,
            priority_score=excluded.priority_score,
            notes=excluded.notes,
            updated_at=CURRENT_TIMESTAMP
        """, (task_id, title, source, course_code, term, due_date, estimated_minutes, priority_score, notes))
        conn.commit()

def clear_announcement_tasks():
    with get_connection() as conn:
        conn.execute("DELETE FROM tasks WHERE source = 'ntulearn'")
        conn.commit()

def get_tasks(term: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if term and term.upper() != "ALL":
            query += " AND (term = ? OR course_code LIKE ?)"
            params.append(term.upper())
            params.append(f"%{term.upper()}%")
        query += " ORDER BY status ASC, priority_score DESC, due_date ASC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

def set_task_status(task_id: str, new_status: str):
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_status, task_id))
        conn.commit()


def delete_task(task_id: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()

def get_all_courses(term: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        if term and term.upper() != "ALL":
            rows = conn.execute("SELECT * FROM courses WHERE term = ? ORDER BY course_code ASC", (term.upper(),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM courses ORDER BY term DESC, course_code ASC").fetchall()
        return [dict(r) for r in rows]

def get_available_terms() -> List[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT term FROM courses WHERE term != 'General' ORDER BY term DESC").fetchall()
        terms = [r["term"] for r in rows if r["term"]]
        return terms

def get_announcements(term: Optional[str] = None, course_code: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        query = "SELECT * FROM announcements WHERE 1=1"
        params = []
        if term and term.upper() != "ALL":
            query += " AND course_code LIKE ?"
            params.append(f"%{term.upper()}%")
        if course_code and course_code.upper() != "ALL":
            clean_c = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', course_code, re.IGNORECASE)
            c_str = clean_c.group(1) if clean_c else course_code
            query += " AND course_code LIKE ?"
            params.append(f"%{c_str.upper()}%")
        query += " ORDER BY posted_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
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

def save_timetable_document(doc_id: str, file_name: str, file_path: str, file_size: int,
                           student_name: str, term: str, total_courses: int, total_aus: int, data_json: str):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO timetable_documents (id, file_name, file_path, file_size, student_name, term, total_courses, total_aus, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            file_name=excluded.file_name,
            file_path=excluded.file_path,
            file_size=excluded.file_size,
            student_name=excluded.student_name,
            term=excluded.term,
            total_courses=excluded.total_courses,
            total_aus=excluded.total_aus,
            data_json=excluded.data_json,
            updated_at=CURRENT_TIMESTAMP
        """, (doc_id, file_name, file_path, file_size, student_name, term, total_courses, total_aus, data_json))
        conn.commit()

def get_active_timetable(term: str = "26S1") -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM timetable_documents WHERE term = ? OR id = 'active_timetable' ORDER BY updated_at DESC LIMIT 1", (term,)).fetchone()
        if row:
            d = dict(row)
            if d.get("data_json"):
                try:
                    d["parsed_data"] = json.loads(d["data_json"])
                except Exception:
                    d["parsed_data"] = {}
            return d
        return None

def delete_active_timetable(term: str = "26S1"):
    with get_connection() as conn:
        conn.execute("DELETE FROM timetable_documents WHERE term = ? OR id = 'active_timetable'", (term,))
        conn.commit()


def record_token_usage(
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    latency_ms: float = 0.0,
    status: str = "success",
    endpoint: str = "chat",
    error_message: str = "",
):
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO token_usage 
                (provider, model, prompt_tokens, completion_tokens, total_tokens, latency_ms, status, endpoint, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (provider, model, prompt_tokens, completion_tokens, total_tokens, latency_ms, status, endpoint, error_message)
            )
            conn.commit()
    except Exception as e:
        print(f"[DB] Error recording token usage: {e}")

def get_token_usage_summary() -> Dict[str, Any]:
    with get_connection() as conn:
        today_clause = "date(created_at, 'localtime') = date('now', 'localtime')"
        
        overall = conn.execute("""
            SELECT 
                COUNT(*) as total_requests,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) as avg_latency
            FROM token_usage
        """).fetchone()
        
        today = conn.execute(f"""
            SELECT 
                COUNT(*) as requests_today,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens_today,
                COALESCE(SUM(completion_tokens), 0) as completion_tokens_today,
                COALESCE(SUM(total_tokens), 0) as tokens_today,
                COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) as avg_latency_today
            FROM token_usage
            WHERE {today_clause}
        """).fetchone()
        
        models_rows = conn.execute(f"""
            SELECT 
                provider,
                model,
                COUNT(*) as total_calls,
                SUM(CASE WHEN {today_clause} THEN 1 ELSE 0 END) as calls_today,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(CASE WHEN {today_clause} THEN total_tokens ELSE 0 END), 0) as tokens_today,
                COALESCE(SUM(CASE WHEN {today_clause} THEN prompt_tokens ELSE 0 END), 0) as prompt_tokens_today,
                COALESCE(SUM(CASE WHEN {today_clause} THEN completion_tokens ELSE 0 END), 0) as completion_tokens_today,
                COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) as avg_latency
            FROM token_usage
            GROUP BY provider, model
            ORDER BY calls_today DESC, total_calls DESC
        """).fetchall()
        
        recent_rows = conn.execute("""
            SELECT id, provider, model, prompt_tokens, completion_tokens, total_tokens, latency_ms, status, endpoint, created_at
            FROM token_usage
            ORDER BY id DESC
            LIMIT 35
        """).fetchall()
        
        return {
            "overall": dict(overall) if overall else {},
            "today": dict(today) if today else {},
            "by_model": [dict(r) for r in models_rows],
            "recent": [dict(r) for r in recent_rows]
        }

def clear_token_usage():
    with get_connection() as conn:
        conn.execute("DELETE FROM token_usage")
        conn.commit()
