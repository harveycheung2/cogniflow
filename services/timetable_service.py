import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
import pdfplumber

import core.database as db
from core.config import BASE_DIR

TIMETABLE_DIR = BASE_DIR / "data" / "timetable"
TIMETABLE_DIR.mkdir(parents=True, exist_ok=True)

class TimetableService:
    def __init__(self):
        self.timetable_dir = TIMETABLE_DIR

    def parse_timetable_pdf(self, pdf_path: Path) -> Dict[str, Any]:
        """Extract all structured academic and schedule information from an NTU STARS Timetable PDF."""
        with pdfplumber.open(str(pdf_path)) as pdf:
            p0 = pdf.pages[0]
            words = p0.extract_words()
            raw_text = p0.extract_text() or ""
            tables = p0.extract_tables()

        # 1. Student Name
        student_name = ""
        # Check top words (y < 35)
        top_words = [w for w in words if w["top"] < 35 and w["text"].upper() not in ["TIME\\DAY", "MON", "TUE", "WED", "THU", "FRI", "SAT"]]
        if top_words:
            top_words.sort(key=lambda w: w["x0"])
            candidate = " ".join(w["text"] for w in top_words).strip()
            if len(candidate) >= 3 and not re.search(r'\b(?:page|date|time)\b', candidate, re.IGNORECASE):
                student_name = candidate

        if not student_name:
            # Fallback bottom regex
            bottom_match = re.search(r'(?:Legend:.*?SEM\s*-\s*Seminar\s*)([A-Z\s\-]{4,40})\Z', raw_text, re.DOTALL)
            if bottom_match:
                student_name = bottom_match.group(1).strip()

        # 2. Academic Year & Semester
        academic_year = "2026"
        semester = "Semester 1"
        term = "26S1"
        term_match = re.search(r'Academic Year\s*(\d{4})\s*,\s*Semester\s*(\d+)', raw_text, re.IGNORECASE)
        if term_match:
            year_str = term_match.group(1)
            sem_str = term_match.group(2)
            academic_year = year_str
            semester = f"Semester {sem_str}"
            term = f"{year_str[-2:]}S{sem_str}"

        # 3. Registered Courses Table
        courses = []
        total_courses = 0
        total_aus = 0

        course_table = None
        for tbl in tables:
            for row in tbl:
                row_str = " ".join(c for c in row if c)
                if "Index" in row_str and "Course" in row_str and "Exam" in row_str:
                    course_table = tbl
                    break
            if course_table:
                break

        if course_table:
            header_idx = -1
            for i, row in enumerate(course_table):
                row_str = " ".join(c for c in row if c)
                if "Index" in row_str and "Course" in row_str:
                    header_idx = i
                    break

            if header_idx != -1:
                for row in course_table[header_idx + 1:]:
                    clean_row = [c.strip().replace("\n", " ") if c else "" for c in row]
                    if not clean_row or not clean_row[0]:
                        continue
                    if clean_row[0].lower() == "total":
                        m_tot = re.search(r'(\d+)\s*Course', " ".join(clean_row))
                        if m_tot:
                            total_courses = int(m_tot.group(1))
                        m_au = re.search(r'(\d+)\s*AU', " ".join(clean_row))
                        if m_au:
                            total_aus = int(m_au.group(1))
                        continue

                    index_no = clean_row[0]
                    if not re.match(r'^\d{4,6}$', index_no):
                        continue

                    course_code = clean_row[1] if len(clean_row) > 1 else ""
                    title = clean_row[2] if len(clean_row) > 2 else ""
                    aus = int(clean_row[3]) if len(clean_row) > 3 and clean_row[3].isdigit() else 0
                    status = clean_row[4] if len(clean_row) > 4 else "Registered"
                    exam_sched = clean_row[5] if len(clean_row) > 5 else "Not Applicable"

                    courses.append({
                        "index": index_no,
                        "course_code": course_code,
                        "title": title,
                        "aus": aus,
                        "status": status,
                        "exam_schedule": exam_sched
                    })

        if not total_courses and courses:
            total_courses = len(courses)
        if not total_aus and courses:
            total_aus = sum(c["aus"] for c in courses)

        # 4. Weekly Grid Slots
        grid_table = None
        for tbl in tables:
            if tbl and len(tbl) > 1:
                first_row_str = " ".join(c for c in tbl[0] if c)
                if "TIME" in first_row_str and "MON" in first_row_str:
                    grid_table = tbl
                    break

        weekly_slots = []
        days_map = {1: "MON", 2: "TUE", 3: "WED", 4: "THU", 5: "FRI", 6: "SAT"}

        if grid_table:
            for row in grid_table[1:]:
                time_col = row[0].replace("\n", " ").strip() if row[0] else ""
                for col_idx in range(1, len(row)):
                    cell_val = row[col_idx]
                    if not cell_val or not cell_val.strip():
                        continue
                    day_name = days_map.get(col_idx, f"DAY_{col_idx}")

                    class_chunks = [c.strip() for c in cell_val.strip().split(";") if c.strip()]
                    for chunk in class_chunks:
                        chunk_clean = chunk.replace("\n", " ").strip()
                        chunk_clean = re.sub(r'E-\s*SPACE', 'E-SPACE', chunk_clean, flags=re.IGNORECASE)
                        chunk_clean = re.sub(r'SBS-\s*TR\+', 'SBS-TR+', chunk_clean, flags=re.IGNORECASE)

                        m_code = re.search(r'\b([A-Z]{2,4}\d{4})\b', chunk_clean)
                        course_code = m_code.group(1) if m_code else ""

                        m_type = re.search(r'\b(LEC/STU|LEC|TUT|LAB|SEM|PRJ|DES)\b', chunk_clean)
                        event_type = m_type.group(1) if m_type else "Class"

                        m_time = re.search(r'(\d{4})to(\d{4})', chunk_clean)
                        if m_time:
                            s_raw = m_time.group(1)
                            e_raw = m_time.group(2)
                            start_time = f"{s_raw[:2]}:{s_raw[2:]}"
                            end_time = f"{e_raw[:2]}:{e_raw[2:]}"
                        else:
                            start_time = ""
                            end_time = ""

                        m_wk = re.search(r'Wk[\d,\-]+', chunk_clean)
                        weeks = m_wk.group(0) if m_wk else "All Weeks"

                        m_venue = re.search(r'\b(LT\d+[A-Z]?|TR\+\d+|SBS-TR\+\d+|UGLAB|E-SPACE|[A-Z]{2,4}-[\w\+]+)\b', chunk_clean)
                        venue = m_venue.group(1) if m_venue else ""

                        group = ""
                        if course_code and event_type:
                            after_type = chunk_clean.split(event_type, 1)
                            if len(after_type) > 1:
                                parts = after_type[1].strip().split()
                                if parts:
                                    group = parts[0]

                        weekly_slots.append({
                            "day": day_name,
                            "course_code": course_code,
                            "event_type": event_type,
                            "group": group,
                            "venue": venue,
                            "start_time": start_time,
                            "end_time": end_time,
                            "time_range": f"{start_time} - {end_time}" if start_time else time_col,
                            "weeks": weeks,
                            "raw_entry": chunk_clean
                        })

        day_order = {"MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6, "SUN": 7}
        weekly_slots.sort(key=lambda s: (day_order.get(s["day"], 9), s["start_time"]))

        return {
            "student_name": student_name,
            "academic_year": academic_year,
            "semester": semester,
            "term": term,
            "total_courses": total_courses,
            "total_aus": total_aus,
            "courses": courses,
            "weekly_slots": weekly_slots
        }

    def save_timetable(self, parsed_data: Dict[str, Any], file_path: Path, file_name: str) -> Dict[str, Any]:
        """Persist timetable document to database, schedule table, and exam tasks."""
        term = parsed_data.get("term", "26S1")
        student_name = parsed_data.get("student_name", "Student")
        total_courses = parsed_data.get("total_courses", len(parsed_data.get("courses", [])))
        total_aus = parsed_data.get("total_aus", sum(c.get("aus", 0) for c in parsed_data.get("courses", [])))
        data_json = json.dumps(parsed_data, ensure_ascii=False)
        file_size = file_path.stat().st_size if file_path.exists() else 0

        # 1. Save document record
        db.save_timetable_document(
            doc_id="active_timetable",
            file_name=file_name,
            file_path=str(file_path),
            file_size=file_size,
            student_name=student_name,
            term=term,
            total_courses=total_courses,
            total_aus=total_aus,
            data_json=data_json
        )

        # 2. Upsert weekly slots into course_schedules
        weekly_slots = parsed_data.get("weekly_slots", [])
        for slot in weekly_slots:
            c_code = slot.get("course_code") or "CLASS"
            e_type = slot.get("event_type") or "Lecture"
            venue = slot.get("venue") or ""
            day = slot.get("day", "")
            trange = slot.get("time_range", "")
            title = f"{c_code} {e_type} ({venue})" if venue else f"{c_code} {e_type}"
            notes = f"Group: {slot.get('group', '')} | Venue: {venue} | Weeks: {slot.get('weeks', '')}"
            
            db.upsert_course_schedule(
                course_code=c_code,
                week_number=0,
                event_type=e_type,
                title=title,
                event_date=f"{day} {trange}".strip(),
                source_doc_id="timetable",
                source_doc_title=f"Official STARS Timetable ({term})",
                notes=notes
            )

        # 3. Create high-priority action items for upcoming exams
        courses = parsed_data.get("courses", [])
        for course in courses:
            exam_sched = course.get("exam_schedule", "")
            if exam_sched and "not applicable" not in exam_sched.lower():
                c_code = course.get("course_code", "")
                c_title = course.get("title", "")
                task_id = f"exam_{c_code}_{course.get('index', '')}"
                exam_task_title = f"{c_code} Final Exam ({c_title})"
                try:
                    with db.get_connection() as conn:
                        conn.execute("""
                        INSERT INTO tasks (id, title, source, course_code, term, due_date, estimated_minutes, priority_score, status, notes)
                        VALUES (?, ?, 'timetable', ?, ?, ?, 120, 9.8, 'pending', ?)
                        ON CONFLICT(id) DO UPDATE SET
                            title=excluded.title,
                            due_date=excluded.due_date,
                            priority_score=excluded.priority_score,
                            notes=excluded.notes,
                            term=excluded.term
                        """, (
                            task_id,
                            exam_task_title,
                            c_code,
                            term,
                            exam_sched,
                            f"Official STARS Exam: {exam_sched}"
                        ))
                        conn.commit()
                except Exception as e:
                    print(f"Error upserting exam task for {c_code}: {e}")

        return parsed_data

    def get_active_timetable(self, term: str = "26S1") -> Optional[Dict[str, Any]]:
        return db.get_active_timetable(term=term)

    def seed_default_if_needed(self, term: str = "26S1") -> Optional[Dict[str, Any]]:
        """Seed the sample timetable if available and no active timetable is in the database."""
        existing = self.get_active_timetable(term=term)
        if existing:
            return existing

        default_file = self.timetable_dir / "harvey_timetable_2026s1.pdf"
        if default_file.exists():
            try:
                parsed = self.parse_timetable_pdf(default_file)
                saved = self.save_timetable(parsed, default_file, default_file.name)
                print(f"[TimetableService] Automatically seeded default timetable for {parsed.get('student_name')}")
                return db.get_active_timetable(term=term)
            except Exception as e:
                print(f"[TimetableService] Error seeding default timetable: {e}")
        return None

timetable_service = TimetableService()
