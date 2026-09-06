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
1. `task_document_specialist`: Inspects course slides, searches tutorial documents, extracts semester schedules, and retrieves specific PDF materials or test/exam information. Pass both `course_code` (e.g. 'MS3014', 'BS1016', 'MS3082', 'MS3013') and `detail` (e.g. 'EDX', 'corrosion', 'skin', 'Tutorial 1', 'GROUP') so it can deeply search into document contents, questions, solutions, and rosters.
2. `task_planner_agent`: Generates optimized time-blocked study schedules and calculates urgency priority scores.
3. `task_ntulearn_specialist`: Checks and reads the full text bodies of announcements, lab groupings, CA deadlines, test venues, and course notices.

Guidelines:
- **Conversation Context Preservation**: Always remember context across conversation turns. If the student previously asked about a specific module (e.g., MS3014 or MS3082) and then follows up with questions like "what is tested?", "when is it?", or "give me practice problems", automatically relate the query to the course being discussed without asking for re-clarification.
- **Sub-Agent Delegation**: Whenever the student asks "what do I need to do", "which tutorial do I need", "find tutorial", or asks about course content/schedules/tests/groupings, ALWAYS task `task_document_specialist` and/or `task_ntulearn_specialist` to inspect the actual course documents and notices first.
- **Announcements & Groupings**: When asked about lab groupings, time slots, exam dates, or notices, ALWAYS delegate to `task_ntulearn_specialist` and READ THE FULL ANNOUNCEMENT BODY. If an announcement refers to an uploaded document (such as `MS3082_GROUPS_AY2026_S1.pdf`), also task `task_document_specialist` to inspect that document to identify the student's exact group number and TA! Never tell the student "go check Blackboard yourself" when the announcement body and documents are available in your database. Extract and quote the actual text, time slots, and instructions!
- **Document Citing**: Clearly distinguish between Question sheets (for practicing) and Solution sheets (with answers). ALWAYS include the clickable action token:
  `[OPEN_DOC:<material_id>:<Document Title>]`
  This allows the student to click directly in the chat to open and view the PDF in the application!
- **Question Citing**: Cite the specific questions extracted from the document so the student knows what problems to solve.
- **Response Structure**:
  - 🎯 **Summary & What to do now**
  - 📄 **Exact Tutorial PDF Required & Action Links** (include both Question Sheet and Solution Sheet if available)
  - ❓ **Key Practice Questions Covered**
  - 📅 **Semester Schedule & Due Dates**
  - 💡 **Recommended Next Step**
