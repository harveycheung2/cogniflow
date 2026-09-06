import os
import re
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
import pypdf

from core.config import SPECIALIST_MODEL_ID, LEAD_MODEL_ID
from services.aws_bedrock import bedrock_client
from services.ntulearn import ntulearn_service
import core.database as db

SCHEDULE_KEYWORDS = [
    "schedule", "timetable", "syllabus", "calendar", "assessment",
    "continuous assessment", "grading", "scheme", "outline", "tutorial",
    "ca1", "ca2", "final exam"
]

class DocumentAgent:
    def __init__(self):
        self.specialist_model = SPECIALIST_MODEL_ID
        self.lead_model = LEAD_MODEL_ID

    def extract_text_from_pdf(self, file_path: Path, max_pages: int = 30) -> Dict[str, Any]:
        """Extract text from PDF, prioritizing schedule and syllabus pages."""
        if not file_path.exists():
            return {"text": "", "page_count": 0, "schedule_pages": []}

        try:
            reader = pypdf.PdfReader(str(file_path))
            total_pages = len(reader.pages)
            extracted_pages = []
            schedule_pages = []

            for i in range(min(total_pages, max_pages)):
                try:
                    page_text = reader.pages[i].extract_text() or ""
                    clean_text = page_text.strip()
                    if clean_text:
                        extracted_pages.append(f"--- [Slide/Page {i+1}] ---\n{clean_text}")
                        if any(k in clean_text.lower() for k in SCHEDULE_KEYWORDS):
                            schedule_pages.append(i + 1)
                except Exception:
                    continue

            return {
                "text": "\n\n".join(extracted_pages),
                "page_count": total_pages,
                "schedule_pages": schedule_pages,
            }
        except Exception as e:
            return {"text": "", "page_count": 0, "schedule_pages": [], "error": str(e)}

    def parse_schedule_heuristic(self, text: str, course_code: str, doc_id: str, doc_title: str) -> List[Dict[str, Any]]:
        """Deterministic extractor for course schedules & assessment slides."""
        schedule_items = []

        # Line by line pattern for lecture & tutorial schedule tables
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

        # Match assessment weights e.g. "2 CAs (Lecture + Lab) 60%", "1 Final Exam 40%"
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
        """Classifies document and extracts schedule using Bedrock or heuristic fallback."""
        mat = db.get_material_by_id(mat_id)
        if not mat:
            return {"success": False, "error": "Material not found"}

        local_path = mat.get("local_path")
        course_code = mat.get("course_code") or "General"
        title = mat.get("title") or "Untitled Document"

        # Download if needed or if corrupted
        needs_download = False
        if not local_path or not Path(local_path).exists():
            needs_download = True
        else:
            try:
                with open(local_path, "rb") as test_f:
                    if test_f.read(50).startswith(b"<!doc"):
                        needs_download = True
            except Exception:
                needs_download = True

        if needs_download:
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

        pdf_info = self.extract_text_from_pdf(Path(local_path))
        raw_text = pdf_info.get("text", "")
        extracted_schedule = []
        doc_type = mat.get("doc_type") or "REFERENCE"
        week_number = 0
        topic = ""
        summary = ""

        # Extract schedule via heuristic
        heuristic_schedules = self.parse_schedule_heuristic(raw_text, course_code, mat_id, title)
        if heuristic_schedules:
            extracted_schedule.extend(heuristic_schedules)

        # Bedrock analysis attempt
        bedrock_success = False
        if bedrock_client.is_ready() and len(raw_text) > 40:
            prompt = f"""Analyze this course document from {course_code} ({title}):

=== DOCUMENT EXCERPT ===
{raw_text[:4000]}
=== END EXCERPT ===

Return ONLY a JSON object:
{{
  "doc_type": "TUTORIAL_QUESTION" | "TUTORIAL_SOLUTION" | "LECTURE_SLIDES" | "SYLLABUS_SCHEDULE" | "ASSIGNMENT_BRIEF" | "LAB_GUIDE" | "REFERENCE",
  "week_number": <int or 0>,
  "topic": "<short subject topic>",
  "summary": "<2-3 sentence overview>",
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
                ai_resp = bedrock_client.converse(
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt="You are an academic document specialist. Output strictly JSON.",
                    model_id=self.specialist_model,
                    max_tokens=1500,
                    temperature=0.1
                )
                clean_json = re.sub(r'^```json\s*', '', ai_resp.strip())
                clean_json = re.sub(r'\s*```$', '', clean_json).strip()
                data = json.loads(clean_json)

                doc_type = data.get("doc_type", doc_type)
                week_number = data.get("week_number", 0)
                topic = data.get("topic", "")
                summary = data.get("summary", "")
                for s in data.get("schedule_items", []):
                    s["source_doc_id"] = mat_id
                    s["source_doc_title"] = title
                    extracted_schedule.append(s)
                bedrock_success = True
            except Exception:
                bedrock_success = False

        if not summary:
            if "TUTORIAL" in doc_type:
                summary = f"Tutorial exercise document for {course_code}. Contains practice questions to prepare for weekly tutorial discussion."
            elif "LECTURE" in doc_type:
                summary = f"Lecture slide deck for {course_code} detailing curriculum concepts and schedule."
            else:
                summary = f"Academic document for {course_code} ({title})."

        if not week_number:
            wm = re.search(r'\b(?:week|w|lec|lecture|t)\s*(\d{1,2})\b', title.lower())
            if wm:
                week_number = int(wm.group(1))

        # Save to DB
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
            schedule_data_json=json.dumps(extracted_schedule)
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
            "week_number": week_number,
            "topic": topic,
            "summary": summary,
            "schedule_count": len(extracted_schedule),
            "bedrock_used": bedrock_success,
        }

    def analyze_all_downloaded_documents(self, course_code: Optional[str] = None) -> Dict[str, Any]:
        """Batch analyze all downloaded documents."""
        materials = db.get_all_materials(course_code=course_code, downloaded_only=True)
        results = []
        schedules_found = 0

        for m in materials:
            if any(x in (m.get("title") or "").lower() for x in ["recorded lecture", "media gallery", "zoom link"]):
                continue
            res = self.analyze_document(m["id"])
            if res.get("success"):
                results.append(res)
                schedules_found += res.get("schedule_count", 0)

        return {
            "documents_analyzed": len(results),
            "schedules_extracted": schedules_found,
            "items": results
        }

document_agent = DocumentAgent()
