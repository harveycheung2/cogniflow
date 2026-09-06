import json
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from services.aws_bedrock import bedrock_client
from core.config import LEAD_MODEL_ID, SPECIALIST_MODEL_ID
import core.database as db
from services.planner import generate_day_schedule
from services.document_agent import document_agent
from services.ntulearn import ntulearn_service

LEAD_SYSTEM_PROMPT = """You are the Lead Orchestrator of Agentic Workday OS v2.
You supervise multiple specialized sub-agents to organize the student's academic life for Semester 1 (26S1).

Available Sub-Agents to Delegate to:
1. `task_document_specialist`: Inspects course slides, searches tutorial documents, extracts semester schedules, and retrieves specific PDF materials or test/exam information.
2. `task_planner_agent`: Generates optimized time-blocked study schedules and calculates urgency priority scores.
3. `task_ntulearn_specialist`: Checks recent announcements, CA deadlines, test venues, and course notices.

Guidelines:
- **Conversation Context Preservation**: Always remember context across conversation turns. If the student previously asked about a specific module (e.g., MH2500 Probability) and then follows up with questions like "what is tested in test 1?", "when is it?", or "give me practice problems", automatically relate the query to the course being discussed without asking for re-clarification.
- **Sub-Agent Delegation**: Whenever the student asks "what do I need to do", "which tutorial do I need", or asks about course content/schedules/tests, delegate to `task_document_specialist` and `task_ntulearn_specialist` to inspect the actual course documents and notices first.
- When you receive the Document Specialist's findings, cite the exact tutorial or test document name and include the clickable action token:
  `[OPEN_DOC:<material_id>:<Document Title>]`
- Structure your response cleanly with:
  - 🎯 **Immediate Priority & What to do now**
  - 📄 **Exact Tutorial / Test PDF Required & Action Link**
  - 📅 **Semester Schedule & Milestones**
  - 💡 **Recommended Study Plan**
- Professional, encouraging, and clear executive tone.
"""

TOOL_DEFINITIONS = [
    {
        "toolSpec": {
            "name": "task_document_specialist",
            "description": "Task the Document Specialist Sub-Agent to inspect, analyze, or search course documents, lecture slides, syllabus timetables, assessment prep, and tutorial question/solution sheets for a course.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "course_code": {
                            "type": "string",
                            "description": "Course code or keyword, e.g. 'MH2500', 'SC2001', 'SC2207', 'MH2802', 'CC0006'"
                        },
                        "task_type": {
                            "type": "string",
                            "enum": ["find_tutorial_pdf", "extract_schedule", "inspect_lecture_slides", "get_solutions", "find_test_or_exam", "general_search"],
                            "description": "Specific intent for the document agent"
                        },
                        "detail": {
                            "type": "string",
                            "description": "Description of what specific tutorial, test, or information is required"
                        }
                    },
                    "required": ["course_code", "task_type"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "task_planner_agent",
            "description": "Task the Planner Sub-Agent to calculate priority rankings, deadlines, and dynamic calendar time-blocking.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "available_hours": {
                            "type": "number",
                            "description": "Number of available study hours, e.g. 3.0, 6.0"
                        },
                        "focus_module": {
                            "type": "string",
                            "description": "Optional specific course code to prioritize"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "task_ntulearn_specialist",
            "description": "Task the NTULearn Specialist Sub-Agent to inspect announcements, deadlines, and module announcements from Blackboard Ultra.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "course_code": {
                            "type": "string",
                            "description": "Course code to filter announcements"
                        },
                        "query": {
                            "type": "string",
                            "description": "Search topic e.g. 'quiz', 'lab', 'homework'"
                        }
                    }
                }
            }
        }
    }
]

