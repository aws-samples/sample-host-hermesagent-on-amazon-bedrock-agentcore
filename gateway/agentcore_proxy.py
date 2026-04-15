"""AgentCoreProxyAgent — drop-in replacement for AIAgent.

Monkey-patches ``run_agent.AIAgent`` so that the hermes-agent gateway
forwards every ``run_conversation()`` call to the AgentCore Runtime via
``invoke_agent_runtime()``.  The gateway handles only platform protocols
(WeChat long-poll, Feishu WebSocket, etc.); all AI inference runs inside
AgentCore Firecracker microVMs.

Usage:
    from agentcore_proxy import patch_aiagent
    patch_aiagent()
    # Then start the gateway normally — AIAgent is now the proxy.
"""

from __future__ import annotations

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
logger = logging.getLogger("agentcore_proxy")


class AgentCoreProxyAgent:
    """Drop-in replacement for AIAgent — forwards to AgentCore."""

    def __init__(self, *, session_id="", platform="", user_id="", **kwargs):
        # Ignore AIAgent's constructor params (model, tools, api_key, etc.)
        # — those are handled by the real AIAgent inside AgentCore.
        self.session_id = session_id or ""
        self.platform = platform or ""
        self.user_id = user_id or ""

        # AgentCore session_id must be >= 33 characters.
        self._ac_session_id = f"{self.platform}__{self.user_id}__{self.session_id}"
        if len(self._ac_session_id) < 33:
            self._ac_session_id += ":" + "0" * (33 - len(self._ac_session_id) - 1)
        self._ac_session_id = self._ac_session_id[:128]
        self._ac_user_id = f"{self.platform}:{self.user_id}" if self.user_id else ""

    def run_conversation(
        self,
        user_message,
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
    ):
        """Call invoke_agent_runtime() and return an AIAgent-compatible result."""
        payload = json.dumps({
            "action": "chat",
            "message": user_message,
            "userId": self._ac_user_id,
            "channel": self.platform,
        })

        kwargs = {
            "agentRuntimeArn": RUNTIME_ARN,
            "runtimeSessionId": self._ac_session_id,
            "payload": payload,
        }
        if self._ac_user_id:
            kwargs["runtimeUserId"] = self._ac_user_id

        text = self._invoke_with_retry(**kwargs)

        return {
            "completed": True,
            "final_response": text,
            "messages": [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": text},
            ],
            "api_calls": 1,
        }

    def _invoke_with_retry(self, max_retries=2, **kwargs):
        for attempt in range(max_retries + 1):
            try:
                response = _client.invoke_agent_runtime(**kwargs)
                return _parse_response(response)
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("ThrottlingException", "ServiceUnavailableException") and attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning("AgentCore %s (attempt %d/%d), retrying in %ds", code, attempt + 1, max_retries + 1, wait)
                    time.sleep(wait)
                    continue
                logger.error("AgentCore invocation failed: %s", e)
                return "Sorry, the service is temporarily busy. Please try again."
        return "Sorry, the service is temporarily busy. Please try again."

    # Attributes the gateway may read on the agent instance — safe defaults.
    context_compressor = None
    session_prompt_tokens = 0
    session_completion_tokens = 0
    model = "agentcore-proxy"
    tools = []

    # Callback attributes set per-message by the gateway — accept silently.
    tool_progress_callback = None
    step_callback = None
    stream_delta_callback = None
    interim_assistant_callback = None
    status_callback = None
    background_review_callback = None
    reasoning_config = None
    service_tier = None
    request_overrides = None
    _print_fn = None


def _parse_response(response):
    """Parse AgentCore invoke response.

    The response format varies by SDK version:
    - Streaming: ``response["body"]`` is an iterable of event dicts
    - Non-streaming: ``response["response"]`` is bytes or str
    """
    # Try streaming body first (EventStream)
    body = response.get("body")
    if body and hasattr(body, "__iter__") and not isinstance(body, (str, bytes)):
        chunks = []
        try:
            for event in body:
                chunk = event.get("chunk", {}).get("bytes", b"")
                if chunk:
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8")
                    chunks.append(chunk)
        except Exception as e:
            logger.warning("Error reading SSE stream: %s", e)
        if chunks:
            return "".join(chunks)

    # Fallback: non-streaming response
    result = response.get("response", "")
    if hasattr(result, "read"):
        result = result.read()
    if isinstance(result, bytes):
        result = result.decode("utf-8")
    result = result.strip()

    # SSE format: data: "..."
    if result.startswith("data: "):
        result = result[6:]
    if result.startswith('"') and result.endswith('"'):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            pass
    return result or "(no response)"


def _dummy_resolve_runtime_agent_kwargs() -> dict:
    """Return dummy provider kwargs so the gateway's pre-flight check passes.

    The gateway calls ``_resolve_runtime_agent_kwargs()`` before creating
    AIAgent to resolve LLM provider credentials.  In proxy mode we don't
    need real credentials — all LLM calls go through AgentCore.
    """
    return {
        "api_key": "agentcore-proxy-not-used",
        "base_url": None,
        "provider": "openai",
        "api_mode": None,
        "command": None,
        "args": [],
        "credential_pool": None,
    }


def patch_aiagent():
    """Monkey-patch run_agent.AIAgent → AgentCoreProxyAgent.

    Must be called before the gateway imports AIAgent.  Since the gateway
    uses ``from run_agent import AIAgent`` (lazy import at call-site), this
    patches the module attribute so all subsequent imports pick up the proxy.

    Also patches the gateway's provider resolution to skip real credential
    lookup — the proxy forwards everything to AgentCore.
    """
    if not RUNTIME_ARN:
        raise RuntimeError(
            "AGENTCORE_RUNTIME_ARN environment variable is required. "
            "Set it to the AgentCore Runtime ARN from Phase 2 deployment."
        )

    import run_agent
    run_agent.AIAgent = AgentCoreProxyAgent

    # Patch the gateway's provider resolution — no real API key needed.
    import gateway.run as gw_run
    gw_run._resolve_runtime_agent_kwargs = _dummy_resolve_runtime_agent_kwargs

    logger.info(
        "Patched run_agent.AIAgent → AgentCoreProxyAgent (runtime=%s)",
        RUNTIME_ARN,
    )
