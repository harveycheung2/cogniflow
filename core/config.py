import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=True)

# Clean up AWS_PROFILE to avoid botocore ProfileNotFound error if no local config exists
if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_PROFILE") == "default":
    os.environ.pop("AWS_PROFILE", None)

# Application metadata
APP_NAME = "CogniFlow OS"
APP_VERSION = "1.0.0"
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8765"))

# Storage paths
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "workday_os.db"
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
AUTH_FILE = BASE_DIR / "auth.json"

# AWS / Bedrock Config
AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN", "")

LEAD_MODEL_ID = os.getenv("LEAD_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
SPECIALIST_MODEL_ID = os.getenv("SPECIALIST_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

# NTULearn Config
NTULEARN_BASE_URL = os.getenv("NTULEARN_BASE_URL", "https://ntulearn.ntu.edu.sg").rstrip("/")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Freemium LLM Config (Groq Primary with Google Gemini Fallback)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
