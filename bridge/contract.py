"""AgentCore HTTP contract server for hermes-agent.

Implements the two endpoints required by Amazon Bedrock AgentCore:
  GET  /ping         → health check
  POST /invocations  → message dispatch

Lifecycle:
  1. Contract server starts on port 8080 (fast, minimal deps).
  2. Lightweight warm-up agent initialises immediately (~1-2 s).
  3. Full hermes-agent loads in a background thread (~10-30 s).
  4. Once ready, all subsequent requests go to the full agent.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

logger = logging.getLogger("agentcore.contract")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class _State:
    """Mutable singleton shared across request threads."""

    agent: Any = None                 # Full AIAgent instance
    agent_ready: bool = False
    agent_lock = threading.Lock()

    lightweight: Any = None           # WarmupAgent instance
    lightweight_ready: bool = False

    busy_count: int = 0               # Number of in-flight requests
    busy_lock = threading.Lock()

    start_time: float = time.time()

    workspace_sync: Any = None        # WorkspaceSync instance (set after init)


S = _State()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class AgentCoreHandler(BaseHTTPRequestHandler):
    """Handles /ping and /invocations per the AgentCore contract."""

    # Keep-alive helps when AgentCore reuses the connection for /ping polling.
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # GET /ping
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/ping":
            # HealthyBusy tells AgentCore the container is active and should
            # not be terminated even if the idle timeout has elapsed.
            busy = S.busy_count > 0 or not S.agent_ready
            status = "HealthyBusy" if busy else "Healthy"
            self._send_json({"status": status})
        else:
            self.send_error(404)

    # ------------------------------------------------------------------
    # POST /invocations
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        if self.path != "/invocations":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body: dict = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, status=400)
            return

        action = body.get("action", "status")

        with S.busy_lock:
            S.busy_count += 1
        try:
            result = self._dispatch(action, body)
            self._send_json(result)
        except Exception:
            tb = traceback.format_exc()
            logger.error("Invocation error: %s", tb)
            self._send_json({"error": str(tb)}, status=500)
        finally:
            with S.busy_lock:
                S.busy_count -= 1

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, action: str, body: dict) -> dict:
        if action == "chat":
            return self._handle_chat(body)
        if action == "warmup":
            return self._handle_warmup(body)
        if action == "cron":
            return self._handle_cron(body)
        if action == "status":
            return self._handle_status()
        return {"error": f"Unknown action: {action}"}

    # -- chat --

    def _handle_chat(self, body: dict) -> dict:
        user_id = body.get("userId", "unknown")
        message = body.get("message", "")
        channel = body.get("channel", "agentcore")

        if not message.strip():
            return {"response": "", "metadata": {"skipped": True}}

        if S.agent_ready:
            resp = _run_full_agent(user_id, message, channel, body)
        elif S.lightweight_ready:
            resp = _run_warmup_agent(user_id, message, channel, body)
        else:
            resp = "Agent is starting up. Please try again in a few seconds."

        return {"response": resp}

    # -- warmup --

    def _handle_warmup(self, body: dict) -> dict:
        _ensure_full_agent_started()
        return {"status": "warming" if not S.agent_ready else "ready"}

    # -- cron --

    def _handle_cron(self, body: dict) -> dict:
        if not S.agent_ready:
            return {"response": "Agent not ready for cron execution", "ready": False}
        prompt = body.get("config", {}).get("prompt", body.get("message", ""))
        user_id = body.get("userId", "cron")
        resp = _run_full_agent(user_id, prompt, "cron", body)
        return {"response": resp, "jobId": body.get("jobId", "")}

    # -- status --

    def _handle_status(self) -> dict:
        return {
            "agent_ready": S.agent_ready,
            "lightweight_ready": S.lightweight_ready,
            "uptime_seconds": int(time.time() - S.start_time),
            "busy_count": S.busy_count,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN401
        # Route stdlib http logs through our logger.
        logger.debug(fmt, *args)


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------

def _run_warmup_agent(user_id: str, message: str, channel: str, body: dict) -> str:
    try:
        return S.lightweight.handle(message, user_id)
    except Exception as exc:
        logger.error("Warmup agent error: %s", exc)
        return "I'm still starting up — please try again in a moment."


def _run_full_agent(user_id: str, message: str, channel: str, body: dict) -> str:
    """Run a message through the full hermes-agent."""
    agent = S.agent
    try:
        # Inject platform context so hermes-agent knows the channel.
        system_extra = f"The user is contacting you via {channel}."
        if body.get("chatId"):
            system_extra += f" Chat ID: {body['chatId']}."

        result = agent.run_conversation(
            user_message=message,
            system_message=system_extra,
            conversation_history=None,
        )
        return result.get("final_response", "")
    except Exception as exc:
        logger.error("Full agent error: %s", exc)
        return f"Sorry, an error occurred: {exc}"


# ---------------------------------------------------------------------------
# Lazy initialisation helpers
# ---------------------------------------------------------------------------

_full_agent_thread: threading.Thread | None = None


def _init_lightweight_agent() -> None:
    """Start the lightweight warm-up agent (fast, minimal deps)."""
    try:
        from bridge.warmup_agent import WarmupAgent  # noqa: WPS433

        S.lightweight = WarmupAgent()
        S.lightweight_ready = True
        logger.info("Lightweight warm-up agent ready")
    except Exception as exc:
        logger.warning("Could not start warm-up agent: %s", exc)


def _init_full_agent() -> None:
    """Load the full hermes-agent (slow — runs in background thread)."""
    logger.info("Loading full hermes-agent …")
    try:
        # Ensure headless mode so gateway channels are never started.
        os.environ["HERMES_HEADLESS"] = "1"
        os.environ.setdefault("AGENTCORE_MODE", "1")

        from run_agent import AIAgent  # noqa: WPS433

        model = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        provider = os.environ.get("HERMES_PROVIDER", "bedrock")
        base_url = os.environ.get("HERMES_BASE_URL", "")

        kwargs: dict[str, Any] = {
            "model": model,
            "quiet_mode": True,
        }
        if provider:
            kwargs["provider"] = provider
        if base_url:
            kwargs["base_url"] = base_url

        S.agent = AIAgent(**kwargs)
        S.agent_ready = True
        logger.info("Full hermes-agent ready (model=%s)", model)
    except Exception:
        logger.error("Failed to load full hermes-agent:\n%s", traceback.format_exc())


def _ensure_full_agent_started() -> None:
    global _full_agent_thread
    if _full_agent_thread is None:
        _full_agent_thread = threading.Thread(target=_init_full_agent, daemon=True)
        _full_agent_thread.start()


# ---------------------------------------------------------------------------
# Workspace sync helpers
# ---------------------------------------------------------------------------

def _init_workspace_sync() -> None:
    """Initialise workspace-sync if S3 bucket is configured."""
    bucket = os.environ.get("S3_BUCKET", "")
    namespace = os.environ.get("AGENTCORE_USER_NAMESPACE", "")
    if not bucket or not namespace:
        logger.info("Workspace sync disabled (S3_BUCKET or AGENTCORE_USER_NAMESPACE not set)")
        return

    try:
        from bridge.workspace_sync import WorkspaceSync  # noqa: WPS433

        sync = WorkspaceSync()
        sync.restore(namespace)
        sync.start_periodic_save(namespace)
        S.workspace_sync = sync
        logger.info("Workspace sync initialised (bucket=%s, ns=%s)", bucket, namespace)
    except Exception as exc:
        logger.warning("Workspace sync init failed: %s", exc)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _sigterm_handler(signum: int, frame: Any) -> None:  # noqa: ANN401
    logger.info("SIGTERM received — saving state …")
    try:
        namespace = os.environ.get("AGENTCORE_USER_NAMESPACE", "")
        if S.workspace_sync and namespace:
            S.workspace_sync.save(namespace)
            logger.info("Final workspace save complete")
    except Exception as exc:
        logger.error("Failed to save state on shutdown: %s", exc)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    port = int(os.environ.get("PORT", "8080"))

    # Register SIGTERM handler for graceful shutdown.
    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Phase 0: restore workspace from S3 (blocking, fast if nothing to restore).
    _init_workspace_sync()

    # Phase 1: lightweight agent — available almost immediately.
    _init_lightweight_agent()

    # Phase 2: full agent — loads in background.
    _ensure_full_agent_started()

    # Phase 3: start HTTP server.
    server = HTTPServer(("0.0.0.0", port), AgentCoreHandler)
    server.request_queue_size = 16
    logger.info("AgentCore contract server listening on port %d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
