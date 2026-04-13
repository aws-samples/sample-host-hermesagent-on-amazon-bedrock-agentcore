"""Tests for the Router Lambda."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda", "router"))

# Patch boto3 before importing the module.
mock_dynamodb_resource = MagicMock()
mock_table = MagicMock()
mock_dynamodb_resource.Table.return_value = mock_table


@pytest.fixture(autouse=True)
def _setup_env():
    """Set required environment variables."""
    with patch.dict(os.environ, {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock:us-east-1:123456789:agent-runtime/hermes",
        "AGENTCORE_QUALIFIER": "production",
        "IDENTITY_TABLE": "hermes-identity",
        "S3_BUCKET": "hermes-user-files",
    }):
        yield


# --------------------------------------------------------------------------
# Tests — helper functions
# --------------------------------------------------------------------------

def test_build_session_id():
    """Session IDs must be >= 33 characters."""
    # Import after env is set.
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import _build_session_id

    session_id = _build_session_id("user_abc123", "telegram")
    assert len(session_id) >= 33
    assert "user_abc123" in session_id
    assert "telegram" in session_id


def test_split_message():
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import _split_message

    # Short message — single chunk.
    chunks = _split_message("hello", max_len=4096)
    assert chunks == ["hello"]

    # Long message — should split.
    long_msg = "x" * 5000
    chunks = _split_message(long_msg, max_len=4096)
    assert len(chunks) == 2
    assert len(chunks[0]) <= 4096
    assert "".join(chunks) == long_msg


def test_split_message_on_newline():
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import _split_message

    # Should prefer splitting on newlines.
    msg = "line1\n" + "x" * 4090 + "\nline3"
    chunks = _split_message(msg, max_len=4096)
    assert len(chunks) >= 2
    assert chunks[0].endswith("line1")  # Split at the first newline within limit.


def test_parse_body_json():
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import _parse_body

    event = {"body": '{"key": "value"}', "isBase64Encoded": False}
    result = _parse_body(event)
    assert result == {"key": "value"}


def test_parse_body_base64():
    import base64
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import _parse_body

    encoded = base64.b64encode(b'{"key": "value"}').decode()
    event = {"body": encoded, "isBase64Encoded": True}
    result = _parse_body(event)
    assert result == {"key": "value"}


# --------------------------------------------------------------------------
# Tests — handler routing
# --------------------------------------------------------------------------

def test_health_endpoint():
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import handler

    event = {
        "rawPath": "/health",
        "requestContext": {"http": {"method": "GET"}},
    }
    result = handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "healthy"


def test_unknown_path():
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import handler

    event = {
        "rawPath": "/unknown",
        "requestContext": {"http": {"method": "GET"}},
    }
    result = handler(event, None)
    assert result["statusCode"] == 404


def test_telegram_empty_update():
    """Telegram updates without a message should be ignored."""
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import handler

    event = {
        "rawPath": "/webhook/telegram",
        "requestContext": {"http": {"method": "POST"}},
        "body": json.dumps({"update_id": 123}),
        "isBase64Encoded": False,
    }
    result = handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "ignored"


def test_slack_url_verification():
    """Slack URL verification challenge should be echoed back."""
    with patch("boto3.resource", return_value=mock_dynamodb_resource), \
         patch("boto3.client"):
        from index import handler

    event = {
        "rawPath": "/webhook/slack",
        "requestContext": {"http": {"method": "POST"}},
        "body": json.dumps({
            "type": "url_verification",
            "challenge": "test_challenge_123",
        }),
        "isBase64Encoded": False,
    }
    result = handler(event, None)
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["challenge"] == "test_challenge_123"
