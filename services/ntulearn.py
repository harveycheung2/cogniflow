import os
import json
import time
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any, List
from playwright.sync_api import sync_playwright

from core.config import (
    AUTH_FILE, DOWNLOADS_DIR, NTULEARN_BASE_URL, DEFAULT_USER_AGENT
)
import core.database as db

def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', '_', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:120]

def infer_doc_type(title: str, file_name: str = "") -> str:
    combined = f"{title} {file_name}".lower()
    if "solution" in combined or "sol" in combined or "ans" in combined:
        if "tutorial" in combined or "tut" in combined or re.search(r'\bt\d+\b', combined):
            return "TUTORIAL_SOLUTION"
    if "tutorial" in combined or "tut" in combined or "sheet" in combined or re.search(r'\bt\d+\b', combined):
        return "TUTORIAL_QUESTION"
    if "syllabus" in combined or "overview" in combined or "schedule" in combined or "outline" in combined or "course info" in combined:
        return "SYLLABUS_SCHEDULE"
    if "lec 1" in combined or "lecture 1" in combined or "week 1" in combined or "w1" in combined:
        return "LECTURE_SLIDES"
    if "lec" in combined or "lecture" in combined or "slide" in combined or "notes" in combined:
        return "LECTURE_SLIDES"
    if "lab" in combined or "solidworks" in combined or "experiment" in combined:
        return "LAB_GUIDE"
    if "assignment" in combined or "ca1" in combined or "ca2" in combined or "rubric" in combined or "project" in combined:
        return "ASSIGNMENT_BRIEF"
    return "REFERENCE"

