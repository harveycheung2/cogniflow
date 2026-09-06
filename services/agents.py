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

def extract_course_from_query(query: str, history: List[Dict[str, Any]], courses: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Detects target course from query, subject keywords, or multi-turn history."""
    if not courses:
        return None

    # 1. Direct course code regex match in user query: e.g. MH2500, CC0006, SC2001
    code_match = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', query, re.IGNORECASE)
    if code_match:
        target = code_match.group(1).upper()
        for c in courses:
            if target in c.get("course_code", "").upper():
                return c

    # 2. Match subject keywords in user query
    q_lower = query.lower()
    keyword_map = {
        "probability": "MH2500",
        "prob": "MH2500",
        "linear algebra": "MH2802",
        "linalg": "MH2802",
        "sustainability": "CC0006",
        "algorithm": "SC2001",
        "algo": "SC2001",
        "database": "SC2207",
        "career": "ML0004",
    }
    for kw, code in keyword_map.items():
        if kw in q_lower:
            for c in courses:
                if code in c.get("course_code", "").upper():
                    return c

    # 3. Check recent conversation history (multi-turn memory) for course context
    for h in reversed(history[-6:]):
        content = h.get("content", "")
        hist_match = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', content, re.IGNORECASE)
        if hist_match:
            target = hist_match.group(1).upper()
            for c in courses:
                if target in c.get("course_code", "").upper():
                    return c
        for kw, code in keyword_map.items():
            if kw in content.lower():
                for c in courses:
                    if code in c.get("course_code", "").upper():
                        return c

    return None

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
        combined_text = f"{task_type} {detail}".lower()
        is_test_query = any(w in combined_text for w in ["test", "exam", "quiz", "mock", "ca1", "ca2", "assessment"])
        is_syllabus_query = any(w in combined_text for w in ["syllabus", "schedule", "timetable", "outline", "hand00", "overview"])

        matching_mats = []
        if is_test_query or is_syllabus_query:
            # Prioritize syllabus/schedule documents and test/mock documents together
            syllabus_mats = [
                m for m in all_mats
                if any(w in m.get("title", "").lower() for w in ["hand00", "syllabus", "schedule", "outline", "overview"])
            ]
            test_mats = [
                m for m in all_mats
                if any(w in m.get("title", "").lower() for w in ["test", "mock", "exam", "quiz", "ca1", "ca2"])
            ]
            if is_test_query and is_syllabus_query:
                matching_mats = syllabus_mats + test_mats
            elif is_test_query:
                matching_mats = test_mats + syllabus_mats
            else:
                matching_mats = syllabus_mats + test_mats

            if not matching_mats:
                matching_mats = all_mats
        else:
            matching_mats = db.find_matching_tutorials(course_code=target_code)
            if not matching_mats:
                matching_mats = all_mats

        # Ensure key materials are analyzed if not yet analyzed (capped to 2 to conserve AWS tokens)
        for m in matching_mats[:2]:
            if not db.get_parsed_documents(course_code=m.get("course_code")):
                document_agent.analyze_document(m["id"])

        schedules = db.get_course_schedules(course_code=target_code)

        # Retrieve course announcements specifically filtered by course_code
        course_anns = db.get_announcements(term=term, course_code=target_code, limit=10)

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
        if matching_mats:
            test_titles = [m['title'] for m in matching_mats if any(k in m['title'].lower() for k in ['test', 'mock', 'exam'])]
            if test_titles:
                finding_notes.append(f"Identified test materials: {', '.join(test_titles[:3])}")
            syllabus_titles = [m['title'] for m in matching_mats if any(k in m['title'].lower() for k in ['hand00', 'syllabus', 'outline', 'schedule'])]
            if syllabus_titles:
                finding_notes.append(f"Syllabus file: {syllabus_titles[0]}")
        
        test_anns = [a['title'] for a in course_anns if any(k in a['title'].lower() for k in ['test', 'venue', 'quiz', 'exam', 'schedule'])]
        if test_anns:
            finding_notes.append(f"Notices: {', '.join(test_anns[:2])}")

        finding_str = f"Found {len(docs_summary)} matching documents for {target_code}. " + (" ".join(finding_notes) if finding_notes else f"Extracted {len(schedules)} semester milestones.")

        return {
            "subagent": "Document Specialist Agent",
            "status": "completed",
            "course_code": target_code,
            "task_type": task_type,
            "documents_found": docs_summary,
            "schedules_extracted": schedules[:8],
            "relevant_notices": [{"title": a["title"], "date": a.get("posted_at", "")} for a in course_anns[:4]],
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
        announcements = db.get_announcements(term=term, course_code=course_code, limit=10)
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

    # Resolve course target from user prompt and multi-turn context
    matched_course = extract_course_from_query(user_query, history, courses)
    target_course_code = matched_course["course_code"] if matched_course else (courses[0]["course_code"] if courses else "General")
    target_course_title = matched_course.get("title") or target_course_code if matched_course else "General Studies"
    clean_target = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', target_course_code)
    target_code = clean_target.group(1) if clean_target else target_course_code

    # Current context grounding
    current_context = f"""Current Date: {datetime.now().strftime('%d %B %Y')} (AY2026/27 Semester 1 - Week 4/5)
Active Term: {term}
Identified Course Target: {target_course_code} ({target_code} - {target_course_title})
Enrolled Modules: {courses_str}

Student Question: {user_query}"""

    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"][0]["text"] += f"\n\nTarget Module: {target_code}\nFollow-up Question: {user_query}"
    else:
        messages.append({"role": "user", "content": [{"text": current_context}]})

    delegation_steps = []
    final_reply = ""

    # Bedrock Converse loop with Tool Calling
    if bedrock_client.is_ready():
        try:
            tool_config = {"tools": TOOL_DEFINITIONS}
            max_turns = 3
            turn = 0

            while turn < max_turns:
                turn += 1
                resp = bedrock_client._client.converse(
                    modelId=LEAD_MODEL_ID,
                    messages=messages,
                    system=[{"text": LEAD_SYSTEM_PROMPT}],
                    toolConfig=tool_config,
                    inferenceConfig={"maxTokens": 1000, "temperature": 0.2}
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

    # If offline, expired credentials, or fallback required
    if not final_reply or "Bedrock tool execution error" in final_reply:
        q_lower = user_query.lower()
        is_test_query = any(w in q_lower for w in ["test", "exam", "quiz", "mock", "ca1", "ca2", "assessment"])
        is_sched_query = any(w in q_lower for w in ["schedule", "timetable", "dates", "calendar", "timeline", "week", "outline", "syllabus"])

        # Execute Document Specialist for the accurately resolved module
        subagent_res = execute_subagent_tool(
            "task_document_specialist",
            {
                "course_code": target_code,
                "task_type": "schedule_and_tests" if (is_sched_query or is_test_query) else "general_search",
                "detail": user_query
            },
            term=term
        )
        delegation_steps.append({
            "agent": "Document Specialist Agent",
            "action": f"Inspected course documents & timetable for {target_code}",
            "input": {"course_code": target_code, "query": user_query},
            "result_summary": subagent_res.get("finding", f"Found materials for {target_code}")
        })

        # Execute NTULearn Specialist for notices
        ntulearn_res = execute_subagent_tool("task_ntulearn_specialist", {"course_code": target_code}, term=term)
        delegation_steps.append({
            "agent": "NTULearn Specialist",
            "action": f"Retrieved announcements & CA notices for {target_code}",
            "input": {"course_code": target_code},
            "result_summary": ntulearn_res.get("finding", "")
        })

        # Gather target course tasks & materials
        course_tasks = [t for t in db.get_tasks(term=term) if target_code in str(t.get("course_code", "")) or target_code in str(t.get("title", ""))]
        all_course_mats = db.get_all_materials(course_code=target_code)
        course_anns = db.get_announcements(term=term, course_code=target_code, limit=5)

        # Categorize documents
        syllabus_docs = [m for m in all_course_mats if any(k in m['title'].lower() for k in ['hand00', 'syllabus', 'schedule', 'outline', 'overview'])]
        test_docs = [m for m in all_course_mats if any(k in m['title'].lower() for k in ['test', 'mock', 'exam', 'quiz'])]
        tut_docs = [m for m in all_course_mats if any(k in m['title'].lower() for k in ['tutorial', 'tut'])]

        # Primary highlight document
        primary_doc = syllabus_docs[0] if syllabus_docs else (test_docs[0] if test_docs else (all_course_mats[0] if all_course_mats else None))
        doc_token = f"[OPEN_DOC:{primary_doc['id']}:{primary_doc['title']}]" if primary_doc else ""

        # Test Prep tokens
        test_tokens = [f"[OPEN_DOC:{m['id']}:{m['title']}]" for m in test_docs[:3]]

        # Construct authoritative course-specific response
        reply_lines = [
            f"### 📅 **{target_code}: Semester Schedule & Assessment Intelligence**",
            f"**Module:** `{target_course_code}` ({target_course_title})",
            f"**Current Academic Timeline:** Currently in **Week 4/5 of Semester 1 (AY2026/27)**.\n"
        ]

        if is_test_query or is_sched_query:
            reply_lines.append("#### 📝 Upcoming Tests & Assessment Milestones:")
            if course_tasks:
                for t in course_tasks:
                    reply_lines.append(f"- **Task Alert:** **{t['title']}** (Urgency Score: `{t.get('priority_score', '9.0')}`) — *Due: {t.get('due_date', 'Upcoming')}*")
            else:
                reply_lines.append(f"- **Upcoming Assessment:** Continuous Assessment / Test 1 scheduled during Semester 1.")

            # Test announcements
            test_notices = [a for a in course_anns if any(k in a['title'].lower() for k in ['test', 'venue', 'exam', 'quiz'])]
            if test_notices:
                for a in test_notices:
                    reply_lines.append(f"- **Official Notice:** **{a['title']}** (Posted: {a.get('posted_at') or 'Recent'})")

            if test_tokens:
                reply_lines.append("\n**Test Preparation Documents & Mock Papers:**")
                for tk in test_tokens:
                    reply_lines.append(f"- {tk}")

            reply_lines.append("\n#### 📑 Course Outline & Syllabus Document:")
            if doc_token:
                reply_lines.append(f"{doc_token}\n*Click above to open the official syllabus schedule, topic distribution, and grading scheme.*")
            else:
                reply_lines.append(f"- *Review lecture handouts and tutorial sheets in the course portal.*")

            reply_lines.append("\n#### 📚 Weekly Lectures & Active Tutorials:")
            reply_lines.append(f"- **Lecture Series:** Handouts released up to Week 4/5 (Hand01 to Hand04).")
            if tut_docs:
                tut_tokens = [f"[OPEN_DOC:{t['id']}:{t['title']}]" for t in tut_docs[:3]]
                reply_lines.append(f"- **Active Problem Sets:** {' | '.join(tut_tokens)}")

        else:
            # Tutorial or general search response
            reply_lines.append("#### 📄 Key Course Documents & Materials:")
            if doc_token:
                reply_lines.append(f"{doc_token}")
            if tut_docs:
                for td in tut_docs[:3]:
                    reply_lines.append(f"- [OPEN_DOC:{td['id']}:{td['title']}]")

            reply_lines.append("\n#### 🎯 Immediate Action Required:")
            reply_lines.append(f"- Review active lecture materials and complete this week's assigned tutorial exercises.")

        if course_anns:
            reply_lines.append("\n#### 📢 Recent Course Announcements:")
            for a in course_anns[:3]:
                reply_lines.append(f"- **{a['title']}**")

        final_reply = "\n".join(reply_lines)

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
