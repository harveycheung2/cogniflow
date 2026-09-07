import json
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from services.aws_bedrock import bedrock_client
from services.llm_provider import llm_provider
from core.config import LEAD_MODEL_ID, SPECIALIST_MODEL_ID
import core.database as db
from services.planner import generate_day_schedule
from services.document_agent import document_agent
from services.ntulearn import ntulearn_service

LEAD_SYSTEM_PROMPT = """You are the Lead Orchestrator of CogniFlow OS.
You supervise multiple specialized sub-agents to organize the student's academic life for Semester 1 (26S1).

Available Sub-Agents to Delegate to:
1. `task_document_specialist`: Inspects course slides, searches tutorial documents, extracts semester schedules, and retrieves specific PDF materials or test/exam information. Pass both `course_code` (e.g. 'MH2500', 'SC2001', 'SC2207', 'MH2802', 'CC0006') and `detail` (e.g. 'schedule', 'test', 'mock', 'Tutorial 1', 'syllabus') so it can deeply search document contents, questions, solutions, and milestones.
2. `task_planner_agent`: Generates optimized time-blocked study schedules and calculates urgency priority scores.
3. `task_ntulearn_specialist`: Checks and reads the full text bodies of announcements, deadlines, test venues, and course notices.

Guidelines:
- **Conversation Context Preservation & Course Accuracy**: Always remember context across conversation turns. Ensure tool calls always use the specific course code (e.g., MH2500) asked about in the prompt. Never substitute with an unrelated course (like CC0006) when the student asked about a different module.
- **Sub-Agent Delegation**: Whenever the student asks "what do I need to do", "which tutorial do I need", "find tutorial", or asks about course content/schedules/tests, ALWAYS task `task_document_specialist` and/or `task_ntulearn_specialist` to inspect the actual course documents and notices first.
- **Announcements & Notices**: When asked about test venues, deadlines, or schedules, ALWAYS delegate to `task_ntulearn_specialist` and check the announcement notices and bodies. Extract and quote the actual dates, venues, and instructions!
- **Document Citing**: Clearly distinguish between Question sheets, Solution hints, Mock test papers, and Syllabus documents. ALWAYS include the clickable action token:
  `[OPEN_DOC:<material_id>:<Document Title>]`
  This allows the student to click directly in the chat to open and view the PDF in the application!
- **Response Structure**:
  - 🎯 **Summary & Immediate Priorities**
  - 📄 **Exact Tutorial / Test / Syllabus PDFs Required & Action Links**
  - 📅 **Semester Schedule & Milestones**
  - 💡 **Recommended Next Step**
- Professional, encouraging, and clear executive tone.
"""

TOOL_DEFINITIONS = [
    {
        "toolSpec": {
            "name": "task_document_specialist",
            "description": "Task the Document Specialist Sub-Agent to deeply inspect, analyze, or search course documents, lecture slides, syllabus timetables, and tutorial question/solution sheets for a course. Searches inside document text, extracted questions, and rosters.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "course_code": {
                            "type": "string",
                            "description": "Course code or keyword, e.g. 'MH2500', 'SC2001', 'SC2207', 'MH2802', 'CC0006', 'ML0004'"
                        },
                        "task_type": {
                            "type": "string",
                            "enum": ["find_tutorial_pdf", "extract_schedule", "inspect_lecture_slides", "get_solutions", "general_search"],
                            "description": "Specific intent for the document agent"
                        },
                        "detail": {
                            "type": "string",
                            "description": "Topic, keyword, or specific tutorial/test name to search within the text/questions, e.g. 'schedule', 'test', 'mock', 'Tutorial 1', 'probability'"
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
                            "description": "Search topic e.g. 'quiz', 'test', 'venue', 'exam', 'slot', 'schedule'"
                        }
                    }
                }
            }
        }
    }
]

