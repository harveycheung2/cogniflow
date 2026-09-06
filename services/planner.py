from datetime import datetime, timedelta
import re
from typing import List, Dict, Any
import core.database as db

def calculate_priority(due_date_str: str, estimated_mins: int = 60, is_graded: bool = True) -> float:
    """Computes a 0.0 - 10.0 urgency score."""
    base_score = 5.0
    if not due_date_str:
        return base_score

    try:
        # Simple date parsing
        due = datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
        now = datetime.now(due.tzinfo)
        diff_hours = (due - now).total_seconds() / 3600.0

        if diff_hours < 0:
            return 10.0 # Overdue
        elif diff_hours < 24:
            urgency = 9.5
        elif diff_hours < 48:
            urgency = 8.5
        elif diff_hours < 120: # 5 days
            urgency = 7.0
        else:
            urgency = 4.0

        if is_graded:
            urgency += 0.5
        return min(10.0, round(urgency, 1))
    except Exception:
        return base_score

def extract_tasks_from_announcements():
    """Parse NTULearn announcements for deadlines and action items."""
    announcements = db.get_announcements(limit=30)
    deadline_patterns = [
        r'(?:due|submit|deadline|submission)\s+(?:by|on|at)?\s*([A-Za-z0-9,:\s]{4,25})',
        r'assignment\s*\d+',
        r'quiz\s*\d+',
        r'project\s+(?:milestone|report|proposal)',
    ]

    for ann in announcements:
        text = (ann.get("title", "") + " " + ann.get("body", "")).lower()
        has_action = any(re.search(p, text) for p in deadline_patterns)
        if has_action:
            task_id = f"ann_{ann.get('id')}"
            title = f"Review: {ann.get('title')}"
            course_code = ann.get("course_code", "")
            priority = 7.5 if "due" in text or "deadline" in text else 6.0

            # Default due date to 3 days from now if not explicitly parsed
            due_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d 23:59")
            db.upsert_task(
                task_id=task_id,
                title=title,
                source="ntulearn",
                course_code=course_code,
                due_date=due_date,
                estimated_minutes=45,
                priority_score=priority,
                notes=ann.get("body", "")[:200],
            )

def generate_day_schedule(available_hours: float = 6.0) -> List[Dict[str, Any]]:
    """Generates an optimized, time-blocked schedule for the pending tasks."""
    pending = db.get_tasks(status="pending")
    if not pending:
        extract_tasks_from_announcements()
        pending = db.get_tasks(status="pending")

    schedule = []
    current_time = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    total_minutes_left = available_hours * 60

    for task in pending:
        duration = min(task.get("estimated_minutes", 60), 120)
        if total_minutes_left < 30:
            break

        start_str = current_time.strftime("%I:%M %p")
        end_time = current_time + timedelta(minutes=duration)
        end_str = end_time.strftime("%I:%M %p")

        schedule.append({
            "task_id": task["id"],
            "title": task["title"],
            "course_code": task.get("course_code", ""),
            "start": start_str,
            "end": end_str,
            "duration_mins": duration,
            "priority": task.get("priority_score", 5.0),
        })

        current_time = end_time + timedelta(minutes=15) # 15 min buffer
        total_minutes_left -= (duration + 15)

    return schedule
