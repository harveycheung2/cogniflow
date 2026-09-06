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
        """Reload credentials from AWS SSO profile (hackathon/hack2026) or .env."""
        if ENV_FILE.exists():
            load_dotenv(ENV_FILE, override=True)

        profile = os.getenv("AWS_PROFILE", "").strip()
        region = os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1"
        ak = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        sk = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
        st = os.getenv("AWS_SESSION_TOKEN", "").strip()

        # 1. Try SSO profiles (configured profile, hackathon, or hack2026)
        profiles_to_try = [p for p in [profile, "hackathon", "hack2026", "default"] if p and p != "none"]
        for prof in profiles_to_try:
            try:
                sess = boto3.Session(profile_name=prof, region_name=region)
                client = sess.client("bedrock-runtime", region_name=region)
                self._session = sess
                self._client = client
                self._last_token = prof
                return
            except Exception:
                continue

        # 2. Fall back to explicit access keys if provided
        if ak and sk:
            try:
                self._session = boto3.Session(
                    aws_access_key_id=ak,
                    aws_secret_access_key=sk,
                    aws_session_token=st or None,
                    region_name=region,
                )
                self._client = self._session.client("bedrock-runtime", region_name=region)
                self._last_token = st
                return
            except Exception:
                self._client = None
                return

        self._client = None

    def is_ready(self) -> bool:
        curr_st = os.getenv("AWS_SESSION_TOKEN", "").strip()
        curr_prof = os.getenv("AWS_PROFILE", "").strip()
        if curr_st != self._last_token and curr_prof != self._last_token:
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
                "region": region,
            }
        except Exception as e:
            return {"ready": False, "error": f"STS Authentication failed: {e}"}

bedrock_client = BedrockClient()