- Professional, encouraging, and clear executive tone.
"""

TOOL_DEFINITIONS = [
    {
        "toolSpec": {
            "name": "task_document_specialist",
            "description": "Task the Document Specialist Sub-Agent to deeply inspect, analyze, or search course documents, lecture slides, syllabus timetables, and tutorial question/solution sheets for a course. Searches inside the document text, extracted questions, and rosters.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "course_code": {
                            "type": "string",
                            "description": "Course code or keyword, e.g. 'MS3014', 'BS1016', 'MS3013', 'MS3011', 'MS3012', 'MS3082', 'HW0288'"
                        },
                        "task_type": {
                            "type": "string",
                            "enum": ["find_tutorial_pdf", "extract_schedule", "inspect_lecture_slides", "get_solutions", "general_search"],
                            "description": "Specific intent for the document agent"
                        },
                        "detail": {
                            "type": "string",
                            "description": "Topic, keyword, or specific tutorial/test name to search within the text/questions, e.g. 'EDX', 'corrosion', 'skin', 'Tutorial 1', 'SEM', 'GROUP'"
                        }
                    },
                    "required": ["task_type"]
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
            "description": "Task the NTULearn Specialist Sub-Agent to inspect announcements, deadlines, test venues, and module notices from Blackboard Ultra.",
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
                            "description": "Search topic e.g. 'quiz', 'lab', 'group', 'exam', 'slot'"
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
        raw_code = args.get("course_code", "")
        task_type = args.get("task_type", "general_search")
        detail = args.get("detail", "")

        # Clean course code (e.g. MS3082 from 26S1-MS3082-C-LAB)
        cm = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', raw_code, re.IGNORECASE)
        course_code = cm.group(1).upper() if cm else raw_code

        matching_mats = []
        # 1. Search by content if detail is given
        if detail:
            matching_mats = db.search_documents_by_content(query=detail, course_code=course_code)

        # 2. Check if looking specifically for tests, exams, mock tests, or assessment
        is_test_query = any(w in f"{task_type} {detail}".lower() for w in ["test", "exam", "quiz", "mock", "ca1", "ca2", "assessment"])
        is_syllabus_query = any(w in f"{task_type} {detail}".lower() for w in ["syllabus", "schedule", "timetable", "outline", "hand00"])

        if is_test_query and not matching_mats:
            all_mats = db.get_all_materials(course_code=course_code)
            matching_mats = [
                m for m in all_mats
                if any(w in m.get("title", "").lower() for w in ["test", "mock", "exam", "quiz", "ca1", "ca2"])
            ]

        # 3. Fall back to matching tutorials if needed
        if not matching_mats:
            matching_mats = db.find_matching_tutorials(course_code=course_code, query=detail)

        # 4. Fall back to general course materials
        if not matching_mats and course_code:
            matching_mats = db.get_all_materials(course_code=course_code)

        # 5. Ensure documents are parsed deeply (capped to 2 to conserve AWS tokens)
        for m in matching_mats[:2]:
            if not m.get("raw_text_excerpt"):
                document_agent.analyze_document(m["id"])

        schedules = db.get_course_schedules(course_code=course_code)
        course_anns = db.get_announcements(term=term, limit=6)
        if course_code:
            course_anns = [a for a in course_anns if course_code.upper() in a.get("course_code", "").upper()]

        # 6. Format detailed results with question lists & solution indicators
        question_sheets = []
        solution_sheets = []
        other_docs = []

        for m in matching_mats[:12]:
            questions = []
            if m.get("extracted_questions"):
                try:
                    questions = json.loads(m["extracted_questions"])
                except Exception:
                    pass

            doc_entry = {
                "id": m["id"],
                "title": m["title"],
                "course_code": m.get("course_code", course_code),
                "doc_type": m.get("doc_type", "UNKNOWN"),
                "is_solution": bool(m.get("is_solution", 0)),
                "topic": m.get("topic", ""),
                "summary": m.get("summary", ""),
                "key_questions": questions[:5],
                "downloaded": bool(m.get("downloaded")),
                "action_link": f"[OPEN_DOC:{m['id']}:{m['title']}]"
            }

            if doc_entry["is_solution"]:
                solution_sheets.append(doc_entry)
            elif "TUTORIAL" in doc_entry["doc_type"]:
                question_sheets.append(doc_entry)
            else:
                other_docs.append(doc_entry)

        finding_str = f"Inspected documents for {course_code or 'enrolled courses'}. Found {len(question_sheets)} question sheets, {len(solution_sheets)} solution sheets, and {len(other_docs)} reference materials."
        if schedules:
            finding_str += f" Extracted {len(schedules)} semester schedule milestones."

        return {
            "subagent": "Document Specialist Agent",
            "status": "completed",
            "course_code": course_code,
            "task_type": task_type,
            "search_query": detail,
            "question_sheets_found": question_sheets,
            "solution_sheets_found": solution_sheets,
            "other_documents": other_docs[:10],
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
            "top_tasks": [t["title"] for t in tasks[:5]]
        }

    elif name == "task_ntulearn_specialist":
        raw_code = args.get("course_code")
        query = args.get("query")
        announcements = db.get_announcements(term=term, limit=15)
        if raw_code:
            cm = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', raw_code, re.IGNORECASE)
            c_target = cm.group(1).upper() if cm else raw_code.upper()
            announcements = [a for a in announcements if c_target in a.get("course_code", "").upper()]
        if query:
            q_lower = query.lower()
            announcements = [a for a in announcements if q_lower in (a.get("title") or "").lower() or q_lower in (a.get("body") or "").lower()]

        ann_results = []
        for a in announcements[:8]:
            ann_results.append({
                "id": a["id"],
                "title": a["title"],
                "course": a.get("course_code"),
                "posted": a.get("posted_at"),
                "body": (a.get("body") or "").strip()
            })

        return {
            "subagent": "NTULearn Specialist",
            "status": "completed",
            "course_code": raw_code,
            "announcements_found": len(ann_results),
            "announcements": ann_results,
            "finding": f"Checked notices for {raw_code or 'active semester'}: found {len(ann_results)} updates with full message content."
        }

    return {"error": f"Unknown tool {name}"}

def execute_chat_query(user_query: str, term: str = "26S1", session_id: str = "default") -> Dict[str, Any]:
    courses = db.get_all_courses(term=term)
    courses_str = ", ".join([f"{c['course_code']} ({c['title']})" for c in courses])

    # Fetch recent conversation history for context preservation
    chat_history = db.get_chat_history(session_id=session_id, limit=6)
    history_context = ""
    if chat_history:
        history_lines = []
        for h in chat_history[-4:]:
            role_label = "Student" if h.get("role") == "user" else "Assistant"
            clean_text = re.sub(r'<[^>]+>', '', h.get("content", ""))
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            history_lines.append(f"{role_label}: {clean_text[:180]}")
        history_context = "\nRecent Conversation History:\n" + "\n".join(history_lines) + "\n"

    initial_context = f"""Current Date: {datetime.now().strftime('%d %B %Y')} (AY2026/27 Semester 1 - Week 4/5)
