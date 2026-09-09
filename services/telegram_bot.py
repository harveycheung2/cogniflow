import os
import re
import time
import json
import uuid
import mimetypes
import logging
import threading
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from core.config import (
    TELEGRAM_BOT_TOKEN, DOWNLOADS_DIR, APP_NAME, BASE_DIR
)
import core.database as db
from services.agents import execute_chat_query
from services.planner import generate_day_schedule
from services.timetable_service import timetable_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("CogniFlowTelegram")

def infer_doc_type(title: str, file_name: str = "") -> str:
    combined = f"{title} {file_name}".lower()
    if "solution" in combined or "sol" in combined or "ans" in combined:
        return "TUTORIAL_SOLUTION"
    if "tutorial" in combined or "tut" in combined or "sheet" in combined or re.search(r'\bt\d+\b', combined):
        return "TUTORIAL_QUESTION"
    if "syllabus" in combined or "overview" in combined or "schedule" in combined or "outline" in combined:
        return "SYLLABUS_SCHEDULE"
    if "lec" in combined or "lecture" in combined or "slide" in combined:
        return "LECTURE_SLIDES"
    if "lab" in combined or "experiment" in combined:
        return "LAB_GUIDE"
    return "REFERENCE"

class TelegramBot:
    def __init__(self, token: Optional[str] = None):
        self.token = token or TELEGRAM_BOT_TOKEN
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{self.token}"
        self.is_running = False
        self.last_update_id = 0
        self.bot_info = {}
        # Multi-turn conversational state tracking per chat_id (e.g. interactive /addtask)
        self.user_states: Dict[int, Dict[str, Any]] = {}

    def api_call(self, endpoint: str, data: Optional[Dict[str, Any]] = None, files: Optional[Dict[str, Tuple[str, bytes]]] = None, timeout: int = 35) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        headers = {"User-Agent": "CogniFlowTelegramBot/1.0"}

        if files:
            boundary = f"----CogniFlowBoundary{uuid.uuid4().hex}"
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            crlf = b"\r\n"
            body_parts = []

            if data:
                for k, v in data.items():
                    if v is not None:
                        body_parts.append(b"--" + boundary.encode("utf-8") + crlf)
                        body_parts.append(f'Content-Disposition: form-data; name="{k}"'.encode("utf-8") + crlf + crlf)
                        body_parts.append(str(v).encode("utf-8") + crlf)

            for field_name, (filename, file_bytes) in files.items():
                mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                body_parts.append(b"--" + boundary.encode("utf-8") + crlf)
                body_parts.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode("utf-8") + crlf)
                body_parts.append(f'Content-Type: {mime_type}'.encode("utf-8") + crlf + crlf)
                body_parts.append(file_bytes + crlf)

            body_parts.append(b"--" + boundary.encode("utf-8") + b"--" + crlf)
            payload = b"".join(body_parts)
            req = urllib.request.Request(url, data=payload, headers=headers)
        elif data:
            payload = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=payload, headers=headers)
        else:
            req = urllib.request.Request(url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            logger.error(f"Telegram HTTPError on {endpoint}: {e.code} - {err_body}")
            try:
                return json.loads(err_body)
            except Exception:
                return {"ok": False, "error_code": e.code, "description": str(e)}
        except Exception as e:
            logger.error(f"Telegram API Exception on {endpoint}: {e}")
            return {"ok": False, "description": str(e)}

    def get_me(self) -> Optional[Dict[str, Any]]:
        res = self.api_call("getMe", timeout=10)
        if res.get("ok"):
            self.bot_info = res["result"]
            return self.bot_info
        return None

    def send_chat_action(self, chat_id: int, action: str = "typing"):
        return self.api_call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=8)

    def send_message(self, chat_id: int, text: str, parse_mode: Optional[str] = "Markdown", reply_markup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            data["reply_markup"] = reply_markup

        if len(text) > 4000:
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            last_res = {}
            for chunk in chunks:
                data["text"] = chunk
                res = self.api_call("sendMessage", data)
                if not res.get("ok") and "parse_mode" in data:
                    data_plain = dict(data)
                    data_plain.pop("parse_mode", None)
                    res = self.api_call("sendMessage", data_plain)
                last_res = res
                time.sleep(0.1)
            return last_res

        res = self.api_call("sendMessage", data)
        if not res.get("ok") and parse_mode:
            data_plain = dict(data)
            data_plain.pop("parse_mode", None)
            res = self.api_call("sendMessage", data_plain)
        return res

    def send_document(self, chat_id: int, file_path: Path, caption: Optional[str] = None) -> Dict[str, Any]:
        if not file_path.exists():
            return {"ok": False, "error": "File not found"}

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        files = {"document": (file_path.name, file_bytes)}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        return self.api_call("sendDocument", data=data, files=files, timeout=60)

    def download_telegram_file(self, file_id: str, dest_path: Path) -> bool:
        """Downloads a document or media file from Telegram's servers to local storage."""
        info = self.api_call("getFile", {"file_id": file_id})
        if not info.get("ok"):
            logger.error(f"Could not fetch file info for {file_id}: {info}")
            return False

        remote_path = info["result"].get("file_path")
        if not remote_path:
            return False

        file_url = f"{self.file_base_url}/{remote_path}"
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        req = urllib.request.Request(file_url, headers={"User-Agent": "CogniFlowTelegramBot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                with open(dest_path, "wb") as out_f:
                    out_f.write(resp.read())
            logger.info(f"Downloaded Telegram file to {dest_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to download Telegram file from {file_url}: {e}")
            return False

    def format_courses_list(self) -> str:
        courses = db.get_all_courses(term="26S1")
        if not courses:
            return (
                "📚 *Your Enrolled Modules (26S1):*\n"
                "You don't have any modules registered yet!\n\n"
                "💡 *How to add your courses:*\n"
                "1. **Send your Timetable PDF** directly here to auto-import your courses and class timings!\n"
                "2. Or add a course manually: `/addcourse SC2001 Algorithm Design`"
            )

        lines = ["📚 *Your Enrolled Modules (26S1):*\n"]
        for c in courses:
            code = c.get("course_code", "")
            title = c.get("title", "")
            materials = db.get_all_materials(course_code=code)
            lines.append(f"• *{code}*: {title} _({len(materials)} docs)_")

        lines.append("\n💡 _Ask me 'What tutorial do I need?' or upload a tutorial PDF to start analyzing!_")
        return "\n".join(lines)

    def format_schedule(self) -> str:
        blocks = generate_day_schedule(term="26S1")
        if not blocks or not isinstance(blocks, list):
            return (
                "🗓️ *Today's Schedule:*\n"
                "No scheduled study blocks or events found.\n\n"
                "💡 _Send your NTU Timetable PDF to register your classes, or use `/addtask` to create action items._"
            )

        lines = ["🗓️ *Today's Optimized Time-Blocked Schedule:*\n"]
        for b in blocks:
            time_range = f"{b.get('start', '')} - {b.get('end', '')}"
            title = b.get("title", "Study Block")
            code = b.get("course_code", "Academic")
            duration = b.get("duration_mins", 60)
            lines.append(f"⚡ *{time_range}* ({duration}m)\n   [{code}] {title}")

        return "\n".join(lines)

    def format_urgent_deadlines(self) -> str:
        announcements = db.get_announcements(term="26S1", limit=10)
        tasks = db.get_tasks(term="26S1", status="pending")

        lines = ["🚨 *Urgent Deadlines & Recent Announcements:*\n"]

        if tasks:
            lines.append("*Pending Action Items:*")
            for t in tasks[:6]:
                due = t.get("due_date", "Soon")
                prio = t.get("priority_score", 1.0)
                prio_tag = "🔴 High" if prio >= 7.0 else "🟡 Medium" if prio >= 4.0 else "🟢 Normal"
                lines.append(f"• [{prio_tag}] *{t.get('title')}* (Due: {due})")
            lines.append("")

        if announcements:
            lines.append("*Latest Notices:*")
            for a in announcements[:4]:
                c_code = a.get("course_code", "Notice")
                title = a.get("title", "")
                lines.append(f"📢 *[{c_code}]* {title}")
        else:
            lines.append("No urgent announcement flags found.")

        return "\n".join(lines)

    def handle_document_upload(self, chat_id: int, user_id: str, user_name: str, doc: Dict[str, Any], caption: str):
        file_id = doc.get("file_id")
        file_name = doc.get("file_name", f"upload_{int(time.time())}.pdf")
        mime_type = doc.get("mime_type", "")

        self.send_chat_action(chat_id, "upload_document")
        self.send_message(chat_id, f"📥 Receiving `{file_name}`... Analyzing file with AI...")

        user_downloads = db.get_user_downloads_dir(user_id)
        dest_file = user_downloads / file_name

        success = self.download_telegram_file(file_id, dest_file)
        if not success:
            self.send_message(chat_id, "❌ Failed to download file from Telegram servers. Please try again.")
            return

        is_pdf = file_name.lower().endswith(".pdf") or "pdf" in mime_type.lower()
        combined_text = f"{file_name} {caption}".lower()

        # Check if it's a Timetable PDF
        is_timetable = (
            "timetable" in combined_text or
            "schedule" in combined_text or
            "/timetable" in caption.lower() or
            "acad_cal" in combined_text
        )

        if is_pdf and is_timetable:
            try:
                parsed = timetable_service.parse_timetable_pdf(dest_file)
                saved = timetable_service.save_timetable(parsed, dest_file, file_name)

                courses_added = []
                for c in parsed.get("courses", []):
                    code = c.get("course_code", "").strip()
                    title = c.get("course_title", "").strip() or code
                    if code:
                        db.upsert_course(f"course_{code}_{user_id}", code, title, term="26S1")
                        courses_added.append(code)

                course_str = ", ".join(courses_added) if courses_added else "courses extracted"
                self.send_message(
                    chat_id,
                    f"🎉 *Timetable Registered Successfully!*\n\n"
                    f"• *Parsed Classes:* {len(parsed.get('slots', []))} weekly slots\n"
                    f"• *Modules Detected:* {course_str}\n\n"
                    f"Your weekly classes and venues are now active in your workspace. Try `/schedule` or ask _'When is my next lecture?'_!"
                )
                return
            except Exception as e:
                logger.error(f"Error parsing timetable PDF: {e}")

        # Otherwise: Index as Course Material
        try:
            doc_type = infer_doc_type(caption, file_name)
            assigned_code = "General"
            existing_courses = db.get_all_courses(term="26S1")
            for ec in existing_courses:
                if ec["course_code"].lower() in combined_text:
                    assigned_code = ec["course_code"]
                    break

            mat_id = f"mat_user_{uuid.uuid4().hex[:10]}"
            file_size = dest_file.stat().st_size if dest_file.exists() else 0

            db.upsert_material(
                mat_id=mat_id,
                course_id=f"course_{assigned_code}",
                title=file_name.replace(".pdf", "").replace("_", " "),
                content_type=mime_type or "application/pdf",
                url="",
                download_url=""
            )
            db.mark_material_downloaded(
                mat_id=mat_id,
                local_path=str(dest_file),
                file_size=file_size,
                file_name=file_name,
                doc_type=doc_type
            )

            self.send_message(
                chat_id,
                f"📄 *Document Indexed in Your Workspace!*\n\n"
                f"• *File:* `{file_name}`\n"
                f"• *Module:* `{assigned_code}`\n"
                f"• *Type:* `{doc_type}`\n\n"
                f"You can now ask me questions about this document (e.g. _'What are the questions in this document?'_)!"
            )
        except Exception as e:
            logger.error(f"Error indexing document: {e}")
            self.send_message(chat_id, f"⚠️ Saved file, but encountered indexing note: {str(e)[:150]}")

    def process_message(self, message: Dict[str, Any]):
        chat_id = message.get("chat", {}).get("id")
        user_id = str(message.get("from", {}).get("id", chat_id))
        user_name = message.get("from", {}).get("first_name", "Student")
        text = message.get("text", "").strip()
        caption = message.get("caption", "").strip()
        doc = message.get("document")

        if not chat_id:
            return

        # Activate User-Specific Database & File Isolation
        db.set_user_context(user_id)

        # Handle Incoming Document/PDF Uploads
        if doc:
            self.handle_document_upload(chat_id, user_id, user_name, doc, caption or text)
            return

        if not text:
            return

        logger.info(f"Incoming from {user_name} ({user_id}): {text}")

        # =====================================================================
        # INTERACTIVE TASK CREATION CONVERSATIONAL FLOW
        # =====================================================================
        user_state = self.user_states.get(chat_id)

        # Allow user to abort at any time
        if text.startswith("/cancel"):
            if user_state:
                self.user_states.pop(chat_id, None)
                self.send_message(chat_id, "🚫 Action cancelled. What would you like to do next?")
            else:
                self.send_message(chat_id, "No active action to cancel.")
            return

        # Step 2: Waiting for deadline after user entered task title
        if user_state and user_state.get("step") == "waiting_task_deadline":
            deadline_input = text.strip()
            task_title = user_state.get("task_title", "New Task")
            course_code = user_state.get("course_code", "General")
            self.user_states.pop(chat_id, None)

            # Determine priority score based on deadline urgency
            lower_d = deadline_input.lower()
            if lower_d in ["none", "skip", "no", "nil", "-", "no deadline"]:
                due_date_str = "No fixed deadline"
                priority_score = 3.0
                prio_label = "🟢 Normal"
            elif any(w in lower_d for w in ["today", "tonight", "urgent", "asap", "immediate"]):
                due_date_str = deadline_input
                priority_score = 9.0
                prio_label = "🔴 Critical"
            elif any(w in lower_d for w in ["tomorrow", "tmr"]):
                due_date_str = deadline_input
                priority_score = 8.0
                prio_label = "🔴 High"
            else:
                due_date_str = deadline_input
                priority_score = 5.0
                prio_label = "🟡 Medium"

            task_id = f"task_{uuid.uuid4().hex[:8]}"
            db.upsert_task(
                task_id=task_id,
                title=task_title,
                source="telegram",
                course_code=course_code,
                due_date=due_date_str,
                estimated_minutes=60,
                priority_score=priority_score,
                notes=f"Added via Telegram by {user_name}",
                term="26S1"
            )

            self.send_message(
                chat_id,
                f"✅ *Task Added Successfully!*\n\n"
                f"📌 *Task:* {task_title}\n"
                f"📚 *Module:* {course_code}\n"
                f"⏰ *Deadline:* {due_date_str}\n"
                f"⚡ *Priority:* {prio_label}\n\n"
                f"💡 _View your updated schedule anytime with `/schedule` or check pending tasks with `/urgent`._"
            )
            return

        # Step 1: Waiting for task title
        if user_state and user_state.get("step") == "waiting_task_title":
            task_title = text.strip()
            detected_course = "General"
            for c in db.get_all_courses(term="26S1"):
                if c["course_code"].lower() in task_title.lower():
                    detected_course = c["course_code"]
                    break

            self.user_states[chat_id] = {
                "step": "waiting_task_deadline",
                "task_title": task_title,
                "course_code": detected_course
            }

            self.send_message(
                chat_id,
                f"⏰ Got it: *{task_title}*\n\n"
                f"What is the deadline for this task?\n"
                f"_(e.g. `Tomorrow 5pm`, `Friday 23:59`, `2026-09-18`, or reply `none` / `skip`)_\n\n"
                f"💡 _Reply with the deadline, or send `/cancel` to abort._"
            )
            return

        # /addtask Trigger
        if text.startswith("/addtask"):
            remainder = text.replace("/addtask", "").strip()
            if remainder:
                # Title provided directly in command: /addtask Finish Lab Report
                task_title = remainder
                detected_course = "General"
                for c in db.get_all_courses(term="26S1"):
                    if c["course_code"].lower() in task_title.lower():
                        detected_course = c["course_code"]
                        break

                self.user_states[chat_id] = {
                    "step": "waiting_task_deadline",
                    "task_title": task_title,
                    "course_code": detected_course
                }

                self.send_message(
                    chat_id,
                    f"⏰ Got it: *{task_title}*\n\n"
                    f"What is the deadline for this task?\n"
                    f"_(e.g. `Tomorrow 5pm`, `Friday 23:59`, `2026-09-18`, or reply `none` / `skip`)_\n\n"
                    f"💡 _Reply with the deadline, or send `/cancel` to abort._"
                )
            else:
                # Bare /addtask command -> Ask for title first
                self.user_states[chat_id] = {
                    "step": "waiting_task_title"
                }

                self.send_message(
                    chat_id,
                    f"📝 *Create a New Action Task*\n\n"
                    f"What is the title or description of your task?\n"
                    f"_(e.g. `Finish MH2500 Tutorial 2` or `Prepare SolidWorks Lab Report`)_\n\n"
                    f"💡 _Reply with the task title, or send `/cancel` to abort._"
                )
            return

        # Standard Command Dispatch
        if text.startswith("/start") or text.startswith("/help"):
            is_admin = (user_id == db.PRIMARY_USER_ID)
            role_tag = "👑 Primary Workspace" if is_admin else f"👤 Private Workspace ({user_id})"

            welcome_msg = (
                f"👋 Hello {user_name}! Welcome to *CogniFlow OS* on Telegram.\n"
                f"_{role_tag}_\n\n"
                "I am your Multi-Agent Academic Orchestrator. You have your own dedicated, private workspace for all your courses, timetables, and documents.\n\n"
                "*Quick Setup:*\n"
                "• **Send your Timetable PDF** here to auto-import your class schedule & modules!\n"
                "• **Send any Tutorial / Lecture PDF** to index and analyze it with AI.\n\n"
                "*Commands:*\n"
                "• /addtask - Add a task (interactive prompt for title & deadline)\n"
                "• /schedule - Today's time-blocked schedule\n"
                "• /urgent - Upcoming deadlines & urgent notices\n"
                "• /courses - View your registered modules\n"
                "• /addcourse <CODE> <Title> - Add a course manually\n"
                "• /reset - Clear conversation history\n\n"
                "💬 Or ask anything: 'When is my next class?', 'Plan a 3-hour study session for tonight.'"
            )
            self.send_message(chat_id, welcome_msg)
            return

        if text.startswith("/whoami") or text.startswith("/myid"):
            self.send_message(chat_id, f"🆔 *Your Telegram ID:* `{user_id}`\nWorkspace: `data/users/{user_id}/`")
            return

        if text.startswith("/addcourse"):
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                self.send_message(chat_id, "Usage: `/addcourse <COURSE_CODE> [Course Title]`\nExample: `/addcourse SC2001 Algorithm Design`")
                return
            code = parts[1].upper()
            title = parts[2] if len(parts) > 2 else code
            db.upsert_course(f"course_{code}_{user_id}", code, title, term="26S1")
            self.send_message(chat_id, f"✅ Added course module *{code}*: {title}")
            return

        if text.startswith("/courses") or text.startswith("/modules"):
            self.send_message(chat_id, self.format_courses_list())
            return

        if text.startswith("/schedule") or text.startswith("/today"):
            self.send_message(chat_id, self.format_schedule())
            return

        if text.startswith("/urgent") or text.startswith("/deadlines"):
            self.send_message(chat_id, self.format_urgent_deadlines())
            return

        if text.startswith("/reset") or text.startswith("/clear"):
            session_id = f"tg_{chat_id}"
            with db.get_connection() as conn:
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
                conn.commit()
            self.send_message(chat_id, "🔄 Conversation session reset! What would you like to focus on now?", parse_mode=None)
            return

        # Natural Language Academic Query via Multi-Agent Cascade
        self.send_chat_action(chat_id, "typing")
        session_id = f"tg_{chat_id}"

        try:
            res = execute_chat_query(
                user_query=text,
                term="26S1",
                session_id=session_id
            )

            raw_reply = res.get("reply", "No response generated.")

            # 1. Parse out VOICE_SUMMARY
            voice_summary = ""
            voice_match = re.search(r'\[VOICE_SUMMARY:\s*([^\]]+)\]', raw_reply)
            if voice_match:
                voice_summary = voice_match.group(1).strip()
                raw_reply = raw_reply.replace(voice_match.group(0), "").strip()

            # 2. Extract [OPEN_DOC:<mat_id>:<title>] and collect file attachments
            doc_matches = re.findall(r'\[OPEN_DOC:([^:\]]+):([^\]]+)\]', raw_reply)
            clean_reply = re.sub(r'\[OPEN_DOC:([^:\]]+):([^\]]+)\]', r'📄 *\2*', raw_reply)

            # 3. If there is a voice summary, highlight it as an executive takeaway
            if voice_summary:
                clean_reply += f"\n\n🎙️ *Executive Brief:*\n_{voice_summary}_"

            # Send text reply (with automatic fallback to plain text if Markdown parsing fails)
            self.send_message(chat_id, clean_reply)

            # 4. If actual documents are cited and exist locally, send the PDFs to Telegram!
            if doc_matches:
                for mat_id, doc_title in doc_matches:
                    mat = db.get_material_by_id(mat_id)
                    if mat and mat.get("local_path"):
                        local_file = Path(mat["local_path"])
                        if not local_file.is_absolute():
                            local_file = BASE_DIR / local_file

                        if local_file.exists():
                            self.send_chat_action(chat_id, "upload_document")
                            logger.info(f"Sending document {local_file.name} to Telegram user {chat_id}")
                            self.send_document(
                                chat_id=chat_id,
                                file_path=local_file,
                                caption=f"📄 {doc_title} ({mat.get('course_code', '')})"
                            )

        except Exception as e:
            logger.error(f"Error handling Telegram query '{text}': {e}", exc_info=True)
            self.send_message(
                chat_id,
                f"⚠️ Sorry, an error occurred while processing your request: {str(e)[:200]}",
                parse_mode=None
            )

    def poll_loop(self):
        self.is_running = True
        logger.info("CogniFlow Telegram Bot polling loop started.")

        while self.is_running:
            try:
                updates_res = self.api_call("getUpdates", {
                    "offset": self.last_update_id + 1,
                    "timeout": 20,
                    "allowed_updates": ["message"]
                }, timeout=30)

                if updates_res.get("ok"):
                    for update in updates_res.get("result", []):
                        self.last_update_id = update["update_id"]
                        if "message" in update:
                            self.process_message(update["message"])
                else:
                    time.sleep(2)

            except Exception as e:
                logger.warning(f"Polling loop transient exception: {e}")
                time.sleep(3)

    def start_polling(self, daemon: bool = True) -> threading.Thread:
        bot_info = self.get_me()
        if not bot_info:
            logger.error("Could not connect to Telegram. Please check TELEGRAM_BOT_TOKEN in .env")
            return None

        logger.info(f"Bot authenticated as @{bot_info.get('username')} ({bot_info.get('first_name')})")
        t = threading.Thread(target=self.poll_loop, daemon=daemon, name="TelegramBotThread")
        t.start()
        return t

    def stop_polling(self):
        self.is_running = False

telegram_bot_service = TelegramBot()