def execute_subagent_tool(name: str, args: Dict[str, Any], term: str = "26S1") -> Dict[str, Any]:
    """Dispatches tool call directly to the requested specialist sub-agent."""
    if name == "task_document_specialist":
        course_code = args.get("course_code", "")
        task_type = args.get("task_type", "general_search")
        detail = args.get("detail", "")

        # Clean course code (e.g. MH2500 from 26S1-MH2500-LEC)
        clean_code = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', course_code)
        target_code = clean_code.group(1) if clean_code else course_code

        all_mats = db.get_all_materials(course_code=target_code)

        # Check if looking specifically for tests, exams, mock tests, or assessment
        is_test_query = any(w in f"{task_type} {detail}".lower() for w in ["test", "exam", "quiz", "mock", "ca1", "ca2", "assessment"])
        is_syllabus_query = any(w in f"{task_type} {detail}".lower() for w in ["syllabus", "schedule", "timetable", "outline", "hand00"])

        if is_test_query:
            matching_mats = [
                m for m in all_mats
                if any(w in m.get("title", "").lower() for w in ["test", "mock", "exam", "quiz", "ca1", "ca2"])
            ]
            if not matching_mats:
                matching_mats = all_mats
        elif is_syllabus_query:
            matching_mats = [
                m for m in all_mats
                if any(w in m.get("title", "").lower() for w in ["hand00", "syllabus", "schedule", "outline", "overview", "lec 1", "lecture 1"])
            ]
            if not matching_mats:
                matching_mats = all_mats
        else:
            matching_mats = db.find_matching_tutorials(course_code=target_code)
            if not matching_mats:
                matching_mats = all_mats

        # Ensure key materials are analyzed if not yet analyzed
        for m in matching_mats[:4]:
            if not db.get_parsed_documents(course_code=m.get("course_code")):
                document_agent.analyze_document(m["id"])

        schedules = db.get_course_schedules(course_code=target_code)

        # Also retrieve course announcements for additional context
        anns = db.get_announcements(term=term, limit=15)
        course_anns = [
            a for a in anns
            if target_code.upper() in a.get("course_code", "").upper()
        ]

        docs_summary = [
            {
                "id": m["id"],
                "title": m["title"],
                "doc_type": m.get("doc_type", "UNKNOWN"),
                "downloaded": bool(m.get("downloaded")),
                "local_path": m.get("local_path", "")
            }
            for m in matching_mats[:8]
        ]

        finding_notes = []
        if is_test_query and matching_mats:
            test_titles = [m['title'] for m in matching_mats if any(k in m['title'].lower() for k in ['test', 'mock', 'exam'])]
            if test_titles:
                finding_notes.append(f"Identified test materials: {', '.join(test_titles[:3])}")
        
        test_anns = [a['title'] for a in course_anns if any(k in a['title'].lower() for k in ['test', 'venue', 'quiz', 'exam'])]
        if test_anns:
            finding_notes.append(f"Relevant notices: {', '.join(test_anns[:2])}")

        finding_str = f"Found {len(docs_summary)} matching documents for {target_code}. " + (" ".join(finding_notes) if finding_notes else f"Extracted {len(schedules)} semester milestones.")

        return {
            "subagent": "Document Specialist Agent",
            "status": "completed",
            "course_code": target_code,
            "task_type": task_type,
            "documents_found": docs_summary,
            "schedules_extracted": schedules[:8],
            "relevant_notices": [{"title": a["title"], "date": a.get("posted_at", "")} for a in course_anns[:3]],
            "finding": finding_str
        }

    elif name == "task_planner_agent":
        hours = float(args.get("available_hours", 6.0))
        schedule = generate_day_schedule(term=term, available_hours=hours)
        tasks = db.get_tasks(term=term)
        return {
            "subagent": "Planner Engine",
            "status": "completed",
            "available_hours": hours,
            "scheduled_blocks": schedule[:6],
            "pending_tasks_count": len(tasks),
            "top_tasks": [t["title"] for t in tasks[:5]],
            "finding": f"Optimized {len(schedule)} focus blocks ({hours}h available) across {len(tasks)} prioritized tasks."
        }

    elif name == "task_ntulearn_specialist":
        course_code = args.get("course_code")
        announcements = db.get_announcements(term=term, limit=10)
        if course_code:
            announcements = [a for a in announcements if course_code.upper() in a.get("course_code", "").upper()]
        return {
            "subagent": "NTULearn Specialist",
            "status": "completed",
            "course_code": course_code,
            "announcements": [
                {"title": a["title"], "course": a.get("course_code"), "posted": a.get("posted_at")}
                for a in announcements[:6]
            ],
            "finding": f"Checked notices for {course_code or 'active semester'}: found {len(announcements)} updates."
        }

    return {"error": f"Unknown tool {name}"}

