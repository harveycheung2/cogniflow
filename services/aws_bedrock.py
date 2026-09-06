import os
from pathlib import Path
import boto3
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

class BedrockClient:
    def __init__(self):
        self._session = None
        self._client = None
        self._last_token = None
        self._credentials_expired = False
        self.reload_client()

    def reload_client(self):
        """Reload credentials from AWS SSO profile or .env in case user updated session token."""
        if ENV_FILE.exists():
            load_dotenv(ENV_FILE, override=True)

        profile = os.getenv("AWS_PROFILE", "hack2026").strip()
        region = os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1"
        ak = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        sk = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
        st = os.getenv("AWS_SESSION_TOKEN", "").strip()

        # 1. Check AWS SSO Profile first (e.g. hack2026)
        if profile and profile != "default":
            try:
                sess = boto3.Session(profile_name=profile, region_name=region)
                sts = sess.client("sts", region_name=region)
                sts.get_caller_identity()
                self._session = sess
                self._client = self._session.client("bedrock-runtime", region_name=region)
                self._credentials_expired = False
                return
            except Exception:
                pass

        # 2. Fall back to static access keys from .env
        if not ak or not sk:
            self._client = None
            return

        try:
            if os.environ.get("AWS_PROFILE") == "default":
                os.environ.pop("AWS_PROFILE", None)

            self._session = boto3.Session(
                aws_access_key_id=ak,
                aws_secret_access_key=sk,
                aws_session_token=st or None,
                region_name=region,
            )
            self._client = self._session.client("bedrock-runtime", region_name=region)
            self._last_token = st
            self._credentials_expired = False
        except Exception:
            self._client = None

    def is_ready(self) -> bool:
        # Check if .env changed
        curr_st = os.getenv("AWS_SESSION_TOKEN", "").strip()
        if curr_st != self._last_token:
            self.reload_client()
        return self._client is not None and not self._credentials_expired

    def get_account_identity(self) -> Dict[str, Any]:
        self.reload_client()
        if not self._session:
            return {"ready": False, "error": "No AWS credentials configured."}
        try:
            region = os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1"
            sts = self._session.client("sts", region_name=region)
            identity = sts.get_caller_identity()
            self._credentials_expired = False
            return {
                "ready": True,
                "arn": identity.get("Arn", "Unknown ARN"),
                "account_id": identity.get("Account", "Unknown Account"),
                "user_id": identity.get("UserId", "Unknown User"),
                "region": region,
            }
        except Exception as e:
            err_str = str(e)
            if "ExpiredToken" in err_str:
                self._credentials_expired = True
            return {"ready": False, "error": f"STS Authentication failed: {err_str}"}

    def converse(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_id: str = "anthropic.claude-3-5-sonnet-20240620-v1:0",
        max_tokens: int = 1500,
        temperature: float = 0.2
    ) -> str:
        if not self.is_ready():
            raise RuntimeError("Bedrock Client not configured or token expired.")

        system_config = [{"text": system_prompt}] if system_prompt else []
        formatted_messages = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                formatted_messages.append({
                    "role": m.get("role", "user"),
                    "content": [{"text": content}]
                })
            elif isinstance(content, list):
                formatted_messages.append({
                    "role": m.get("role", "user"),
                    "content": content
                })

        try:
            resp = self._client.converse(
                modelId=model_id,
                messages=formatted_messages,
                system=system_config,
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature
                }
            )
            return resp["output"]["message"]["content"][0]["text"]
        except Exception as e:
            err_str = str(e)
            if "ExpiredToken" in err_str:
                self._credentials_expired = True
            raise e

bedrock_client = BedrockClient()
