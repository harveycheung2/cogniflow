import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# Safe .env loader that doesn't strictly depend on python-dotenv
def load_env_file(filepath):
    if not filepath.exists():
        return
    for line in filepath.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'\"")
        if k and k not in os.environ:
            os.environ[k] = v

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE, override=True)
except ImportError:
    load_env_file(ENV_FILE)

# Ensure virtualenv site-packages are added to sys.path if running under global python
venv_site = BASE_DIR / ".venv" / "Lib" / "site-packages"
if venv_site.exists() and str(venv_site) not in sys.path:
    sys.path.insert(0, str(venv_site))

try:
    from openai import OpenAI
except ImportError:
    print("Error: openai package not found.")
    print("Run with the virtual environment Python:")
    print(r"& 'c:\Users\harve\agentic_ai_hackathon_2026\agentic_system\.venv\Scripts\python.exe' test_groq_gemini.py")
    sys.exit(1)

groq_key = os.getenv("GROQ_API_KEY", "").strip()
groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()

gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()

print("=" * 65)
print(" CogniFlow OS — Groq & Gemini API Live Diagnostic Test")
print("=" * 65)
print(f"Config File: {ENV_FILE}")
print(f"Python Exec: {sys.executable}")
print()

# ---------------------------------------------------------------------
# TEST 1: GROQ CLOUD API
# ---------------------------------------------------------------------
print("[1/3] Testing Groq Cloud API...")
if not groq_key:
    print("  [!] GROQ_API_KEY is empty in .env")
    print("      Get your 100% free key at: https://console.groq.com")
    print("      Paste it in .env on Line 41: GROQ_API_KEY=gsk_...")
    print()
else:
    masked = f"{groq_key[:6]}...{groq_key[-4:]}" if len(groq_key) > 10 else "***"
    print(f"  Found Key: {masked} | Model: {groq_model}")
    try:
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key, timeout=20.0)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=groq_model,
            messages=[
                {"role": "system", "content": "You are a test assistant. Answer in 1 concise sentence."},
                {"role": "user", "content": "Test ping from CogniFlow OS. State your model name."}
            ],
            max_tokens=60,
            temperature=0.1
        )
        latency = (time.time() - t0) * 1000
        reply = resp.choices[0].message.content.strip() if resp.choices else "No reply"
        print(f"  [SUCCESS] Connected to Groq in {latency:.0f}ms!")
        print(f"  Response: {reply}")
    except Exception as e:
        print(f"  [FAILED] Groq error: {e}")
    print()

# ---------------------------------------------------------------------
# TEST 2: GOOGLE GEMINI API
# ---------------------------------------------------------------------
print("[2/3] Testing Google Gemini API...")
if not gemini_key:
    print("  [!] GEMINI_API_KEY is empty in .env")
    print("      Get your 100% free key at: https://aistudio.google.com")
    print("      Paste it in .env on Line 43: GEMINI_API_KEY=AIza...")
    print()
else:
    masked = f"{gemini_key[:6]}...{gemini_key[-4:]}" if len(gemini_key) > 10 else "***"
    print(f"  Found Key: {masked} | Model: {gemini_model}")
    try:
        client = OpenAI(base_url="https://generativelanguage.googleapis.com/v1beta/openai/", api_key=gemini_key, timeout=25.0)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=gemini_model,
            messages=[
                {"role": "system", "content": "You are a test assistant. Answer in 1 concise sentence."},
                {"role": "user", "content": "Test ping from CogniFlow OS. State your model name."}
            ],
            max_tokens=60,
            temperature=0.1
        )
        latency = (time.time() - t0) * 1000
        reply = resp.choices[0].message.content.strip() if resp.choices else "No reply"
        print(f"  [SUCCESS] Connected to Google Gemini in {latency:.0f}ms!")
        print(f"  Response: {reply}")
    except Exception as e:
        print(f"  [FAILED] Gemini error: {e}")
    print()

# ---------------------------------------------------------------------
# TEST 3: COGNIFLOW LLM PROVIDER AUTO-CASCADE
# ---------------------------------------------------------------------
print("[3/3] Testing Integrated LLMProvider Failover Cascade...")
try:
    sys.path.insert(0, str(BASE_DIR))
    from services.llm_provider import llm_provider
    status = llm_provider.get_status()
    print(f"  Provider Status: {status.get('primary_provider')}")
    print(f"  Fallback Chain:  {status.get('fallback_chain')}")
    print(f"  Ready for Chat:  {status.get('ready')}")

    res_msg, provider_used = llm_provider.chat_completion(
        messages=[{"role": "user", "content": "Say hello from the multi-agent engine in 6 words."}],
        max_tokens=40
    )
    if res_msg and res_msg.content:
        clean_text = res_msg.content.strip()
        print(f"  Active Engine Used: {provider_used}")
        print(f"  Output: {clean_text}")
    else:
        print(f"  Active Engine Used: {provider_used} (Local Specialist Rules)")
except Exception as e:
    print(f"  Cascade check error: {e}")

print()
print("=" * 65)
