from datetime import datetime, timedelta
import re
from typing import List, Dict, Any
import core.database as db

def calculate_priority(due_date_str: str, estimated_mins: int = 60, is_graded: bool = True) -> float:
    base_score = 5.0
    if not due_date_str:
        return base_score

    try:
        due = datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
        now = datetime.now(due.tzinfo)
        diff_hours = (due - now).total_seconds() / 3600.0

        if diff_hours < 0:
            return 10.0
        elif diff_hours < 24:
            urgency = 9.5
        elif diff_hours < 48:
            urgency = 8.5
        elif diff_hours < 120:
            urgency = 7.0
        else:
            urgency = 5.0

        if is_graded:
            urgency += 0.5
        return min(10.0, round(urgency, 1))
    except Exception:
        return base_score

def extract_tasks_from_announcements():
    announcements = db.get_announcements(limit=50)
    deadline_patterns = [
        (r'\b(?:homework|hw)\b', 8.5, 60),
        (r'\b(?:ca1|ca2|continuous assessment)\b', 9.0, 90),
        (r'\b(?:quiz|test|exam)\b', 9.2, 90),
        (r'\b(?:lab|laboratory|grouping)\b', 7.5, 60),
        (r'\b(?:tutorial|sheet)\b', 7.0, 45),
        (r'\b(?:due|submit|deadline|submission)\b', 8.8, 60),
        (r'\b(?:project|milestone|presentation)\b', 8.0, 120),
    ]

    for ann in announcements:
        title = str(ann.get("title", ""))
        body = str(ann.get("body", ""))
        text = f"{title} {body}".lower()

        matched = False
        priority = 6.0
        est_mins = 60

        for pattern, p_score, mins in deadline_patterns:
            if re.search(pattern, text):
                matched = True
                priority = max(priority, p_score)
                est_mins = mins

        if matched:
            task_id = f"ann_{ann.get('id')}"
            clean_title = title.replace("IMP/", "").replace("IMP:", "").strip()
            course_code = ann.get("course_code", "")
            
            date_match = re.search(r'([A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?)', text)
            if date_match:
                due_date = date_match.group(1)
            else:
                due_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

            db.upsert_task(
                task_id=task_id,
                title=f"Action: {clean_title[:80]}",
                source="ntulearn",
                course_code=course_code,
                due_date=due_date,
                estimated_minutes=est_mins,
                priority_score=priority,
                notes=body[:200],
            )

def generate_day_schedule(available_hours: float = 6.0) -> List[Dict[str, Any]]:
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

        current_time = end_time + timedelta(minutes=15)
        total_minutes_left -= (duration + 15)

    return schedule