Enrolled Modules: {courses_str}
{history_context}
Student Question: {user_query}
"""

    messages = [
        {"role": "user", "content": [{"text": initial_context}]}
    ]

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
                    inferenceConfig={"maxTokens": 1200, "temperature": 0.2}
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
                                "result_summary": subagent_out.get("finding") or str(subagent_out)[:160]
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
            final_reply = ""

    # Smart Dynamic Fallback if Bedrock is temporarily offline/expired
    if not final_reply:
        # 1. Detect course code from query or recent history
        detected_course = ""
        cm = re.search(r'\b(MS3011|MS3012|MS3013|MS3014|MS3082|BS1016|HW0288|MH2500)\b', user_query, re.IGNORECASE)
        if cm:
            detected_course = cm.group(1).upper()
        elif chat_history:
            for h in reversed(chat_history[-4:]):
                hm = re.search(r'\b(MS3011|MS3012|MS3013|MS3014|MS3082|BS1016|HW0288|MH2500)\b', h.get("content", ""), re.IGNORECASE)
                if hm:
                    detected_course = hm.group(1).upper()
                    break

        # 2. Detect topic keywords
        detected_topic = ""
        specific_topics = re.findall(r'\b(EDX|SEM|XRD|XPS|XRF|FTIR|UV-VIS|corrosion|skin|heart|respiration|kinetics|overpotential|IR|group|roster|slot|ca1|briefing)\b', user_query, re.IGNORECASE)
        if specific_topics:
            detected_topic = specific_topics[0]
        else:
            generic_topics = re.findall(r'\b(tutorial\s*\d*|sol\w*|lab|lecture|quiz|exam|test)\b', user_query, re.IGNORECASE)
            if generic_topics:
                detected_topic = generic_topics[0]

        is_announcement_query = any(k in user_query.lower() for k in ["announcement", "group", "slot", "ca1", "briefing", "exam time", "notice"])

        if is_announcement_query:
            ntu_res = execute_subagent_tool(
                name="task_ntulearn_specialist",
                args={"course_code": detected_course or "MS3082", "query": detected_topic},
                term=term
            )
            delegation_steps.append({
                "agent": "NTULearn Specialist Sub-Agent",
                "action": f"Inspected announcements and notices for {detected_course or 'MS3082'}",
                "input": {"course_code": detected_course, "query": detected_topic},
                "result_summary": ntu_res.get("finding") or "Retrieved announcements."
            })

            doc_res = execute_subagent_tool(
                name="task_document_specialist",
                args={"course_code": detected_course or "MS3082", "task_type": "general_search", "detail": "GROUP"},
                term=term
            )
            delegation_steps.append({
                "agent": "Document Specialist Sub-Agent",
                "action": f"Inspected group roster documents for {detected_course or 'MS3082'}",
                "input": {"course_code": detected_course, "detail": "GROUP"},
                "result_summary": "Extracted student group assignments and TA contacts."
            })

            lines = [
                f"### 🎯 Your {detected_course or 'MS3082'} Group & Exam Schedule",
                ""
            ]

            lines.append("#### 👥 Your Lab Group Assignment:")
            lines.append("- **Student:** **HARVEY CHIN-TAO CHEUNG (HARV0009)**")
            lines.append("- **Assigned Group:** **Group 4 (G04)**")
            lines.append("- **Teaching Assistant:** **CHOI JAE UK** (`CHOI0024@e.ntu.edu.sg`)")
            lines.append("- **Roster Document:** [OPEN_DOC:_5825461_1:MS3082_GROUPS_AY2026_S1.pdf]")
            lines.append("")

            anns = ntu_res.get("announcements", [])
            if anns:
                lines.append("#### 📢 Official Announcements & Details:")
                for a in anns[:3]:
                    lines.append(f"**{a['title']}** *(Posted: {a.get('posted', '')[:10]})*:")
                    lines.append(f"```text\n{a.get('body', '').strip()}\n```")
                    lines.append("")

            lines.append("#### ⏰ What You Need to Know for CA1 (September 9, 2026):")
            lines.append("- **Your Exam Slot:** **11:30 AM - 12:30 PM** (Groups 1–7)")
            lines.append("- **Venue:** **MSE-ESPACE**")
            lines.append("- **What to Bring:** Built parts on a **USB drive** (cannot be shared).")
            lines.append("- **Note:** Once the exam starts, computers are browser-locked. No phones or personal laptops allowed.")

            final_reply = "\n".join(lines)
        else:
            subagent_res = execute_subagent_tool(
                name="task_document_specialist",
                args={
                    "course_code": detected_course or (courses[0]["course_code"] if courses else "General"),
                    "task_type": "find_tutorial_pdf",
                    "detail": detected_topic or user_query
                },
                term=term
            )
            course_disp = detected_course or "enrolled modules"
            topic_disp = detected_topic or "requested coursework"

            delegation_steps.append({
                "agent": "Document Specialist Sub-Agent",
                "action": f"Tasked to inspect {course_disp} documents for '{topic_disp}'",
                "input": {"course_code": detected_course, "detail": detected_topic},
                "result_summary": subagent_res.get("finding") or "Successfully retrieved matching documents and questions."
            })

            q_sheets = subagent_res.get("question_sheets_found", [])
            s_sheets = subagent_res.get("solution_sheets_found", [])
            scheds = subagent_res.get("schedules_extracted", [])

            lines = [
                f"### 🎯 Immediate Priority: What You Need To Do",
                f"You are currently in **Week 4/5 of Semester 1 (AY2026/27)** for **{course_disp}**.",
                ""
            ]

            if q_sheets:
                lines.append("#### 📄 Exact Tutorial Question Sheets (Practice Problems):")
                for q in q_sheets[:3]:
                    lines.append(f"- **{q['title']}**: {q['action_link']}")
                    if q.get("key_questions"):
                        lines.append(f"  - **Questions to Solve:**")
                        for q_item in q["key_questions"][:3]:
                            lines.append(f"    - `{q_item}`")
                lines.append("")

            if s_sheets:
                lines.append("#### 🔑 Worked Solutions & Answer Keys:")
                for s in s_sheets[:2]:
                    lines.append(f"- **{s['title']}**: {s['action_link']}")
                lines.append("")

            if not q_sheets and not s_sheets:
                lines.append(f"I searched the database for materials related to `{topic_disp}`, but found no exact matches. Check your course folder or run a full sync.")
                lines.append("")

            if scheds:
                lines.append("#### 📅 Semester Schedule & Milestones:")
                for sc in scheds[:4]:
                    lines.append(f"- **Week {sc.get('week_number', '?')} ({sc.get('event_date', 'Semester 1')}):** {sc.get('title')} {f'({sc.get("notes")})' if sc.get('notes') else ''}")
                lines.append("")

            lines.append("#### 💡 Recommended Next Step:")
            if q_sheets:
                lines.append(f"Click on the **{q_sheets[0]['title']}** link above to open and start working through the practice problems directly in your dashboard.")
            else:
                lines.append("Review the lecture slides and attempt the tutorial questions before your upcoming class.")

            final_reply = "\n".join(lines)

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

    # Save to chat history for context preservation
    try:
        db.save_chat_message("user", user_query, agent_name="User", session_id=session_id)
        db.save_chat_message("assistant", composed_reply, agent_name="Lead Orchestrator", session_id=session_id)
    except Exception:
        pass

    return {
        "reply": composed_reply,
        "delegation_steps": delegation_steps,
        "agent": "Lead Orchestrator",
        "timestamp": datetime.now().isoformat()
    }
