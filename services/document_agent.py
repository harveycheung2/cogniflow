import os
import re
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
import pypdf

from core.config import SPECIALIST_MODEL_ID, LEAD_MODEL_ID
from services.aws_bedrock import bedrock_client
from services.llm_provider import llm_provider
from services.ntulearn import ntulearn_service
import core.database as db

SCHEDULE_KEYWORDS = [
    "schedule", "timetable", "syllabus", "calendar", "assessment",
    "continuous assessment", "grading", "scheme", "outline", "tutorial",
    "ca1", "ca2", "final exam"
]

SOLUTION_KEYWORDS = [
    "solution", "solutions", "answer", "answers", "ans", "worked",
    "key", "marking scheme", "rubric"
]

class DocumentAgent:
    def __init__(self):
        self.specialist_model = SPECIALIST_MODEL_ID
        self.lead_model = LEAD_MODEL_ID

    def extract_text_and_questions(self, file_path: Path, max_pages: int = 25) -> Dict[str, Any]:
        """Deep text extractor that reads across pages, finds questions, and detects solutions."""
        if not file_path.exists():
            return {"text": "", "page_count": 0, "questions": [], "is_solution": False}

        try:
            reader = pypdf.PdfReader(str(file_path))
            total_pages = len(reader.pages)
            extracted_pages = []
            all_text_chunks = []

            for i in range(min(total_pages, max_pages)):
                try:
                    page_text = reader.pages[i].extract_text() or ""
                    clean_text = page_text.strip()
                    if clean_text:
                        extracted_pages.append(f"--- [Page {i+1}] ---\n{clean_text}")
                        all_text_chunks.append(clean_text)
                except Exception:
                    continue

            full_text = "\n\n".join(extracted_pages)
            combined_raw = " ".join(all_text_chunks)

            # 1. Detect if solution sheet vs question sheet
            is_solution = False
            lower_name = file_path.name.lower()
            if any(k in lower_name for k in ["solution", "sol", "answer", "ans."]):
                is_solution = True
            elif any(k in combined_raw[:1000].lower() for k in ["solution", "solutions", "model answer"]):
                is_solution = True

            # 2. Extract individual numbered questions
            # e.g. "1. Calculate...", "2. When investigating..."
            question_pattern = re.compile(
                r'(?:^|\n)\s*(\d{1,2}\.|Q\d+[:.]?)\s+([A-Z0-9][^\n]{15,200})',
                re.MULTILINE
            )
            found_questions = []
            for match in question_pattern.finditer(full_text):
                q_num = match.group(1).strip()
                q_snippet = re.sub(r'\s+', ' ', match.group(2)).strip()
                found_questions.append(f"{q_num} {q_snippet[:140]}...")

            return {
                "text": full_text,
                "raw_excerpt": re.sub(r'\s+', ' ', full_text)[:3000],
                "page_count": total_pages,
                "questions": found_questions[:8],
                "is_solution": is_solution,
            }
        except Exception as e:
            return {"text": "", "raw_excerpt": "", "page_count": 0, "questions": [], "is_solution": False, "error": str(e)}

    def parse_schedule_heuristic(self, text: str, course_code: str, doc_id: str, doc_title: str) -> List[Dict[str, Any]]:
        """Deterministic extractor for course schedules & assessment slides."""
        schedule_items = []

        line_pattern = re.compile(
            r'^\s*(\d{1,2})\s+([0-3]?\d\s+[A-Za-z]+(?:\s+\d{4})?)\s+(.*?)\s+(LT\d+.*|Online.*|Hive.*|LHN.*)?\s*(T\d+|Tutorial\s*\d+)?\s*$',
            re.IGNORECASE
        )

        for line in text.splitlines():
            line_str = line.strip()
            m = line_pattern.match(line_str)
            if m:
                try:
                    week_num = int(m.group(1))
                    date_str = m.group(2).strip()
                    content = m.group(3).strip()
                    venue = m.group(4).strip() if m.group(4) else ""
                    tutorial = m.group(5).strip() if m.group(5) else ""

                    if "lecture" in content.lower() or "module" in content.lower() or "part" in content.lower() or content:
                        schedule_items.append({
                            "week_number": week_num,
                            "event_type": "lecture",
                            "title": f"Lecture: {content}",
                            "event_date": date_str,
                            "notes": venue,
                            "source_doc_id": doc_id,
                            "source_doc_title": doc_title,
                        })

                    if tutorial:
                        schedule_items.append({
                            "week_number": week_num,
                            "event_type": "tutorial",
                            "title": f"Tutorial: {tutorial} ({content})",
                            "event_date": date_str,
                            "notes": f"Discussed in {venue}" if venue else "",
                            "source_doc_id": doc_id,
                            "source_doc_title": doc_title,
                        })
                except Exception:
                    continue

        ca_matches = re.findall(
            r'(\d+\s*(?:CA[s]?|Continuous Assessment|Quiz|Test|Exam|Project)[^\n\d]*?(\d{1,3}%))',
            text, re.IGNORECASE
        )
        for ca_text, weight in ca_matches:
            clean_ca = re.sub(r'\s+', ' ', ca_text).strip()
            schedule_items.append({
                "week_number": 0,
                "event_type": "exam" if "exam" in clean_ca.lower() else "ca_quiz",
                "title": f"Assessment: {clean_ca}",
                "event_date": "Semester AY2026/27",
                "notes": f"Weightage: {weight}",
                "source_doc_id": doc_id,
                "source_doc_title": doc_title,
            })

        return schedule_items

    def analyze_document(self, mat_id: str) -> Dict[str, Any]:
        """Deeply reads into the document text, classifies content, extracts questions & schedule."""
        mat = db.get_material_by_id(mat_id)
        if not mat:
            return {"success": False, "error": "Material not found"}

        local_path = mat.get("local_path")
        course_code = mat.get("course_code") or "General"
        title = mat.get("title") or "Untitled Document"

        # Ensure downloaded and valid
        if not local_path or not Path(local_path).exists():
            download_url = mat.get("download_url")
            if not download_url:
                return {"success": False, "error": "No download URL available"}
            path_obj = ntulearn_service.download_material_file(
                mat_id=mat_id,
                course_code=course_code,
                title=title,
                download_url=download_url,
                file_name=mat.get("file_name") or ""
            )
            if not path_obj or not path_obj.exists():
                return {"success": False, "error": "Failed to download document"}
            local_path = str(path_obj)

        doc_info = self.extract_text_and_questions(Path(local_path))
        raw_text = doc_info.get("text", "")
        raw_excerpt = doc_info.get("raw_excerpt", "")
        questions_found = doc_info.get("questions", [])
        is_solution = doc_info.get("is_solution", False)

        extracted_schedule = []
        doc_type = "TUTORIAL_SOLUTION" if is_solution else "TUTORIAL_QUESTION"
        if not ("tutorial" in title.lower() or "tut" in title.lower() or "edx" in title.lower() or "ir " in title.lower() or "xrd" in title.lower() or "xps" in title.lower() or "t1" in title.lower() or "t2" in title.lower()):
            if "lec" in title.lower() or "slide" in title.lower():
                doc_type = "LECTURE_SLIDES"
            elif "schedule" in title.lower() or "syllabus" in title.lower():
                doc_type = "SYLLABUS_SCHEDULE"
            else:
                doc_type = "REFERENCE"

        week_number = 0
        topic = ""
        summary = ""

        # Extract schedule via heuristic
        heuristic_schedules = self.parse_schedule_heuristic(raw_text, course_code, mat_id, title)
        if heuristic_schedules:
            extracted_schedule.extend(heuristic_schedules)

        # AI Specialist Reading into Document (Groq Primary with Gemini Fallback)
        ai_success = False
        if (llm_provider.is_ready() or bedrock_client.is_ready()) and len(raw_text) > 40:
            prompt = f"""You are the Academic Document Specialist. Read this course material from {course_code} ({title}):

=== EXTRACTED DOCUMENT TEXT ===
{raw_excerpt[:3500]}
=== END TEXT ===

Analyze the actual contents and return ONLY valid JSON:
{{
  "doc_type": "TUTORIAL_QUESTION" | "TUTORIAL_SOLUTION" | "LECTURE_SLIDES" | "SYLLABUS_SCHEDULE" | "ASSIGNMENT_BRIEF" | "LAB_GUIDE" | "REFERENCE",
  "is_solution": <true if this document contains answers/solutions, false if it is a question sheet>,
  "week_number": <int or 0>,
  "topic": "<primary technical topic covered, e.g. 'SEM-EDX & Backscattered Electron Contrast' or 'Infrared Spectroscopy & Vibrational Modes'>",
  "summary": "<2-3 sentence overview explaining what concepts are covered and what the student is required to do>",
  "key_questions": ["<Question 1 summary>", "<Question 2 summary>"],
  "schedule_items": [
    {{
      "week_number": <int>,
      "event_type": "lecture" | "tutorial" | "ca_quiz" | "exam" | "submission",
      "title": "<event name>",
      "event_date": "<date string>",
      "notes": "<venue/details>"
    }}
  ]
}}"""
            try:
                ai_resp = None
                if llm_provider.is_ready():
                    ai_resp, provider = llm_provider.analyze_document(
                        prompt=prompt,
                        system_prompt="You are an academic materials intelligence reader. Output strictly valid JSON without markdown wrapping.",
                        max_tokens=750,
                        temperature=0.1
                    )

                if not ai_resp and bedrock_client.is_ready():
                    ai_resp = bedrock_client.converse(
                        messages=[{"role": "user", "content": prompt}],
                        system_prompt="You are an academic materials intelligence reader. Output strictly valid JSON without markdown wrapping.",
                        model_id=self.specialist_model,
                        max_tokens=700,
                        temperature=0.1
                    )

                if ai_resp:
                    clean_json = re.sub(r'^```(?:json)?\s*', '', ai_resp.strip(), flags=re.IGNORECASE)
                    clean_json = re.sub(r'\s*```$', '', clean_json).strip()
                    data = json.loads(clean_json)

                    doc_type = data.get("doc_type", doc_type)
                    is_solution = data.get("is_solution", is_solution)
                    week_number = data.get("week_number", 0)
                    topic = data.get("topic", "")
                    summary = data.get("summary", "")
                    if data.get("key_questions"):
                        questions_found = data.get("key_questions")
                    for s in data.get("schedule_items", []):
                        s["source_doc_id"] = mat_id
                        s["source_doc_title"] = title
                        extracted_schedule.append(s)
                    ai_success = True
            except Exception as e:
                print(f"[DocumentAgent] Document parsing exception: {e}")
                ai_success = False

        if not summary:
            if is_solution:
                summary = f"Worked solutions and answer key for {course_code} ({title}). Covers step-by-step problem solutions."
            elif "TUTORIAL" in doc_type:
                summary = f"Tutorial exercise problems for {course_code} ({title}). Contains practice questions to prepare for tutorial classes."
            elif "LECTURE" in doc_type:
                summary = f"Lecture slides for {course_code} covering core curriculum theory."
            else:
                summary = f"Academic document for {course_code} ({title})."

        if not week_number:
            wm = re.search(r'\b(?:week|w|lec|lecture|t)\s*(\d{1,2})\b', title.lower())
            if wm:
                week_number = int(wm.group(1))

        # Save to DB with raw excerpt and extracted questions
        db.upsert_parsed_document(
            doc_id=mat_id,
            course_id=mat.get("course_id", ""),
            course_code=course_code,
            title=title,
            file_name=mat.get("file_name") or Path(local_path).name,
            doc_type=doc_type,
            week_number=week_number,
            topic=topic or title,
            summary=summary,
            local_path=local_path,
            schedule_data_json=json.dumps(extracted_schedule),
            raw_text_excerpt=raw_excerpt[:2500],
            extracted_questions=json.dumps(questions_found),
            is_solution=1 if is_solution else 0
        )

        for item in extracted_schedule:
            db.upsert_course_schedule(
                course_code=course_code,
                week_number=item.get("week_number", 0),
                event_type=item.get("event_type", "lecture"),
                title=item.get("title", "Course Event"),
                event_date=item.get("event_date", ""),
                source_doc_id=mat_id,
                source_doc_title=title,
                notes=item.get("notes", "")
            )

        return {
            "success": True,
            "mat_id": mat_id,
            "course_code": course_code,
            "title": title,
            "doc_type": doc_type,
            "is_solution": is_solution,
            "week_number": week_number,
            "topic": topic,
            "summary": summary,
            "questions_count": len(questions_found),
            "schedule_count": len(extracted_schedule),
            "bedrock_used": ai_success, "ai_parsed": ai_success,
        }

    def analyze_all_downloaded_documents(self, course_code: Optional[str] = None) -> Dict[str, Any]:
        """Batch analyze all downloaded documents."""
        materials = db.get_all_materials(course_code=course_code, downloaded_only=True)
        results = []
        schedules_found = 0
        tutorials_read = 0

        for m in materials:
            if any(x in (m.get("title") or "").lower() for x in ["recorded lecture", "media gallery", "zoom link", ".mp4", ".mov"]):
                continue
            res = self.analyze_document(m["id"])
            if res.get("success"):
                results.append(res)
                schedules_found += res.get("schedule_count", 0)
                if "TUTORIAL" in res.get("doc_type", ""):
                    tutorials_read += 1

        return {
            "documents_analyzed": len(results),
            "tutorials_read": tutorials_read,
            "schedules_extracted": schedules_found,
            "items": results
        }

document_agent = DocumentAgent()
