"""Lightweight warm-up agent for fast cold-start response.

Handles user messages while the full hermes-agent is loading (~10-30 s).
Uses Bedrock ConverseStream directly via boto3 — no hermes dependencies.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3

logger = logging.getLogger("agentcore.warmup")

SYSTEM_PROMPT = """\
You are Hermes, a helpful AI assistant deployed on Amazon Bedrock AgentCore.

The full agent (with 40+ tools including terminal, browser, web search, file \
operations, code execution, and more) is still loading. While it warms up you \
can answer questions directly from your knowledge.

For requests that require tools (running code, browsing the web, managing \
files, etc.), let the user know you will be fully ready in a moment.

Keep responses concise and helpful."""


class WarmupAgent:
    """Minimal agent backed by a single Bedrock Converse call."""

    def __init__(self) -> None:
        self.bedrock = boto3.client("bedrock-runtime")
        self.model_id = os.environ.get(
            "WARMUP_MODEL_ID",
            os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6-v1"),
        )
        logger.info("WarmupAgent initialised (model=%s)", self.model_id)

    def handle(self, message: str, user_id: str) -> str:
        """Return a response for *message* using Bedrock Converse."""
        try:
            response = self._call_bedrock(message)
            return self._extract_text(response)
        except Exception as exc:
            logger.error("WarmupAgent.handle error: %s", exc)
            return (
                "I'm still starting up — please try again in a few seconds."
            )

    # ------------------------------------------------------------------

    def _call_bedrock(self, message: str) -> dict[str, Any]:
        return self.bedrock.converse(
            modelId=self.model_id,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": message}],
                },
            ],
            system=[{"text": SYSTEM_PROMPT}],
            inferenceConfig={
                "maxTokens": 2048,
                "temperature": 0.7,
            },
        )

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        try:
            blocks = response["output"]["message"]["content"]
            return "".join(b["text"] for b in blocks if "text" in b)
        except (KeyError, IndexError, TypeError):
            return ""
