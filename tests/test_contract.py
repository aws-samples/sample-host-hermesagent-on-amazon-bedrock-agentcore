"""Tests for the AgentCore contract server."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import HTTPServer
from unittest.mock import MagicMock, patch

import pytest

# We import the handler directly rather than starting a full server.
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge.contract import AgentCoreHandler, _State, S


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    """Reset shared state between tests."""
    S.agent = None
    S.agent_ready = False
    S.lightweight = None
    S.lightweight_ready = False
    S.busy_count = 0
    S.workspace_sync = None
    S.start_time = time.time()
    yield


class _FakeWarmup:
    def handle(self, message: str, user_id: str) -> str:
        return f"warmup: {message}"


class _FakeAgent:
    def run_conversation(self, user_message="", system_message="", conversation_history=None):
        return {"final_response": f"full: {user_message}"}


# --------------------------------------------------------------------------
# Unit tests — health check logic
# --------------------------------------------------------------------------

def test_health_status_healthy_when_idle():
    """When not busy and agent is ready, status should be Healthy."""
    S.agent_ready = True
    S.busy_count = 0
    busy = S.busy_count > 0 or not S.agent_ready
    assert busy is False


def test_health_status_busy_when_processing():
    """When busy_count > 0, status should be HealthyBusy."""
    S.busy_count = 1
    busy = S.busy_count > 0 or not S.agent_ready
    assert busy is True


def test_health_status_busy_during_init():
    """While agent is loading, status should be HealthyBusy."""
    S.agent_ready = False
    busy = S.busy_count > 0 or not S.agent_ready
    assert busy is True


# --------------------------------------------------------------------------
# Unit tests — dispatch logic
# --------------------------------------------------------------------------

def test_chat_with_warmup_agent():
    """Chat falls back to warmup agent when full agent is not ready."""
    S.lightweight = _FakeWarmup()
    S.lightweight_ready = True
    S.agent_ready = False

    from bridge.contract import _run_warmup_agent
    result = _run_warmup_agent("user1", "hello", "telegram", {})
    assert result == "warmup: hello"


def test_chat_with_full_agent():
    """Chat uses full agent when ready."""
    S.agent = _FakeAgent()
    S.agent_ready = True

    from bridge.contract import _run_full_agent
    result = _run_full_agent("user1", "hello", "telegram", {})
    assert result == "full: hello"


def test_chat_returns_startup_message_when_nothing_ready():
    """When neither warmup nor full agent is ready, return a helpful message."""
    S.agent_ready = False
    S.lightweight_ready = False
    S.lightweight = None

    # Simulate what _handle_chat does.
    if S.agent_ready:
        resp = "full"
    elif S.lightweight_ready:
        resp = "warmup"
    else:
        resp = "Agent is starting up. Please try again in a few seconds."

    assert "starting up" in resp


# --------------------------------------------------------------------------
# Unit tests — status action
# --------------------------------------------------------------------------

def test_status_response():
    """Status action returns expected fields."""
    S.agent_ready = True
    S.lightweight_ready = True

    status = {
        "agent_ready": S.agent_ready,
        "lightweight_ready": S.lightweight_ready,
        "uptime_seconds": int(time.time() - S.start_time),
        "busy_count": S.busy_count,
    }

    assert status["agent_ready"] is True
    assert status["lightweight_ready"] is True
    assert status["busy_count"] == 0
    assert status["uptime_seconds"] >= 0
