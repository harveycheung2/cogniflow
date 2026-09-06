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
        self.reload_client()

    def reload_client(self):
        """Reload credentials from .env in case user updated session token."""
        if ENV_FILE.exists():
            load_dotenv(ENV_FILE, override=True)

        ak = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        sk = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
        st = os.getenv("AWS_SESSION_TOKEN", "").strip()
        region = os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1"

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
        except Exception:
            self._client = None

    def is_ready(self) -> bool:
        # Check if .env changed
        curr_st = os.getenv("AWS_SESSION_TOKEN", "").strip()
        if curr_st != self._last_token:
            self.reload_client()
        return self._client is not None

    def get_account_identity(self) -> Dict[str, Any]:
        self.reload_client()
        if not self._session:
            return {"ready": False, "error": "No AWS credentials configured."}
        try:
            region = os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1"
            sts = self._session.client("sts", region_name=region)
            identity = sts.get_caller_identity()
            return {
                "ready": True,
                "arn": identity.get("Arn", "Unknown ARN"),
                "account_id": identity.get("Account", "Unknown Account"),
                "user_id": identity.get("UserId", "Unknown User"),
            }
        except Exception as e:
            return {"ready": False, "error": f"STS Authentication failed: {str(e)}"}

    def converse(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_id: str = "anthropic.claude-3-5-sonnet-20240620-v1:0",
        max_tokens: int = 1500,
        temperature: float = 0.2
    ) -> str:
        if not self.is_ready():
            raise RuntimeError("Bedrock Client not configured.")

        system_config = [{"text": system_prompt}] if system_prompt else []
        formatted_messages = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                formatted_messages.append({"role": m["role"], "content": [{"text": content}]})
            else:
                formatted_messages.append(m)

        try:
            response = self._client.converse(
                modelId=model_id,
                messages=formatted_messages,
                system=system_config,
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature}
            )
            output = response.get("output", {}).get("message", {}).get("content", [])
            for block in output:
                if "text" in block:
                    return block["text"]
            return ""
        except Exception as e:
            if "ExpiredToken" in str(e):
                self.reload_client()
            raise e

bedrock_client = BedrockClient()