class NTULearnService:
    def __init__(self):
        self.auth_file = AUTH_FILE
        self.downloads_dir = DOWNLOADS_DIR
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

    def is_authenticated(self) -> bool:
        if not self.auth_file.exists():
            return False
        try:
            with open(self.auth_file, "r") as f:
                data = json.load(f)
                return "cookies" in data and len(data["cookies"]) > 0
        except Exception:
            return False

    def get_cookie_header(self) -> str:
        if not self.auth_file.exists():
            return ""
        try:
            with open(self.auth_file, "r") as f:
                data = json.load(f)
                cookies = data.get("cookies", [])
                return "; ".join([
                    f"{c['name']}={c['value']}"
                    for c in cookies
                    if "ntu" in c.get("domain", "") or "blackboard" in c.get("domain", "")
                ])
        except Exception:
            return ""

    def _api_get(self, url: str) -> Optional[Dict[str, Any]]:
        cookie_header = self.get_cookie_header()
        if not cookie_header:
            return None
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Cookie": cookie_header,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    raw_data = resp.read().decode("utf-8", errors="replace")
                    return json.loads(raw_data)
        except Exception:
            return None
        return None

    def get_user_id(self) -> Optional[str]:
        data = self._api_get(f"{NTULEARN_BASE_URL}/learn/api/v1/users/me")
        if data:
            return data.get("id")
        return None

    def sync_courses_and_announcements(self) -> Dict[str, Any]:
        """Fetch all courses, save to SQLite, and pull recent announcements."""
        user_id = self.get_user_id()
        if not user_id:
            return {"success": False, "error": "Session expired or not authenticated."}

        memberships_url = (
            f"{NTULEARN_BASE_URL}/learn/api/v1/users/{user_id}/memberships"
            f"?expand=course.effectiveAvailability,course.permissions,courseRole&includeCount=true&limit=100"
        )
        data = self._api_get(memberships_url)
        if not data or "results" not in data:
            return {"success": False, "error": "Failed to fetch course memberships."}

        courses_saved = 0
        announcements_saved = 0

        for item in data.get("results", []):
            course_obj = item.get("course", {})
            course_id = item.get("courseId") or course_obj.get("id")
            if not course_id:
                continue

            course_code = course_obj.get("courseId", "")
            title = course_obj.get("name") or course_obj.get("title") or course_code
            term = "General"

            # Parse term e.g. 25S2, 26S1
            m = re.search(r'(\d{2}[sS]\d)', course_code)
            if m:
                term = m.group(1).upper()

            db.upsert_course(course_id, course_code, title, term)
            courses_saved += 1

            # Fetch announcements for this course
            ann_url = f"{NTULEARN_BASE_URL}/learn/api/v1/courses/{course_id}/announcements?limit=5"
            ann_data = self._api_get(ann_url)
            if ann_data and "results" in ann_data:
                for ann in ann_data["results"]:
                    ann_id = ann.get("id", "")
                    ann_title = ann.get("title", "Course Announcement")
                    raw_body = ann.get("body", "")
                    if isinstance(raw_body, dict):
                        raw_body = raw_body.get("rawText") or raw_body.get("displayText") or raw_body.get("text") or raw_body.get("raw") or ""
                    elif not isinstance(raw_body, str):
                        raw_body = str(raw_body or "")
                    import html as html_module
                    clean_text = html_module.unescape(raw_body)
                    clean_text = re.sub(r'<br\s*/?>', '\n', clean_text)
                    clean_text = re.sub(r'</p>', '\n', clean_text)
                    ann_body = re.sub(r'<[^>]+>', '', clean_text).strip()
                    posted_at = ann.get("createdDate") or ann.get("modifiedDate") or ann.get("created", "")
                    db.upsert_announcement(ann_id, course_id, course_code, ann_title, ann_body, posted_at)
                    announcements_saved += 1

        return {
            "success": True,
            "courses_synced": courses_saved,
            "announcements_synced": announcements_saved,
        }

    def crawl_course_materials(self, course_id: str, course_code: str = "") -> List[Dict[str, Any]]:
        """Recursively crawl all contents, folders, and documents for a course including Blackboard Ultra documents."""
        discovered = []
        visited_ids = set()

        def _traverse(url: str, parent_id: str = "", depth: int = 0):
            if depth > 8:
                return
            data = self._api_get(url)
            if not data or "results" not in data:
                return

            for item in data.get("results", []):
                cid = item.get("id")
                if not cid or cid in visited_ids:
                    continue
                visited_ids.add(cid)

                title = item.get("title") or "Untitled Item"
                handler_val = item.get("contentHandler")
                handler_str = handler_val if isinstance(handler_val, str) else str(handler_val or "")

                detail = item.get("contentDetail", {})
                file_info = detail.get("resource/x-bb-file", {}).get("file", {})
                if not file_info:
                    for k, v in detail.items():
                        if isinstance(v, dict) and "file" in v:
                            file_info = v.get("file", {})
                            break

                f_name = file_info.get("fileName") or ""
                f_size = file_info.get("fileSize") or 0
                perm_url = file_info.get("permanentUrl") or ""
                viewer_url = file_info.get("viewerUrl") or ""

                download_url = ""
                if perm_url:
                    download_url = f"{NTULEARN_BASE_URL}{perm_url}"
                elif viewer_url:
                    clean_viewer = viewer_url.split("?")[0]
                    download_url = f"{NTULEARN_BASE_URL}{clean_viewer}"
                elif "file" in handler_str.lower() or "attachment" in handler_str.lower():
                    download_url = f"{NTULEARN_BASE_URL}/webapps/blackboard/execute/content/file?cmd=view&content_id={cid}&course_id={course_id}"

                # Check if this is an Ultra Document or folder containing embedded files in HTML body
                embedded_files_found = False
                raw_body = item.get("body", {}).get("rawText") or ""
                if not raw_body and "document" in handler_str.lower():
                    detail_resp = self._api_get(f"{NTULEARN_BASE_URL}/learn/api/v1/courses/{course_id}/contents/{cid}")
                    if detail_resp:
                        raw_body = detail_resp.get("body", {}).get("rawText") or ""

                if raw_body:
                    import html as html_lib
                    unescaped = html_lib.unescape(raw_body)
                    bbfile_matches = re.findall(r'data-bbfile=["\']({.*?})["\']', unescaped)
                    file_idx = 0
                    for m_json in bbfile_matches:
                        try:
                            finfo = json.loads(m_json)
                            r_url = finfo.get("resourceUrl") or ""
                            d_name = finfo.get("displayName") or finfo.get("linkName") or ""
                            if r_url:
                                file_idx += 1
                                sub_id = f"{cid}_f{file_idx}" if file_idx > 1 else cid
                                sub_title = d_name or title
                                sub_doctype = infer_doc_type(sub_title, d_name)
                                db.upsert_material(
                                    mat_id=sub_id,
                                    course_id=course_id,
                                    title=sub_title,
                                    content_type="resource/x-bb-file",
                                    url=item.get("links", {}).get("self", ""),
                                    download_url=r_url,
                                    parent_id=parent_id or cid,
                                    course_code=course_code,
                                    file_size=0,
                                    file_name=d_name,
                                    doc_type=sub_doctype
                                )
                                discovered.append({
                                    "id": sub_id,
                                    "title": sub_title,
                                    "file_name": d_name,
                                    "download_url": r_url,
                                    "doc_type": sub_doctype,
                                    "course_code": course_code,
                                    "file_size": 0
                                })
                                embedded_files_found = True
                        except Exception:
                            pass

                if not embedded_files_found:
                    doc_type = infer_doc_type(title, f_name)
                    db.upsert_material(
                        mat_id=cid,
                        course_id=course_id,
                        title=title,
                        content_type=handler_str,
                        url=item.get("links", {}).get("self", ""),
                        download_url=download_url,
                        parent_id=parent_id,
                        course_code=course_code,
                        file_size=f_size,
                        file_name=f_name,
                        doc_type=doc_type
                    )
                    item_record = {
                        "id": cid,
                        "title": title,
                        "file_name": f_name,
                        "download_url": download_url,
                        "doc_type": doc_type,
                        "course_code": course_code,
                        "file_size": f_size,
                    }
                    discovered.append(item_record)

                # If folder, lesson, module, or document has children, recurse
                if any(x in handler_str for x in ["folder", "lesson", "module", "document"]) or item.get("hasChildren"):
                    child_url = f"{NTULEARN_BASE_URL}/learn/api/v1/courses/{course_id}/contents/{cid}/children?limit=100"
                    _traverse(child_url, parent_id=cid, depth=depth + 1)

        root_url = f"{NTULEARN_BASE_URL}/learn/api/v1/courses/{course_id}/contents?limit=100"
        _traverse(root_url, parent_id="", depth=0)
        return discovered

    def download_material_file(self, mat_id: str, course_code: str, title: str, download_url: str, file_name: str = "") -> Optional[Path]:
        """Download file and save to downloads directory."""
        cookie_header = self.get_cookie_header()
        if not cookie_header or not download_url:
            return None

        # Clean URL if contains query params
        if "/bbcswebdav/" in download_url and "?" in download_url:
            download_url = download_url.split("?")[0]

        clean_course = sanitize_filename(course_code.split("-")[1] if "-" in course_code else course_code)
        if not clean_course:
            clean_course = "General"

        dest_dir = self.downloads_dir / clean_course
        dest_dir.mkdir(parents=True, exist_ok=True)

        candidate_name = file_name or title
        clean_name = sanitize_filename(candidate_name)
        if not any(clean_name.lower().endswith(ext) for ext in [".pdf", ".pptx", ".ppt", ".docx", ".doc", ".zip", ".xlsx"]):
            clean_name += ".pdf"

        dest_file = dest_dir / clean_name

        try:
            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Cookie": cookie_header}
            )
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest_file, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)

            # Check if file is HTML redirect/error
            with open(dest_file, "rb") as check_f:
                head = check_f.read(200)
                if head.startswith(b"<!doc") or head.startswith(b"<html"):
                    dest_file.unlink(missing_ok=True)
                    return None

            file_size = dest_file.stat().st_size
            db.mark_material_downloaded(mat_id, str(dest_file), file_size, clean_name)
            return dest_file
        except Exception:
            if dest_file.exists():
                try:
                    dest_file.unlink()
                except Exception:
                    pass
            return None

    def download_all_documents_for_term(self, term: str = "26S1", progress_callback=None) -> Dict[str, Any]:
        """Crawl all enrolled courses in term, then download all PDFs/documents."""
        courses = db.get_all_courses(term=term)
        total_discovered = 0
        total_downloaded = 0
        failed = 0

        for c in courses:
            cid = c["id"]
            ccode = c["course_code"]
            if progress_callback:
                progress_callback(f"Discovering contents for {ccode}...")
            discovered = self.crawl_course_materials(cid, course_code=ccode)
            total_discovered += len(discovered)

        # Download pending downloadable materials (priority: PDFs, docx, pptx)
        all_materials = db.get_all_materials(downloaded_only=False)
        downloadable = [
            m for m in all_materials
            if m.get("download_url") and not m.get("downloaded")
            and not any(x in (m.get("title") or "").lower() for x in ["recorded lecture", "media gallery", "zoom link"])
        ]

        count = 0
        for m in downloadable:
            count += 1
            if progress_callback:
                progress_callback(f"Downloading [{count}/{len(downloadable)}]: {m.get('title')[:40]}...")
            path = self.download_material_file(
                mat_id=m["id"],
                course_code=m.get("course_code") or "General",
                title=m["title"],
                download_url=m["download_url"],
                file_name=m.get("file_name") or ""
            )
            if path and path.exists():
                total_downloaded += 1
            else:
                failed += 1

        return {
            "total_courses": len(courses),
            "total_discovered": total_discovered,
            "total_downloaded": total_downloaded,
            "failed": failed,
        }

    def trigger_login_portal(self) -> bool:
        """Launches visible Chromium browser for student SSO & 2FA login."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = context.new_page()

            try:
                page.goto(NTULEARN_BASE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

            start_time = time.time()
            logged_in = False

            while time.time() - start_time < 300: # 5 min timeout
                try:
                    current_url = page.url.lower()
                    if "/ultra" in current_url and not any(s in current_url for s in ["login", "sso", "adfs"]):
                        page.wait_for_timeout(2500)
                        logged_in = True
                        break
                    if page.query_selector("nav[aria-label*='Navigation'], a[href*='/ultra/course']"):
                        page.wait_for_timeout(2000)
                        logged_in = True
                        break
                except Exception:
                    pass
                time.sleep(1)

            if logged_in:
                context.storage_state(path=str(self.auth_file))
                browser.close()
                return True
            else:
                browser.close()
                return False

ntulearn_service = NTULearnService()
