"""Per-user STS scoped credentials for AgentCore.

Each user's container gets short-lived AWS credentials that restrict S3 access
to that user's namespace only.  Credentials are refreshed automatically every
45 minutes (well within the 1-hour STS maximum).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

import boto3

logger = logging.getLogger("agentcore.credentials")


class ScopedCredentials:
    """Manages STS session credentials scoped to one user's S3 namespace."""

    REFRESH_INTERVAL = 2700  # 45 minutes

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.bucket = os.environ["S3_BUCKET"]
        self.role_arn = os.environ["EXECUTION_ROLE_ARN"]
        self._sts: Any = boto3.client("sts")
        self._credentials: dict[str, str] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self) -> dict[str, str]:
        """Return current scoped credentials, refreshing if needed."""
        with self._lock:
            if self._credentials is None:
                self._refresh()
            return self._credentials  # type: ignore[return-value]

    def start_refresh_loop(self) -> None:
        """Background thread that refreshes credentials every 45 min."""

        def _loop() -> None:
            while not self._stop.is_set():
                self._stop.wait(self.REFRESH_INTERVAL)
                if self._stop.is_set():
                    break
                try:
                    with self._lock:
                        self._refresh()
                except Exception as exc:
                    logger.error("Credential refresh failed: %s", exc)

        t = threading.Thread(target=_loop, daemon=True, name="credential-refresh")
        t.start()
        logger.info("Credential refresh loop started (interval=%ds)", self.REFRESH_INTERVAL)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        session_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:ListBucket",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{self.bucket}/{self.namespace}/*",
                        f"arn:aws:s3:::{self.bucket}",
                    ],
                    "Condition": {
                        "StringLike": {
                            "s3:prefix": [f"{self.namespace}/*"],
                        },
                    },
                },
            ],
        }

        resp = self._sts.assume_role(
            RoleArn=self.role_arn,
            RoleSessionName=f"hermes-{self.namespace[:32]}",
            Policy=json.dumps(session_policy),
            DurationSeconds=3600,
        )
        creds = resp["Credentials"]
        self._credentials = {
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"],
        }
        logger.info("Scoped credentials refreshed for namespace=%s", self.namespace)