def execute_chat_query(user_query: str, term: str = "26S1", session_id: str = "default") -> Dict[str, Any]:
    courses = db.get_all_courses(term=term)
    courses_str = ", ".join([f"{c['course_code']} ({c.get('title') or c['course_code']})" for c in courses])

    # ── Multi-turn history retrieval ──────────────────────────────────────────
    history = db.get_chat_history(session_id=session_id, limit=8)
    messages: List[Dict[str, Any]] = []

    for h in history:
        r = h.get("role", "user")
        txt = h.get("content", "").strip()
        # Strip internal HTML tags and callout cards so model sees clean semantic text
        txt = re.sub(r'<div class="subagent-[^"]*">.*?</div>', '', txt, flags=re.DOTALL)
        txt = re.sub(r'<[^>]+>', '', txt).strip()
        if not txt:
            continue

        # Bedrock requires alternating roles (user, assistant, user, assistant...)
        if messages and messages[-1]["role"] == r:
            messages[-1]["content"][0]["text"] += f"\n\n{txt}"
        else:
            messages.append({"role": r, "content": [{"text": txt}]})

    # Ensure messages array starts with a 'user' turn
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    # Current context grounding
    current_context = f"""Current Date: {datetime.now().strftime('%d %B %Y')} (AY2026/27 Semester 1 - Week 4/5)
Active Term: {term}
Enrolled Modules: {courses_str}

Student Question: {user_query}"""

    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"][0]["text"] += f"\n\nFollow-up Question: {user_query}"
    else:
        messages.append({"role": "user", "content": [{"text": current_context}]})

    delegation_steps = []
    final_reply = ""

    # Bedrock Converse loop with Tool Calling
    if bedrock_client.is_ready():
        try:
            tool_config = {"tools": TOOL_DEFINITIONS}
            max_turns = 4
            turn = 0

            while turn < max_turns:
                turn += 1
                resp = bedrock_client._client.converse(
                    modelId=LEAD_MODEL_ID,
                    messages=messages,
                    system=[{"text": LEAD_SYSTEM_PROMPT}],
                    toolConfig=tool_config,
                    inferenceConfig={"maxTokens": 1400, "temperature": 0.2}
                )

                output_msg = resp.get("output", {}).get("message", {})
                messages.append(output_msg)

                stop_reason = resp.get("stopReason")
                content_blocks = output_msg.get("content", [])

                # Check if tool was called
                if stop_reason == "tool_use":
                    tool_result_contents = []

                    for block in content_blocks:
                        if "toolUse" in block:
                            t_use = block["toolUse"]
                            t_id = t_use["toolUseId"]
                            t_name = t_use["name"]
                            t_input = t_use.get("input", {})

                            # Execute subagent
                            subagent_out = execute_subagent_tool(t_name, t_input, term=term)
                            delegation_steps.append({
                                "agent": subagent_out.get("subagent", t_name),
                                "action": f"Delegated to {t_name}",
                                "input": t_input,
                                "result_summary": subagent_out.get("finding") or str(subagent_out)[:140]
                            })

                            tool_result_contents.append({
                                "toolResult": {
                                    "toolUseId": t_id,
                                    "content": [{"json": subagent_out}],
                                    "status": "success"
                                }
                            })

                    # Send tool result back to Lead Agent
                    messages.append({
                        "role": "user",
                        "content": tool_result_contents
                    })
                else:
                    # Model provided final text
                    for block in content_blocks:
                        if "text" in block:
                            final_reply += block["text"]
                    break

        except Exception as e:
            final_reply = f"Bedrock tool execution error: {e}"

    # If offline or fallback
    if not final_reply or "Bedrock tool execution error" in final_reply:
        # Dynamic fallback based on active courses
        primary_course = courses[0]["course_code"] if courses else "General"
        subagent_res = execute_subagent_tool("task_document_specialist", {"course_code": primary_course, "task_type": "general_search"}, term=term)
        delegation_steps.append({
            "agent": "Document Specialist Agent",
            "action": f"Inspected course materials for {primary_course}",
            "input": {"course_code": primary_course},
            "result_summary": f"Found {len(subagent_res.get('documents_found', []))} materials for {primary_course}"
        })
        matched_tut = subagent_res["documents_found"][0] if subagent_res.get("documents_found") else None
        doc_token = f"[OPEN_DOC:{matched_tut['id']}:{matched_tut['title']}]" if matched_tut else ""

        final_reply = f"""### 🎯 Immediate Priority: What You Need To Do

You are currently in **Week 4/5 of Semester 1 (AY2026/27)**.

#### 📄 Course Materials & Documents:
{doc_token}

- **Active Module:** **{primary_course}**
- **Action:** Review active lecture handouts and complete this week's tutorial questions.

Click the **Open Document** link above to inspect your materials."""

    # Build visible compact Sub-Agent Delegation badges
    delegation_callout = ""
    if delegation_steps:
        sub_badges = []
        for s in delegation_steps:
            agent_title = s.get("agent", "Specialist Agent")
            summary = s.get("result_summary", "")
            if len(summary) > 130:
                summary = summary[:127] + "..."
            sub_badges.append(
                f'<div class="subagent-compact-badge">'
                f'<span class="subagent-pulse"></span>'
                f'<span class="subagent-badge-title">⚡ {agent_title}:</span> '
                f'<span class="subagent-badge-desc">{summary}</span>'
                f'</div>'
            )
        delegation_callout = "".join(sub_badges) + "\n\n"

    composed_reply = f"{delegation_callout}{final_reply}"

    db.save_chat_message("user", user_query, agent_name="User", session_id=session_id)
    db.save_chat_message("assistant", composed_reply, agent_name="Lead Orchestrator", session_id=session_id)

    return {
        "reply": composed_reply,
        "agent": "Lead Orchestrator",
        "delegation_steps": delegation_steps,
        "model": LEAD_MODEL_ID,
        "term": term,
    }
