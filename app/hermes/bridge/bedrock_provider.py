"""Bedrock LLM provider configuration for hermes-agent.

.. deprecated::
    This module is **not currently used** by the runtime.  The active
    integration path is the Anthropic SDK monkey-patch in
    ``app/hermes/main.py``.  This file is retained as a reference for a
    potential future litellm-based provider route.

This module configures litellm as a transparent OpenAI-compatible proxy that
routes model calls to Amazon Bedrock ConverseStream.  hermes-agent's existing
multi-provider architecture only needs a ``base_url`` override to use it.

Usage in contract.py:
    from bridge.bedrock_provider import configure_bedrock
    configure_bedrock()
    # Then start litellm proxy or configure hermes-agent to point at it.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("agentcore.bedrock_provider")

# ---- Model mapping -------------------------------------------------------
# hermes-agent model name  →  litellm / Bedrock model identifier
# The "bedrock/" prefix tells litellm to use the Bedrock provider.

MODEL_MAP: dict[str, str] = {
    # Anthropic — cross-region inference profiles (global.*)
    "claude-opus-4":           "bedrock/global.anthropic.claude-opus-4-6-v1",
    "claude-opus-4-6":         "bedrock/global.anthropic.claude-opus-4-6-v1",
    "claude-sonnet-4":         "bedrock/global.anthropic.claude-sonnet-4-6-v1",
    "claude-sonnet-4-6":       "bedrock/global.anthropic.claude-sonnet-4-6-v1",
    "claude-haiku-3.5":        "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1",
    "claude-haiku-4-5":        "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1",
    # Direct region model IDs (no global prefix)
    "anthropic.claude-opus-4-6-v1":         "bedrock/anthropic.claude-opus-4-6-v1",
    "anthropic.claude-sonnet-4-6-v1":       "bedrock/anthropic.claude-sonnet-4-6-v1",
    "anthropic.claude-haiku-4-5-20251001-v1": "bedrock/anthropic.claude-haiku-4-5-20251001-v1",
}


def resolve_model(hermes_model: str) -> str:
    """Resolve a hermes-agent model name to a litellm/Bedrock model ID.

    If the model is not in the map, return it unchanged (allows passthrough
    for non-Bedrock models accessed via NAT gateway).
    """
    return MODEL_MAP.get(hermes_model, hermes_model)


def configure_bedrock() -> None:
    """Set environment variables so litellm uses Bedrock by default.

    Call once at startup, before any litellm import.
    """
    # litellm reads AWS credentials from the IAM role attached to the
    # AgentCore runtime — no explicit keys needed.
    default_model = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6-v1")
    os.environ.setdefault("LITELLM_MODEL", f"bedrock/{default_model}")

    # Suppress litellm telemetry in production.
    os.environ.setdefault("LITELLM_TELEMETRY", "False")

    logger.info("Bedrock provider configured (default_model=%s)", default_model)
