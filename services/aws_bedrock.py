import os
import boto3
from typing import List, Dict, Any, Optional
from core.config import (
    AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
    AWS_SESSION_TOKEN, LEAD_MODEL_ID, SPECIALIST_MODEL_ID
)

class BedrockClient:
    def __init__(self):
        self._session = None
        self._client = None
        self._credentials_expired = False
        self._init_client()

    def _init_client(self):
        if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
            return

        try:
            # Ensure AWS_PROFILE doesn't conflict
            if os.environ.get("AWS_PROFILE") == "default":
                os.environ.pop("AWS_PROFILE", None)

            self._session = boto3.Session(
                aws_access_key_id=AWS_ACCESS_KEY_ID,
                aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                aws_session_token=AWS_SESSION_TOKEN or None,
                region_name=AWS_REGION,
            )
            self._client = self._session.client("bedrock-runtime", region_name=AWS_REGION)
        except Exception as e:
            self._client = None

    def is_ready(self) -> bool:
        return self._client is not None and not self._credentials_expired

    def get_account_identity(self) -> Dict[str, Any]:
        if not self._session:
            return {"ready": False, "error": "No AWS credentials configured."}
        try:
            sts = self._session.client("sts", region_name=AWS_REGION)
            identity = sts.get_caller_identity()
            self._credentials_expired = False
            return {
                "ready": True,
                "arn": identity.get("Arn", ""),
                "region": AWS_REGION,
            }
        except Exception as e:
            err_str = str(e)
            if "ExpiredToken" in err_str:
                self._credentials_expired = True
            return {"ready": False, "error": err_str}

    def converse(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model_id: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.2,
    ) -> str:
        """Call AWS Bedrock Converse API with structured messages."""
        if not self._client:
            self._init_client()

        if not self._client:
            return "⚠️ AWS Bedrock is not configured. Please check your AWS credentials in .env."

        selected_model = model_id or LEAD_MODEL_ID

        formatted_messages = []
        for m in messages:
            content = m.get("content", "")
            role = m.get("role", "user")
            if isinstance(content, str):
                formatted_messages.append({"role": role, "content": [{"text": content}]})
            elif isinstance(content, list):
                formatted_messages.append({"role": role, "content": content})

        kwargs: Dict[str, Any] = {
            "modelId": selected_model,
            "messages": formatted_messages,
            "inferenceConfig": {
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        }

        if system_prompt:
            kwargs["system"] = [{"text": system_prompt}]

        try:
            response = self._client.converse(**kwargs)
            output = response.get("output", {}).get("message", {}).get("content", [])
            if output and "text" in output[0]:
                return output[0]["text"]
            return "No text response received from model."
        except Exception as e:
            # Fallback to Haiku if Sonnet has throttle or quota issue
            if selected_model != SPECIALIST_MODEL_ID:
                try:
                    kwargs["modelId"] = SPECIALIST_MODEL_ID
                    response = self._client.converse(**kwargs)
                    output = response.get("output", {}).get("message", {}).get("content", [])
                    if output and "text" in output[0]:
                        return output[0]["text"]
                except Exception as fallback_err:
                    return f"Error invoking Bedrock models: {e}; Fallback: {fallback_err}"
            return f"Error invoking Bedrock ({selected_model}): {e}"

bedrock_client = BedrockClient()
