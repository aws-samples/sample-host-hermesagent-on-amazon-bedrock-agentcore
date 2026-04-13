"""Cron Lambda — EventBridge Scheduler → AgentCore invocation.

Receives scheduled events from EventBridge and dispatches ``cron`` actions to
the user's AgentCore container.  The container's hermes-agent executes the
prompt and returns the result; this Lambda can optionally deliver the output
to a channel (Telegram, Slack, etc.).

Environment variables:
    AGENTCORE_RUNTIME_ARN  — AgentCore runtime ARN
    AGENTCORE_QUALIFIER    — Runtime qualifier / endpoint
    IDENTITY_TABLE         — DynamoDB table for user lookups
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "")

_agentcore_client: Any = None


def _agentcore() -> Any:
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client("bedrock-agentcore")
    return _agentcore_client


def handler(event: dict, context: Any) -> dict:
    """EventBridge Scheduler handler.

    Expected event format (set in EventBridge rule input):
    {
        "jobId": "daily_summary",
        "userId": "user_abc123",
        "prompt": "Summarize today's AI news",
        "delivery": {
            "channel": "telegram",
            "chatId": "123456789"
        }
    }
    """
    logger.info("Cron event: %s", json.dumps(event))

    job_id = event.get("jobId", f"cron_{int(time.time())}")
    user_id = event.get("userId", "")
    prompt = event.get("prompt", "")
    delivery = event.get("delivery", {})

    if not user_id or not prompt:
        logger.error("Missing userId or prompt in cron event")
        return {"status": "error", "reason": "missing userId or prompt"}

    # Build session ID.
    session_id = f"{user_id}:cron:{job_id}"
    if len(session_id) < 33:
        session_id = session_id + ":" + "0" * (33 - len(session_id) - 1)

    # Invoke AgentCore with cron action.
    payload = {
        "action": "cron",
        "userId": user_id,
        "actorId": f"cron:{job_id}",
        "channel": "cron",
        "message": prompt,
        "jobId": job_id,
        "config": {
            "prompt": prompt,
            "delivery": delivery,
        },
    }

    try:
        response = _agentcore().invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            qualifier=QUALIFIER,
            runtimeSessionId=session_id,
            runtimeUserId=f"cron:{user_id}",
            payload=json.dumps(payload),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["payload"].read())
        agent_response = result.get("response", "")
    except Exception as exc:
        logger.exception("AgentCore cron invocation failed")
        agent_response = f"Cron job {job_id} failed: {exc}"

    # Deliver result to the configured channel.
    if delivery and agent_response:
        _deliver(delivery, agent_response, job_id)

    return {
        "status": "ok",
        "jobId": job_id,
        "responseLength": len(agent_response),
    }


def _deliver(delivery: dict, text: str, job_id: str) -> None:
    """Send the cron output to the specified channel."""
    channel = delivery.get("channel", "")
    chat_id = delivery.get("chatId", "")

    if channel == "telegram" and chat_id:
        _send_telegram(chat_id, f"[Cron: {job_id}]\n\n{text}")
    elif channel == "slack" and chat_id:
        _send_slack(chat_id, f"*Cron: {job_id}*\n\n{text}")
    else:
        logger.info("No delivery channel configured — response logged only")


def _send_telegram(chat_id: str, text: str) -> None:
    token = _get_secret("telegram-bot-token")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Truncate to Telegram's limit.
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        logger.error("Telegram delivery failed: %s", exc)


def _send_slack(channel: str, text: str) -> None:
    token = _get_secret("slack-bot-token")
    url = "https://slack.com/api/chat.postMessage"
    data = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        logger.error("Slack delivery failed: %s", exc)


# ---- Secrets cache -------------------------------------------------------

_secrets_cache: dict[str, str] = {}


def _get_secret(name: str) -> str:
    if name in _secrets_cache:
        return _secrets_cache[name]
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=f"hermes/{name}")
    value = resp["SecretString"]
    _secrets_cache[name] = value
    return value
