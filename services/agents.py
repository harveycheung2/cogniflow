import json
from typing import List, Dict, Any
from services.aws_bedrock import bedrock_client
from core.config import LEAD_MODEL_ID, SPECIALIST_MODEL_ID
import core.database as db
from services.planner import generate_day_schedule

LEAD_SYSTEM_PROMPT = """You are the Lead Orchestrator of the Agentic Workday OS.
Your role is to help university students and busy knowledge workers master their day by synthesizing course materials, deadlines, announcements, emails, and daily time-blocked schedules.

You have access to specialist capabilities:
1. [NTULearn Specialist]: Enrolled courses, syllabi, lecture slides, assignments, and blackboard announcements.
2. [Outlook Specialist]: Email triage, urgent messages, action item extraction.
3. [Planner Engine]: Task ranking by priority, urgency, and calendar blocking.

When answering:
- Be concise, structured, actionable, and encouraging.
- When organizing work, present clear bullet points with estimated durations.
- Highlight urgent deadlines with 🚨 or ⏳ badges.
- Always sign off or tag key agent contributions with `[Lead Agent]`, `[NTULearn Specialist]`, or `[Planner]`.
"""

def execute_chat_query(user_query: str) -> Dict[str, Any]:
    """Execute multi-agent reasoning flow for user query."""
    # Gather live context from SQLite
    courses = db.get_all_courses()
    tasks = db.get_tasks()
    announcements = db.get_announcements(limit=8)
    schedule = generate_day_schedule(available_hours=6.0)

    # Build context snapshot
    courses_summary = ", ".join([f"{c['course_code']} ({c['title']})" for c in courses[:6]]) or "None synced yet."
    pending_tasks = [f"- {t['title']} [Course: {t.get('course_code', 'N/A')}, Due: {t.get('due_date', 'None')}, Score: {t.get('priority_score')}]" for t in tasks[:8]]
    tasks_summary = "\n".join(pending_tasks) or "No pending tasks recorded."
    recent_ann = "\n".join([f"- [{a.get('course_code')}]: {a.get('title')}" for a in announcements[:5]]) or "No recent announcements."

    context_prompt = f"""
Current Student Context:
- Active Courses: {courses_summary}
- Top Prioritized Tasks:
{tasks_summary}
- Recent NTULearn Announcements:
{recent_ann}
- Tentative Today Schedule:
{json.dumps(schedule[:4], indent=2)}

User Question: {user_query}
"""

    messages = [
        {"role": "user", "content": context_prompt}
    ]

    response_text = bedrock_client.converse(
        messages=messages,
        system_prompt=LEAD_SYSTEM_PROMPT,
        model_id=LEAD_MODEL_ID,
        max_tokens=1000,
        temperature=0.2,
    )

    # Save to history
    db.save_chat_message("user", user_query, agent_name="User")
    db.save_chat_message("assistant", response_text, agent_name="Lead Orchestrator")

    return {
        "reply": response_text,
        "agent": "Lead Orchestrator",
        "model": LEAD_MODEL_ID,
    }
