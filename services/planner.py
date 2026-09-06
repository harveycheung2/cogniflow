from datetime import datetime, timedelta
import re
from typing import List, Dict, Any, Optional
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

def extract_tasks_from_announcements(term: str = "26S1"):
    """Extract action items exclusively for real urgent tests, exams, and deliverables.

    Informational notices (slides, surveys, group lists) are left in announcements
    and not duplicated as tasks.
    """
    db.clear_announcement_tasks()
    announcements = db.get_announcements(term=term, limit=60)

    # Strictly urgent deliverable and test patterns
    urgent_patterns = [
        # Tests & Exams (highest priority)
        (r'\b(?:final exam|finals|midterm|mid-term)\b', "Exam", 9.8, 120),
        (r'\b(?:test\s*1|test\s*2|test\s*3|quiz\s*\d+|quiz)\b', "Test", 9.5, 90),
        (r'\b(?:ca1|ca2|continuous assessment)\b', "Assessment", 9.3, 90),
        # Hard Deadlines & Submissions
        (r'\b(?:submission deadline|due date|submit by|deadline for|hard deadline)\b', "Submission", 9.0, 90),
        (r'\b(?:homework|hw\s*\d+|assignment\s*\d+|graded assignment)\b', "Assignment", 8.8, 75),
        (r'\b(?:project milestone|project report|consultation\s*#?\d*)\b', "Project", 8.5, 120),
    ]

    # Explicit ignore list for routine notifications
    ignore_patterns = [
        r'\b(?:pre-course survey|survey reminder|feedback form)\b',
        r'\b(?:recording(?:s)? available|lecture recording|slides uploaded|lecture slides)\b',
        r'\b(?:textbook online|table/group assigned|team assignment for lab)\b',
        r'\b(?:welcome to|general information|consultation hours)\b',
    ]

    for ann in announcements:
        title = str(ann.get("title", ""))
        body = str(ann.get("body", ""))
        text = f"{title} {body}".lower()

        # Check if this is an informational announcement to skip
        if any(re.search(pat, text) for pat in ignore_patterns) and not bool(re.search(r'\b(?:test\s*\d+|exam|quiz|deadline)\b', text)):
            continue

        matched_tag = None
        priority = 6.0
        est_mins = 60

        for pattern, tag, p_score, mins in urgent_patterns:
            if re.search(pattern, text):
                matched_tag = tag
                priority = max(priority, p_score)
                est_mins = mins
                break

        if matched_tag:
            task_id = f"ann_{ann.get('id')}"
            clean_title = title.replace("IMP/", "").replace("IMP:", "").replace("Resend:", "").strip()
            raw_course = ann.get("course_code", "")
            # Clean course code (e.g. 26S1-MH2500-LEC -> MH2500)
            course_match = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', raw_course)
            short_course = course_match.group(1) if course_match else raw_course

            # Attempt to extract actual calendar date e.g. September 9, 2026 or "Week 4/5"
            date_match = re.search(r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?\b', text, re.IGNORECASE)
            if date_match:
                due_date = date_match.group(0)
            elif "week 4" in text:
                due_date = "Week 4"
            elif "week 5" in text:
                due_date = "Week 5 (Upcoming)"
            elif "test 1" in text or "ca1" in text:
                due_date = "Upcoming Test 1"
            else:
                due_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")

            db.upsert_task(
                task_id=task_id,
                title=f"[{matched_tag}] {short_course}: {clean_title[:70]}",
                source="ntulearn",
                course_code=short_course,
                term=term,
                due_date=due_date,
                estimated_minutes=est_mins,
                priority_score=priority,
                notes=body[:200],
            )

def generate_day_schedule(term: str = "26S1", available_hours: float = 6.0) -> List[Dict[str, Any]]:
    pending = db.get_tasks(term=term, status="pending")
    if not pending:
        extract_tasks_from_announcements(term=term)
        pending = db.get_tasks(term=term, status="pending")

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