# Convert Bedrock toolSpec format to standard OpenAI/Groq/Gemini tool format
OPENAI_TOOL_DEFINITIONS = []
for t in TOOL_DEFINITIONS:
    if "toolSpec" in t:
        spec = t["toolSpec"]
        OPENAI_TOOL_DEFINITIONS.append({
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec.get("inputSchema", {}).get("json", {})
            }
        })


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
        raw_code = args.get("course_code", "")
        task_type = args.get("task_type", "general_search")
        detail = args.get("detail", "")

        # Clean course code (e.g. MH2500 from 26S1-MH2500-LEC)
        cm = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', raw_code, re.IGNORECASE)
        course_code = cm.group(1).upper() if cm else raw_code

        matching_mats = []
        if detail and hasattr(db, "search_documents_by_content"):
            matching_mats = db.search_documents_by_content(query=detail, course_code=course_code)

        combined_text = f"{task_type} {detail}".lower()
        is_test_query = any(w in combined_text for w in ["test", "exam", "quiz", "mock", "ca1", "ca2", "assessment"])
        is_syllabus_query = any(w in combined_text for w in ["syllabus", "schedule", "timetable", "outline", "hand00", "overview"])

        all_mats = db.get_all_materials(course_code=course_code)

        if (is_test_query or is_syllabus_query) and not matching_mats:
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

        # Fall back to matching tutorials if needed
        if not matching_mats:
            matching_mats = db.find_matching_tutorials(course_code=course_code, query=detail)

        # Fall back to general course materials
        if not matching_mats:
            matching_mats = all_mats

        # Deep parse top documents (capped to 2 to conserve AWS tokens)
        for m in matching_mats[:2]:
            if not m.get("raw_text_excerpt"):
                document_agent.analyze_document(m["id"])

        schedules = db.get_course_schedules(course_code=course_code)
        course_anns = db.get_announcements(term=term, course_code=course_code, limit=10)

        # Categorize documents
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

        finding_notes = []
        if matching_mats:
            test_titles = [m['title'] for m in matching_mats if any(k in m['title'].lower() for k in ['test', 'mock', 'exam'])]
            if test_titles:
                finding_notes.append(f"Test materials: {', '.join(test_titles[:3])}")
            syllabus_titles = [m['title'] for m in matching_mats if any(k in m['title'].lower() for k in ['hand00', 'syllabus', 'outline', 'schedule'])]
            if syllabus_titles:
                finding_notes.append(f"Syllabus file: {syllabus_titles[0]}")
        
        test_anns = [a['title'] for a in course_anns if any(k in a['title'].lower() for k in ['test', 'venue', 'quiz', 'exam', 'schedule'])]
        if test_anns:
            finding_notes.append(f"Notices: {', '.join(test_anns[:2])}")

        finding_str = f"Found {len(matching_mats)} documents for {course_code}. " + (" ".join(finding_notes) if finding_notes else f"Extracted {len(schedules)} semester milestones.")

        return {
            "subagent": "Document Specialist Agent",
            "status": "completed",
            "course_code": course_code,
            "task_type": task_type,
            "search_query": detail,
            "question_sheets_found": question_sheets,
            "solution_sheets_found": solution_sheets,
            "other_documents": other_docs[:10],
            "documents_found": matching_mats[:8],
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
            "top_tasks": [t["title"] for t in tasks[:5]]
        }

    elif name == "task_ntulearn_specialist":
        raw_code = args.get("course_code")
        query = args.get("query")
        announcements = db.get_announcements(term=term, course_code=raw_code, limit=15)
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
    courses_str = ", ".join([f"{c['course_code']} ({c.get('title') or c['course_code']})" for c in courses])

    # ── Multi-turn history retrieval ──────────────────────────────────────────
    history = db.get_chat_history(session_id=session_id, limit=8)
    messages: List[Dict[str, Any]] = []

    for h in history:
        r = h.get("role", "user")
        txt = h.get("content", "").strip()
        txt = re.sub(r'<div class="subagent-[^"]*">.*?</div>', '', txt, flags=re.DOTALL)
        txt = re.sub(r'<[^>]+>', '', txt).strip()
        if not txt:
            continue

        if messages and messages[-1]["role"] == r:
            messages[-1]["content"][0]["text"] += f"\n\n{txt}"
        else:
            messages.append({"role": r, "content": [{"text": txt}]})

    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    # Resolve course target from user prompt and multi-turn context
    matched_course = extract_course_from_query(user_query, history, courses)
    target_course_code = matched_course["course_code"] if matched_course else (courses[0]["course_code"] if courses else "General")
    target_course_title = matched_course.get("title") or target_course_code if matched_course else "General Studies"
    clean_target = re.search(r'\b([A-Z]{2,4}\d{4}[A-Z]?)\b', target_course_code)
    target_code = clean_target.group(1) if clean_target else target_course_code

    # Active Timetable Context
    active_tt = getattr(db, "get_active_timetable", lambda term: None)(term=term) if hasattr(db, "get_active_timetable") else None
    tt_summary = ""
    if active_tt and active_tt.get("parsed_data"):
        pdata = active_tt["parsed_data"]
        sname = pdata.get("student_name", "Student")
        slots_list = pdata.get("weekly_slots", [])
        slots_text = "; ".join([f"{s['day']} {s['start_time']}-{s['end_time']} {s['course_code']} {s['event_type']} @ {s.get('venue','TBA')}" for s in slots_list])
        exams_list = [f"{c['course_code']}: {c['exam_schedule']}" for c in pdata.get("courses", []) if "not applicable" not in c.get("exam_schedule", "").lower()]
        exams_text = "; ".join(exams_list)
        tt_summary = f"\nStudent Profile: {sname}\nOfficial Timetable Weekly Slots: {slots_text}\nOfficial Final Exam Dates: {exams_text}\n"

    # Current context grounding
    current_context = f"""Current Date: {datetime.now().strftime('%d %B %Y')} (AY2026/27 Semester 1 - Week 4/5)
Active Term: {term}
Identified Course Target: {target_course_code} ({target_code} - {target_course_title})
Enrolled Modules: {courses_str}
{tt_summary}
Student Question: {user_query}"""

    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"][0]["text"] += f"\n\nTarget Module: {target_code}\nFollow-up Question: {user_query}"
    else:
        messages.append({"role": "user", "content": [{"text": current_context}]})

    delegation_steps = []
    final_reply = ""

    # 1. PRIORITY 1: AWS Bedrock Claude 3.5 Sonnet
    provider_name_used = "Offline Hybrid Engine"
    if bedrock_client.is_ready():
        try:
            tool_config = {"tools": TOOL_DEFINITIONS}
            turn = 0
            while turn < 3:
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

                if stop_reason == "tool_use":
                    tool_result_contents = []
                    for block in content_blocks:
                        if "toolUse" in block:
                            t_use = block["toolUse"]
                            subagent_out = execute_subagent_tool(t_use["name"], t_use.get("input", {}), term=term)
                            delegation_steps.append({
                                "agent": subagent_out.get("subagent", t_use["name"]),
                                "action": f"Delegated to {t_use['name']}",
                                "input": t_use.get("input", {}),
                                "result_summary": subagent_out.get("finding") or str(subagent_out)[:140]
                            })
                            tool_result_contents.append({
                                "toolResult": {
                                    "toolUseId": t_use["toolUseId"],
                                    "content": [{"json": subagent_out}],
                                    "status": "success"
                                }
                            })
                    messages.append({"role": "user", "content": tool_result_contents})
                else:
                    for block in content_blocks:
                        if "text" in block:
                            final_reply += block["text"]
                    provider_name_used = "AWS Bedrock (Claude 3.5)"
                    
                    usage = resp.get("usage", {})
                    pt = usage.get("inputTokens", 0)
                    ct = usage.get("outputTokens", 0)
                    tt = usage.get("totalTokens", pt + ct)
                    lat = resp.get("metrics", {}).get("latencyMs", 1000)
                    try:
                        db.record_token_usage("AWS Bedrock", LEAD_MODEL_ID, pt, ct, tt, lat, "success", "chat")
                    except Exception:
                        pass
                    break
        except Exception as e:
            print(f"[Agents] Bedrock priority notice ({e}). Auto-falling back to Groq...")
            final_reply = ""

    # 2. PRIORITY 2 & 3: Groq Cloud with Google Gemini Fallback
    if not final_reply and llm_provider.is_ready():
        try:
            openai_msgs = []
            for h in history[-4:]:
                r = h.get("role", "user")
                t = re.sub(r'<div class="subagent-[^"]*">.*?</div>', '', h.get("content", ""), flags=re.DOTALL)
                t = re.sub(r'<[^>]+>', '', t).strip()
                if t:
                    openai_msgs.append({"role": r, "content": t})
            openai_msgs.append({"role": "user", "content": current_context})

            max_turns = 3
            turn = 0
            while turn < max_turns:
                turn += 1
                tools_to_use = None if (delegation_steps and turn >= 2) else OPENAI_TOOL_DEFINITIONS
                res_msg, provider = llm_provider.chat_completion(
                    messages=openai_msgs,
                    tools=tools_to_use,
                    system_prompt=LEAD_SYSTEM_PROMPT,
                    temperature=0.2,
                    max_tokens=1000
                )
                provider_name_used = provider

                if not res_msg:
                    break

                tool_calls = getattr(res_msg, "tool_calls", None)
                if tool_calls and len(tool_calls) > 0:
                    openai_msgs.append({
                        "role": "assistant",
                        "content": res_msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments
                                }
                            } for tc in tool_calls
                        ]
                    })

                    for tc in tool_calls:
                        t_name = tc.function.name
                        try:
                            t_input = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
                        except Exception:
                            t_input = {}

                        subagent_out = execute_subagent_tool(t_name, t_input, term=term)
                        delegation_steps.append({
                            "agent": subagent_out.get("subagent", t_name),
                            "action": f"Delegated to {t_name}",
                            "input": t_input,
                            "result_summary": subagent_out.get("finding") or str(subagent_out)[:140]
                        })

                        openai_msgs.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(subagent_out)
                        })
                else:
                    if res_msg.content:
                        final_reply = res_msg.content
                    break

        except Exception as e:
            print(f"[Agents] Error during Groq/Gemini execution ({e}), falling back to local specialist...")
            final_reply = ""

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
        "model": provider_name_used,
        "term": term,
    }
