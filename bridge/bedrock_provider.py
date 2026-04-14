"""Bedrock model configuration for hermes-agent.

With the native Bedrock provider (provider="bedrock"), hermes-agent handles
Converse API routing directly via boto3. This module provides model ID
resolution helpers for the bridge layer.

The native provider supports ALL Bedrock models (Claude, Nova, DeepSeek,
Llama, Mistral, etc.) - not just Anthropic models.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("agentcore.bedrock_provider")


def get_default_model() -> str:
    """Return the configured Bedrock model ID."""
    return os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")


def get_region() -> str:
    """Return the configured AWS region."""
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )
