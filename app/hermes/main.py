"""Hermes Agent on Amazon Bedrock AgentCore.

Uses the bedrock-agentcore SDK (BedrockAgentCoreApp) which handles the
/ping and /invocations HTTP contract automatically.

Architecture:
  - Hermes Agent's native Bedrock provider (provider="bedrock") routes
    all API calls through the Converse API with SigV4 authentication.
  - No monkey-patching needed — Hermes handles Bedrock natively.
  - Supports ALL Bedrock models (Claude, Nova, DeepSeek, Llama, etc.)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import traceback
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

logger = logging.getLogger("hermes.agentcore")
app = BedrockAgentCoreApp()
log = app.logger

# ---------------------------------------------------------------------------
# Cached agent singleton
# ---------------------------------------------------------------------------

_agent = None


def _get_region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def get_or_create_agent():
    """Lazy-init the full hermes-agent. Blocks on first call (~5-15s)."""
    global _agent
    if _agent is not None:
        return _agent

    log.info("Initializing hermes-agent (first request) ...")

    os.environ["HERMES_HEADLESS"] = "1"
    os.environ.setdefault("AGENTCORE_MODE", "1")

    region = _get_region()
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    os.environ.setdefault("AWS_REGION", region)

    from run_agent import AIAgent

    model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

    # Use Hermes's native Bedrock provider — Converse API with SigV4 auth.
    # No monkey-patching needed. Supports all Bedrock models.
    _agent = AIAgent(
        model=model,
        provider="bedrock",
        base_url=f"https://bedrock-runtime.{region}.amazonaws.com",
        api_key="aws-sdk",
        quiet_mode=True,
    )

    log.info("hermes-agent ready (model=%s, region=%s, provider=bedrock)", model, region)
    return _agent


# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------

def _sigterm_handler(signum: int, frame: Any) -> None:
    log.info("SIGTERM received -- shutting down")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

@app.entrypoint
async def invoke(payload, context):
    """Handle an AgentCore invocation."""
    prompt = payload.get("prompt", "")
    channel = payload.get("channel", "agentcore")
    message = payload.get("message", prompt)

    if not message or not message.strip():
        yield ""
        return

    try:
        agent = get_or_create_agent()

        system_extra = f"The user is contacting you via {channel}."
        if payload.get("chatId"):
            system_extra += f" Chat ID: {payload['chatId']}."

        result = agent.run_conversation(
            user_message=message,
            system_message=system_extra,
            conversation_history=None,
        )
        yield result.get("final_response", "")
    except Exception as exc:
        log.error("Agent error: %s\n%s", exc, traceback.format_exc())
        yield f"Sorry, an error occurred: {exc}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _sigterm_handler)
    app.run()
