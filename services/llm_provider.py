import os
import time
import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dotenv import load_dotenv
from core import database as db

logger = logging.getLogger("cogniflow.llm")

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=False)

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None

from services.aws_bedrock import bedrock_client
from core.config import LEAD_MODEL_ID


class LLMProvider:
    """
    Unified Cascading LLM Provider with strict priority order:
    1. Priority 1: AWS Bedrock (Claude 3.5 / 4.5 Sonnet)
    2. Priority 2: Groq Cloud (qwen/qwen3.8-27b or llama-3.3-70b-versatile)
    3. Priority 3: Google Gemini (gemini-3.6-flash)
    4. Priority 4: Local Offline Specialist Search Engine (Deterministic SQLite)
    """
    def __init__(self):
        self._groq_client = None
        self._gemini_client = None
        self.reload_clients()

    def reload_clients(self):
        if not OPENAI_AVAILABLE:
            return

        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

        if groq_key:
            try:
                self._groq_client = OpenAI(
                    base_url="https://api.groq.com/openai/v1",
                    api_key=groq_key,
                    timeout=30.0,
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Groq client: {e}")
                self._groq_client = None
        else:
            self._groq_client = None

        if gemini_key:
            try:
                self._gemini_client = OpenAI(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    api_key=gemini_key,
                    timeout=45.0,
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Gemini client: {e}")
                self._gemini_client = None
        else:
            self._gemini_client = None

    def is_ready(self) -> bool:
        self.reload_clients()
        return bedrock_client.is_ready() or self._groq_client is not None or self._gemini_client is not None

    def get_status(self) -> Dict[str, Any]:
        self.reload_clients()
        bedrock_ready = bedrock_client.is_ready()
        groq_ready = self._groq_client is not None
        gemini_ready = self._gemini_client is not None

        primary = "Offline Hybrid (Local DB)"
        if bedrock_ready:
            primary = "AWS Bedrock (Claude 3.5 Sonnet)"
        elif groq_ready:
            primary = f"Groq ({os.getenv('GROQ_MODEL', 'qwen/qwen3.8-27b')})"
        elif gemini_ready:
            primary = f"Gemini ({os.getenv('GEMINI_MODEL', 'gemini-3.6-flash')})"

        return {
            "ready": bedrock_ready or groq_ready or gemini_ready,
            "primary_provider": primary,
            "bedrock_active": bedrock_ready,
            "groq_active": groq_ready,
            "gemini_active": gemini_ready,
            "bedrock_model": LEAD_MODEL_ID,
            "groq_model": os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
            "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
            "fallback_chain": "AWS Bedrock (Claude) -> Groq (Qwen/Llama) -> Gemini (Flash) -> Local Failsafe",
        }

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> Tuple[Optional[Any], str]:
        """
        Executes chat completion with automated priority cascade:
        1. Priority 1: AWS Bedrock Claude 3.5 Sonnet
        2. Priority 2: Groq Cloud (Qwen / Llama)
        3. Priority 3: Google Gemini (Flash)
        4. Priority 4: Local Specialist Engine
        """
        self.reload_clients()

        # -------------------------------------------------------------
        # PRIORITY 1: AWS Bedrock Claude 3.5 Sonnet
        # -------------------------------------------------------------
        if bedrock_client.is_ready() and not tools:
            t0 = time.time()
            try:
                bedrock_msgs = []
                for m in messages:
                    c = m.get("content", "")
                    if isinstance(c, list):
                        c_text = " ".join([b.get("text", "") for b in c if isinstance(b, dict) and "text" in b])
                    else:
                        c_text = str(c)
                    r = m.get("role", "user")
                    if r not in ["user", "assistant"]:
                        r = "user"
                    if c_text.strip():
                        bedrock_msgs.append({"role": r, "content": [{"text": c_text.strip()}]})

                if not bedrock_msgs:
                    bedrock_msgs = [{"role": "user", "content": [{"text": "Hello"}]}]

                converse_kwargs = {
                    "modelId": LEAD_MODEL_ID,
                    "messages": bedrock_msgs,
                    "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature}
                }
                if system_prompt:
                    converse_kwargs["system"] = [{"text": system_prompt}]

                resp = bedrock_client._client.converse(**converse_kwargs)
                latency_ms = (time.time() - t0) * 1000

                output_msg = resp.get("output", {}).get("message", {})
                content_blocks = output_msg.get("content", [])
                text_content = "".join([b.get("text", "") for b in content_blocks if "text" in b])

                usage = resp.get("usage", {})
                pt = usage.get("inputTokens", 0)
                ct = usage.get("outputTokens", 0)
                tt = usage.get("totalTokens", pt + ct)

                class DummyMsg:
                    def __init__(self, content):
                        self.content = content
                        self.tool_calls = None

                db.record_token_usage("AWS Bedrock", LEAD_MODEL_ID, pt, ct, tt, latency_ms, "success", "chat")
                return DummyMsg(text_content), "AWS Bedrock (Claude 3.5)"
            except Exception as e:
                err_str = str(e)
                latency_ms = (time.time() - t0) * 1000
                try:
                    db.record_token_usage("AWS Bedrock", LEAD_MODEL_ID, 0, 0, 0, latency_ms, "fallback", "chat", err_str[:200])
                except Exception:
                    pass
                print(f"[LLMProvider] Bedrock notice ({err_str[:120]}). Auto-falling back to Groq...")

        formatted_messages = []
        if system_prompt:
            formatted_messages.append({"role": "system", "content": system_prompt})
        formatted_messages.extend(messages)

        # -------------------------------------------------------------
        # PRIORITY 2: Groq Cloud
        # -------------------------------------------------------------
        if self._groq_client:
            groq_model = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
            t0 = time.time()
            try:
                kwargs = {
                    "model": groq_model,
                    "messages": formatted_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                resp = self._groq_client.chat.completions.create(**kwargs)
                latency_ms = (time.time() - t0) * 1000
                if resp.choices and len(resp.choices) > 0:
                    pt = getattr(resp.usage, "prompt_tokens", 0) if hasattr(resp, "usage") and resp.usage else 0
                    ct = getattr(resp.usage, "completion_tokens", 0) if hasattr(resp, "usage") and resp.usage else 0
                    tt = getattr(resp.usage, "total_tokens", 0) if hasattr(resp, "usage") and resp.usage else (pt + ct)
                    try:
                        db.record_token_usage("Groq", groq_model, pt, ct, tt, latency_ms, "success", "chat")
                    except Exception as e_db:
                        logger.warning(f"Could not log token usage: {e_db}")
                    return resp.choices[0].message, f"Groq ({groq_model})"
            except Exception as e:
                err_str = str(e)
                latency_ms = (time.time() - t0) * 1000
                try:
                    db.record_token_usage("Groq", groq_model, 0, 0, 0, latency_ms, "fallback", "chat", err_str[:200])
                except Exception:
                    pass
                print(f"[LLMProvider] Groq notice ({err_str[:120]}). Auto-falling back to Gemini...")

        # -------------------------------------------------------------
        # PRIORITY 3: Google Gemini
        # -------------------------------------------------------------
        if self._gemini_client:
            gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
            t0 = time.time()
            try:
                kwargs = {
                    "model": gemini_model,
                    "messages": formatted_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                resp = self._gemini_client.chat.completions.create(**kwargs)
                latency_ms = (time.time() - t0) * 1000
                if resp.choices and len(resp.choices) > 0:
                    pt = getattr(resp.usage, "prompt_tokens", 0) if hasattr(resp, "usage") and resp.usage else 0
                    ct = getattr(resp.usage, "completion_tokens", 0) if hasattr(resp, "usage") and resp.usage else 0
                    tt = getattr(resp.usage, "total_tokens", 0) if hasattr(resp, "usage") and resp.usage else (pt + ct)
                    try:
                        db.record_token_usage("Google Gemini", gemini_model, pt, ct, tt, latency_ms, "success", "chat")
                    except Exception as e_db:
                        logger.warning(f"Could not log token usage: {e_db}")
                    return resp.choices[0].message, f"Google Gemini ({gemini_model})"
            except Exception as e:
                err_str = str(e)
                latency_ms = (time.time() - t0) * 1000
                try:
                    db.record_token_usage("Google Gemini", gemini_model, 0, 0, 0, latency_ms, "fallback", "chat", err_str[:200])
                except Exception:
                    pass
                print(f"[LLMProvider] Gemini notice ({err_str[:120]}). Auto-falling back to Local Engine...")

        # -------------------------------------------------------------
        # PRIORITY 4: Local Specialist Engine
        # -------------------------------------------------------------
        try:
            db.record_token_usage("Local Engine", "Rule & SQLite Specialist", 0, 0, 0, 5.0, "success", "chat")
        except Exception:
            pass
        return None, "offline_hybrid"

    def analyze_document(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 1500,
        temperature: float = 0.1,
    ) -> Tuple[Optional[str], str]:
        """Analyze document text through cascade: Bedrock -> Groq -> Gemini."""
        msg = [{"role": "user", "content": prompt}]
        res_msg, provider = self.chat_completion(
            messages=msg,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if res_msg and hasattr(res_msg, "content") and res_msg.content:
            return res_msg.content, provider
        return None, provider


llm_provider = LLMProvider()
