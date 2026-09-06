import json
import time
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, List, Dict, Any
from playwright.sync_api import sync_playwright

from core.config import (
    AUTH_FILE, NTULEARN_BASE_URL, DEFAULT_USER_AGENT, DOWNLOADS_DIR
)
import core.database as db

def sanitize_filename(name: str) -> str:
    if not name:
        return "unnamed"
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name)
    cleaned = cleaned.strip(". ")
    return cleaned[:100] if len(cleaned) > 100 else (cleaned or "item")

class NTULearnService:
    def __init__(self):
        self.auth_file = AUTH_FILE
        self.downloads_dir = DOWNLOADS_DIR

    def is_authenticated(self) -> bool:
        if not self.auth_file.exists():
            return False
        try:
            with open(self.auth_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return len(data.get("cookies", [])) > 0
        except Exception:
            return False

    def get_cookie_header(self) -> str:
        try:
            if not self.auth_file.exists():
                return ""
            with open(self.auth_file, "r", encoding="utf-8") as f:
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
            with urllib.request.urlopen(req, timeout=25) as resp:
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
                        raw_body = raw_body.get("raw") or raw_body.get("text") or ""
                    elif not isinstance(raw_body, str):
                        raw_body = str(raw_body or "")
                    ann_body = re.sub(r'<[^>]+>', '', raw_body).strip()
                    posted_at = ann.get("created", "")
                    db.upsert_announcement(ann_id, course_id, course_code, ann_title, ann_body, posted_at)
                    announcements_saved += 1

        return {
            "success": True,
            "courses_synced": courses_saved,
            "announcements_synced": announcements_saved,
        }

    def fetch_course_contents(self, course_id: str) -> List[Dict[str, Any]]:
        """Fetch content items (folders, documents, files) for a course."""
        url = f"{NTULEARN_BASE_URL}/learn/api/v1/courses/{course_id}/contents?limit=50"
        data = self._api_get(url)
        if not data or "results" not in data:
            return []

        results = []
        for c in data.get("results", []):
            cid = c.get("id")
            title = c.get("title") or "Untitled Material"
            handlers = c.get("contentHandler", {})
            ctype = handlers.get("id", "folder" if c.get("hasChildren") else "document")
            url_link = c.get("links", {}).get("self", "")
            download_url = ""

            # Check attachment or download link
            if "attachment" in str(handlers).lower() or "file" in str(handlers).lower():
                download_url = f"{NTULEARN_BASE_URL}/webapps/blackboard/execute/content/file?cmd=view&content_id={cid}&course_id={course_id}"

            db.upsert_material(cid, course_id, title, ctype, url_link, download_url)
            results.append({
                "id": cid,
                "title": title,
                "type": ctype,
                "download_url": download_url,
            })
        return results

    def download_material_file(self, course_code: str, title: str, download_url: str) -> Optional[Path]:
        """Download file and save to downloads directory."""
        cookie_header = self.get_cookie_header()
        if not cookie_header or not download_url:
            return None

        safe_course = sanitize_filename(course_code)
        safe_title = sanitize_filename(title)
        if not (safe_title.endswith(".pdf") or safe_title.endswith(".pptx") or safe_title.endswith(".zip") or safe_title.endswith(".docx")):
            safe_title += ".pdf"

        dest_dir = self.downloads_dir / safe_course
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / safe_title

        try:
            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Cookie": cookie_header}
            )
            with urllib.request.urlopen(req, timeout=45) as resp, open(dest_file, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
            return dest_file
        except Exception:
            if dest_file.exists():
                try:
                    dest_file.unlink()
                except Exception:
                    pass
            return None

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
