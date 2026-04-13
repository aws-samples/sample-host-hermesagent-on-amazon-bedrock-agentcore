# Hermes-Agent on Amazon Bedrock AgentCore: Migration Plan

> Phased implementation plan for deploying hermes-agent as a managed AgentCore runtime.

## Executive Summary

**Verdict: Feasible, with significant architectural adaptation.**

Amazon Bedrock AgentCore provides per-user serverless Firecracker microVMs. The reference project ([sample-host-openclaw-on-amazon-bedrock-agentcore](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore)) demonstrates the proven pattern: a bridge container implements the AgentCore HTTP contract, proxies LLM calls to Bedrock, and runs an agent framework headlessly inside the microVM.

hermes-agent follows the same pattern with Python-specific adaptations.

| Challenge | Severity | Reason |
|-----------|----------|--------|
| Cold start latency (Python + deps) | **Critical** | 10-30s startup; warm-up agent required from day 1 |
| Single-process multi-user → per-user microVM | **High** | Architecture inversion: hermes runs one process for all users |
| SQLite → S3-backed persistence | **High** | Session storage ephemeral across container image updates |
| Channel routing extraction | **Medium** | In-process adapters (Telegram, Discord, etc.) move to Router Lambda |
| LLM provider routing | **Low** | litellm handles Bedrock + external API fallback natively |
| Self-learning persistence | **Medium** | Skills, memories, sessions need S3 workspace sync |

**Estimated effort**: 6-8 weeks for production. 3-day PoC path available.

---

## Phase 1: Contract Server + Warm-up Agent (Week 1-2)

### Goal
hermes-agent running inside an AgentCore container, responding to `/ping` and `/invocations`, with a lightweight warm-up agent handling messages during startup.

### 1.1 Contract Server (`bridge/contract.py`)

Minimal HTTP server implementing the AgentCore protocol on port 8080:

```python
"""AgentCore HTTP contract server for hermes-agent."""

import asyncio
import json
import logging
import os
import signal
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("agentcore.contract")

class ContractState:
    """Shared state for the contract server."""
    agent = None                # Full AIAgent instance
    agent_ready = False         # True when full agent is loaded
    agent_lock = threading.Lock()
    is_busy = False             # True during request processing
    start_time = time.time()
    lightweight_agent = None    # Warm-up agent

class AgentCoreHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/ping":
            status = "HealthyBusy" if ContractState.is_busy else "Healthy"
            self._send_json({"status": status})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/invocations":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))
        action = body.get("action", "status")

        ContractState.is_busy = True
        try:
            if action == "chat":
                result = self._handle_chat(body)
                self._send_json({"response": result})
            elif action == "warmup":
                self._handle_warmup(body)
                self._send_json({"status": "ready"})
            elif action == "cron":
                result = self._handle_cron(body)
                self._send_json({"response": result})
            elif action == "status":
                self._send_json({
                    "agent_ready": ContractState.agent_ready,
                    "uptime_seconds": int(time.time() - ContractState.start_time),
                })
            else:
                self._send_json({"error": f"Unknown action: {action}"}, status=400)
        finally:
            ContractState.is_busy = False

    def _handle_chat(self, body):
        user_id = body.get("userId", "unknown")
        message = body.get("message", "")
        channel = body.get("channel", "agentcore")

        if ContractState.agent_ready:
            # Full hermes-agent handles the message
            return self._run_full_agent(user_id, message, channel, body)
        else:
            # Lightweight warm-up agent handles it
            return self._run_warmup_agent(user_id, message, channel, body)

    def _run_full_agent(self, user_id, message, channel, body):
        """Run message through full hermes-agent."""
        agent = ContractState.agent
        # Inject session context
        result = agent.run_conversation(
            user_message=message,
            session_id=user_id,
        )
        return result.get("final_response", "")

    def _run_warmup_agent(self, user_id, message, channel, body):
        """Run message through lightweight warm-up agent."""
        if ContractState.lightweight_agent is None:
            return "Agent is starting up. Please try again in a moment."
        return ContractState.lightweight_agent.handle(message, user_id)

    def _handle_warmup(self, body):
        """Pre-warm the container."""
        self._ensure_agent(body)

    def _handle_cron(self, body):
        """Handle scheduled task execution."""
        job_id = body.get("jobId", "")
        job_config = body.get("config", {})
        if not ContractState.agent_ready:
            return "Agent not ready for cron execution"
        # Execute cron job through full agent
        return self._run_full_agent(
            user_id=body.get("userId", "cron"),
            message=job_config.get("prompt", ""),
            channel="cron",
            body=body,
        )

    def _ensure_agent(self, body):
        """Lazy-initialize the full hermes-agent."""
        with ContractState.agent_lock:
            if not ContractState.agent_ready:
                _init_full_agent(body.get("userId"))

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        logger.info(format, *args)


def _init_lightweight_agent():
    """Initialize the lightweight warm-up agent (fast, minimal deps)."""
    from bridge.warmup_agent import WarmupAgent
    ContractState.lightweight_agent = WarmupAgent()
    logger.info("Lightweight warm-up agent ready")


def _init_full_agent(user_id=None):
    """Initialize the full hermes-agent (slow, all dependencies)."""
    logger.info("Loading full hermes-agent...")
    os.environ["HERMES_HEADLESS"] = "1"

    from run_agent import AIAgent
    ContractState.agent = AIAgent(
        quiet_mode=True,
        session_id=user_id,
    )
    ContractState.agent_ready = True
    logger.info("Full hermes-agent ready")


def _sigterm_handler(signum, frame):
    """Graceful shutdown: save state to S3 before exit."""
    logger.info("SIGTERM received, saving state...")
    try:
        from bridge.workspace_sync import WorkspaceSync
        sync = WorkspaceSync()
        namespace = os.environ.get("AGENTCORE_USER_NAMESPACE", "default")
        sync.save(namespace)
    except Exception as e:
        logger.error("Failed to save state on shutdown: %s", e)
    raise SystemExit(0)


def main():
    port = int(os.environ.get("PORT", "8080"))
    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Phase 1: Start lightweight agent immediately (fast)
    _init_lightweight_agent()

    # Phase 2: Start full agent in background thread (slow)
    bg_thread = threading.Thread(target=_init_full_agent, daemon=True)
    bg_thread.start()

    # Phase 3: Start HTTP server
    server = HTTPServer(("0.0.0.0", port), AgentCoreHandler)
    logger.info("AgentCore contract server listening on port %d", port)
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
```

### 1.2 Warm-up Agent (`bridge/warmup_agent.py`)

Lightweight agent that handles messages during the 10-30s full agent startup:

```python
"""Lightweight warm-up agent for fast cold-start response."""

import json
import boto3
import os

class WarmupAgent:
    """Minimal agent using Bedrock directly (no hermes dependencies)."""

    SYSTEM_PROMPT = """You are Hermes, an AI assistant. The full agent is still loading.
You can answer simple questions directly. For complex tasks requiring tools
(code execution, file operations, web browsing), let the user know you'll be
fully ready in a moment.

Keep responses concise and helpful."""

    def __init__(self):
        self.bedrock = boto3.client("bedrock-runtime")
        self.model_id = os.environ.get(
            "BEDROCK_MODEL_ID",
            "global.anthropic.claude-sonnet-4-6-v1"
        )

    def handle(self, message: str, user_id: str) -> str:
        try:
            response = self.bedrock.converse(
                modelId=self.model_id,
                messages=[{"role": "user", "content": [{"text": message}]}],
                system=[{"text": self.SYSTEM_PROMPT}],
                inferenceConfig={"maxTokens": 1024, "temperature": 0.7},
            )
            return response["output"]["message"]["content"][0]["text"]
        except Exception as e:
            return f"I'm still starting up. Please try again in a few seconds. (Error: {e})"
```

### 1.3 Dockerfile (`bridge/Dockerfile`)

ARM64 multi-stage build for AgentCore:

```dockerfile
# Stage 1: Builder
FROM --platform=linux/arm64 python:3.11-slim AS builder

WORKDIR /build

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make libffi-dev libsqlite3-dev git \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies (cached layer)
COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install \
    -e ".[cron,mcp]" litellm boto3 || \
    pip install --no-cache-dir --prefix=/install \
    litellm boto3

# Copy source
COPY . /build/

# Install hermes-agent
RUN pip install --no-cache-dir --prefix=/install -e ".[cron,mcp]"


# Stage 2: Runtime
FROM --platform=linux/arm64 python:3.11-slim

WORKDIR /app

# Minimal runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsqlite3-0 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages
COPY --from=builder /install /usr/local

# Copy application source
COPY --from=builder /build /app

# Copy bridge components
COPY bridge/ /app/bridge/

# Create hermes user
RUN useradd -u 10000 -m -d /opt/data hermes \
    && mkdir -p /opt/data/.hermes \
    && chown -R hermes:hermes /opt/data

# AgentCore workspace mount point
VOLUME ["/mnt/workspace"]

# Non-root execution
USER hermes

# AgentCore health check port
EXPOSE 8080

# Environment
ENV HERMES_HEADLESS=1
ENV AGENTCORE_MODE=1
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Entrypoint
COPY bridge/entrypoint.sh /app/bridge/entrypoint.sh
ENTRYPOINT ["/app/bridge/entrypoint.sh"]
```

### 1.4 Entrypoint Script (`bridge/entrypoint.sh`)

```bash
#!/bin/bash
set -e

# Force IPv4 DNS resolution (AgentCore VPC best practice)
export NODE_OPTIONS="--dns-result-order=ipv4first"

# Setup hermes home directory
HERMES_HOME="${HERMES_HOME:-/opt/data/.hermes}"
WORKSPACE="/mnt/workspace"

# If session storage is available, use it as hermes home
if [ -d "$WORKSPACE" ]; then
    mkdir -p "$WORKSPACE/.hermes"
    # Symlink hermes home to workspace for persistence
    if [ ! -L "$HERMES_HOME" ]; then
        rm -rf "$HERMES_HOME"
        ln -s "$WORKSPACE/.hermes" "$HERMES_HOME"
    fi
fi

# Restore from S3 if available (workspace_sync handles this)
if [ -n "$S3_BUCKET" ] && [ -n "$AGENTCORE_USER_NAMESPACE" ]; then
    python -c "
from bridge.workspace_sync import WorkspaceSync
sync = WorkspaceSync()
sync.restore('$AGENTCORE_USER_NAMESPACE')
" 2>/dev/null || echo "S3 restore skipped (first run or no backup)"
fi

# Launch contract server
exec python -m bridge.contract "$@"
```

### 1.5 Headless Mode

Add `HERMES_HEADLESS` check to gateway startup:

```python
# In gateway/run.py, at the top of GatewayRunner.__init__:
if os.environ.get("HERMES_HEADLESS"):
    raise RuntimeError("Gateway disabled in headless mode (HERMES_HEADLESS=1)")

# In hermes_cli/main.py, skip channel boot:
if os.environ.get("HERMES_HEADLESS"):
    # Only expose agent loop via contract server
    pass
```

### 1.6 Bedrock LLM Provider via litellm

```python
# bridge/bedrock_provider.py
"""Bedrock LLM provider for hermes-agent via litellm."""

import os

def configure_litellm():
    """Configure litellm as the LLM provider for Bedrock."""
    # litellm reads AWS credentials from IAM role (no API keys needed)
    os.environ.setdefault("LITELLM_MODEL", "bedrock/anthropic.claude-sonnet-4-6-v1")

    # Model mapping: hermes model names → Bedrock model IDs
    MODEL_MAP = {
        "claude-opus-4": "bedrock/global.anthropic.claude-opus-4-6-v1",
        "claude-sonnet-4": "bedrock/global.anthropic.claude-sonnet-4-6-v1",
        "claude-haiku-3.5": "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1",
    }
    return MODEL_MAP
```

### Deliverable
hermes-agent running in a Docker container on AgentCore, responding to `/ping` and `/invocations`, with a warm-up agent for fast cold-start responses.

---

## Phase 2: State Persistence (Week 2-4)

### Goal
All hermes-agent state (sessions, memory, skills, SQLite) persists across container restarts via S3 backup.

### 2.1 Workspace Sync Module (`bridge/workspace_sync.py`)

Port the pattern from the sample's `workspace-sync.js`:

```python
"""S3-backed workspace persistence for AgentCore."""

import os
import time
import sqlite3
import logging
import threading
from pathlib import Path
from fnmatch import fnmatch

import boto3

logger = logging.getLogger("agentcore.workspace_sync")

SKIP_PATTERNS = [
    "__pycache__/", "*.pyc", "*.log", "*.tmp",
    "node_modules/", ".git/", "*.sock",
]

class WorkspaceSync:
    def __init__(self):
        self.s3 = boto3.client("s3")
        self.bucket = os.environ.get("S3_BUCKET", "")
        self.workspace = Path(os.environ.get("WORKSPACE_PATH", "/mnt/workspace/.hermes"))
        self.sync_interval = int(os.environ.get("WORKSPACE_SYNC_INTERVAL", "300"))
        self._stop_event = threading.Event()

    def restore(self, namespace: str):
        """Download workspace from S3 on container init."""
        prefix = f"{namespace}/.hermes/"
        logger.info("Restoring workspace from s3://%s/%s", self.bucket, prefix)

        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                relative = key[len(prefix):]
                local_path = self.workspace / relative
                if self._should_skip(relative):
                    continue
                local_path.parent.mkdir(parents=True, exist_ok=True)
                self.s3.download_file(self.bucket, key, str(local_path))

        # Special handling: restore SQLite with integrity check
        db_path = self.workspace / "state.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                result = conn.execute("PRAGMA integrity_check").fetchone()
                conn.close()
                if result[0] != "ok":
                    logger.warning("SQLite integrity check failed, removing corrupted DB")
                    db_path.unlink()
            except Exception:
                logger.warning("SQLite open failed, removing corrupted DB")
                db_path.unlink()

        logger.info("Workspace restore complete")

    def save(self, namespace: str):
        """Upload workspace to S3."""
        prefix = f"{namespace}/.hermes/"
        logger.info("Saving workspace to s3://%s/%s", self.bucket, prefix)

        # Hot-copy SQLite before upload
        db_path = self.workspace / "state.db"
        if db_path.exists():
            backup_path = self.workspace / "state.db.bak"
            conn = sqlite3.connect(str(db_path))
            backup_conn = sqlite3.connect(str(backup_path))
            conn.backup(backup_conn)
            backup_conn.close()
            conn.close()
            self.s3.upload_file(str(backup_path), self.bucket, f"{prefix}state.db")
            backup_path.unlink()

        # Upload all other files
        for path in self.workspace.rglob("*"):
            if path.is_dir() or path.name == "state.db":
                continue
            relative = str(path.relative_to(self.workspace))
            if self._should_skip(relative):
                continue
            self.s3.upload_file(str(path), self.bucket, f"{prefix}{relative}")

        logger.info("Workspace save complete")

    def save_immediate(self, namespace: str):
        """Trigger an immediate save (for critical writes like skill creation)."""
        threading.Thread(target=self.save, args=(namespace,), daemon=True).start()

    def start_periodic_save(self, namespace: str):
        """Start background thread for periodic S3 sync."""
        def _loop():
            while not self._stop_event.is_set():
                self._stop_event.wait(self.sync_interval)
                if not self._stop_event.is_set():
                    try:
                        self.save(namespace)
                    except Exception as e:
                        logger.error("Periodic save failed: %s", e)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        logger.info("Periodic workspace sync started (interval=%ds)", self.sync_interval)

    def stop(self):
        self._stop_event.set()

    def _should_skip(self, path: str) -> bool:
        for pattern in SKIP_PATTERNS:
            if fnmatch(path, pattern) or fnmatch(path, f"*/{pattern}"):
                return True
        return False
```

### 2.2 S3-Backed SQLite Wrapper

```python
# bridge/s3_database.py
"""SQLite database with S3 backup/restore for AgentCore."""

import os
import sqlite3
from pathlib import Path

class S3BackedDatabase:
    def __init__(self, namespace: str, workspace_sync):
        self.local_path = Path("/mnt/workspace/.hermes/state.db")
        self.namespace = namespace
        self.sync = workspace_sync

    def get_connection(self) -> sqlite3.Connection:
        """Get SQLite connection (restored from S3 if needed)."""
        return sqlite3.connect(str(self.local_path))

    def on_critical_write(self):
        """Called after skill creation, memory write, etc."""
        if os.environ.get("AGENTCORE_MODE"):
            self.sync.save_immediate(self.namespace)
```

### 2.3 Session Storage Symlink

On container init (`entrypoint.sh`):
```
~/.hermes → /mnt/workspace/.hermes (if session storage available)
Falls back to S3-only mode if /mnt/workspace not mounted
```

### 2.4 SIGTERM Graceful Shutdown

Already implemented in `contract.py` (`_sigterm_handler`). On shutdown:
1. Stop accepting new requests
2. Wait for in-flight requests to complete (10s timeout)
3. Final S3 backup of all state
4. Close SQLite connections
5. Exit

### Deliverable
All hermes-agent state persists across container restarts. SQLite hot-copy safety. Immediate sync on critical writes.

---

## Phase 3: Channel Routing via Router Lambda (Week 3-5)

### Goal
All messaging channels routed through Router Lambda + API Gateway. Container receives clean text/image payloads.

### 3.1 Router Lambda (`lambda/router/index.py`)

```python
"""Router Lambda: channel webhook → AgentCore invocation."""

import json
import os
import boto3
import hashlib
import hmac
import time
import logging

logger = logging.getLogger()
dynamodb = boto3.resource("dynamodb")
agentcore = boto3.client("bedrock-agentcore")
identity_table = dynamodb.Table(os.environ["IDENTITY_TABLE"])

RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
QUALIFIER = os.environ["AGENTCORE_QUALIFIER"]


def handler(event, context):
    """API Gateway HTTP API handler."""
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "")

    # Channel routing
    if path.startswith("/webhook/telegram"):
        return handle_telegram(event)
    elif path.startswith("/webhook/slack"):
        return handle_slack(event)
    elif path.startswith("/webhook/discord"):
        return handle_discord(event)
    else:
        return {"statusCode": 404, "body": "Not found"}


def handle_telegram(event):
    """Process Telegram webhook."""
    body = json.loads(event.get("body", "{}"))
    # Validate Telegram webhook...

    message = body.get("message", {})
    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))
    user_id = str(message.get("from", {}).get("id", ""))
    actor_id = f"telegram:{user_id}"

    # Check allowlist
    if not is_allowed(actor_id):
        return {"statusCode": 200, "body": "OK"}

    # Resolve identity
    hermes_user_id = resolve_user(actor_id)

    # Build session ID (must be >= 33 characters)
    session_id = build_session_id(hermes_user_id, "telegram")

    # Invoke AgentCore
    payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "telegram",
        "chatId": chat_id,
        "message": text,
    }

    response = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier=QUALIFIER,
        runtimeSessionId=session_id,
        runtimeUserId=actor_id,
        payload=json.dumps(payload),
        contentType="application/json",
        accept="application/json",
    )

    result = json.loads(response["payload"].read())

    # Send response back to Telegram
    send_telegram_message(chat_id, result.get("response", ""))

    return {"statusCode": 200, "body": "OK"}


def resolve_user(actor_id: str) -> str:
    """Look up or create user in DynamoDB identity table."""
    resp = identity_table.get_item(Key={"PK": f"CHANNEL#{actor_id}", "SK": "PROFILE"})
    if "Item" in resp:
        return resp["Item"]["userId"]

    # Create new user
    user_id = f"user_{hashlib.sha256(actor_id.encode()).hexdigest()[:12]}"
    identity_table.put_item(Item={
        "PK": f"CHANNEL#{actor_id}",
        "SK": "PROFILE",
        "userId": user_id,
        "createdAt": int(time.time()),
    })
    identity_table.put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": f"CHANNEL#{actor_id}",
        "actorId": actor_id,
    })
    return user_id


def build_session_id(user_id: str, channel: str) -> str:
    """Build AgentCore session ID (>= 33 chars)."""
    return f"{user_id}:{channel}:{'0' * (33 - len(user_id) - len(channel) - 2)}"


def is_allowed(actor_id: str) -> bool:
    """Check if actor is in allowlist."""
    resp = identity_table.get_item(Key={"PK": f"ALLOW#{actor_id}", "SK": "ALLOW"})
    return "Item" in resp


def send_telegram_message(chat_id: str, text: str):
    """Send message via Telegram Bot API."""
    import urllib.request
    token = get_secret("telegram-bot-token")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)


def get_secret(name: str) -> str:
    """Get secret from Secrets Manager (cached)."""
    if not hasattr(get_secret, "_cache"):
        get_secret._cache = {}
    if name not in get_secret._cache:
        sm = boto3.client("secretsmanager")
        resp = sm.get_secret_value(SecretId=f"hermes/{name}")
        get_secret._cache[name] = resp["SecretString"]
    return get_secret._cache[name]
```

### 3.2 DynamoDB Identity Table Schema

| PK | SK | Attributes |
|----|-----|------------|
| `CHANNEL#telegram:123456` | `PROFILE` | userId, createdAt |
| `USER#user_abc` | `SESSION` | sessionId, lastActive |
| `USER#user_abc` | `CHANNEL#telegram:123456` | actorId, channel |
| `ALLOW#telegram:123456` | `ALLOW` | addedBy, addedAt |

### 3.3 API Gateway Routes

| Route | Method | Handler |
|-------|--------|---------|
| `/webhook/telegram` | POST | `handle_telegram()` |
| `/webhook/slack` | POST | `handle_slack()` |
| `/webhook/discord` | POST | `handle_discord()` |
| `/webhook/matrix` | POST | `handle_matrix()` |
| `/health` | GET | Health check |

### 3.4 Channel Support Matrix

| Channel | hermes-agent | Router Lambda | Migration Effort |
|---------|-------------|---------------|------------------|
| Telegram | Full | Port from hermes + sample | Low (both have it) |
| Slack | Full | Port from hermes + sample | Low |
| Discord | Full | New Lambda handler | Medium |
| Matrix | Full | New Lambda handler | Medium |
| WhatsApp | Full | New Lambda handler | Medium |
| DingTalk | Full | Port from hermes | Medium |
| Feishu | Full | Port from hermes + sample | Low |
| Email | Full | New Lambda handler (SES) | High |
| SMS | Full | New Lambda handler (SNS) | Medium |
| Web UI | Full | Cognito + API GW WebSocket | High (Phase 7) |

### Deliverable
All messaging channels routed through Router Lambda + API Gateway. hermes-agent container receives clean payloads, responds via `/invocations`.

---

## Phase 4: Per-User Credential Isolation (Week 4-5)

### Goal
Each user's container can only access their own S3 namespace. Tool execution sandboxed.

### 4.1 STS Scoped Credentials (`bridge/scoped_credentials.py`)

```python
"""Per-user STS scoped credentials for AgentCore."""

import json
import os
import time
import threading
import boto3
import logging

logger = logging.getLogger("agentcore.credentials")

class ScopedCredentials:
    REFRESH_INTERVAL = 2700  # 45 min (STS max 1 hour)

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.sts = boto3.client("sts")
        self.bucket = os.environ["S3_BUCKET"]
        self.role_arn = os.environ["EXECUTION_ROLE_ARN"]
        self._credentials = None
        self._stop = threading.Event()

    def get(self):
        if self._credentials is None:
            self._refresh()
        return self._credentials

    def _refresh(self):
        session_policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject", "s3:PutObject",
                    "s3:DeleteObject", "s3:ListBucket"
                ],
                "Resource": [
                    f"arn:aws:s3:::{self.bucket}/{self.namespace}/*",
                    f"arn:aws:s3:::{self.bucket}",
                ],
                "Condition": {
                    "StringLike": {
                        "s3:prefix": [f"{self.namespace}/*"]
                    }
                }
            }]
        }
        resp = self.sts.assume_role(
            RoleArn=self.role_arn,
            RoleSessionName=f"hermes-{self.namespace[:32]}",
            Policy=json.dumps(session_policy),
            DurationSeconds=3600,
        )
        self._credentials = resp["Credentials"]
        logger.info("Scoped credentials refreshed for namespace=%s", self.namespace)

    def start_refresh_loop(self):
        def _loop():
            while not self._stop.is_set():
                self._stop.wait(self.REFRESH_INTERVAL)
                if not self._stop.is_set():
                    try:
                        self._refresh()
                    except Exception as e:
                        logger.error("Credential refresh failed: %s", e)
        threading.Thread(target=_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
```

### 4.2 Path Guard for AgentCore

Extend hermes-agent's existing `tools/path_guard.py`:

```python
# Additional blocked paths in AgentCore context
AGENTCORE_BLOCKED = [
    "/root/.aws/",
    "/etc/boto*",
    "/var/run/secrets/",
]
```

### Deliverable
Per-user S3 namespace isolation via STS. Credential auto-refresh every 45 minutes.

---

## Phase 5: Self-Learning Adaptation (Week 5-6)

### Goal
Full self-learning loop operational on AgentCore.

### 5.1 Background Review Agent
**No changes needed.** The `_spawn_background_review()` mechanism runs a thread-local review AIAgent. It writes to MEMORY.md/USER.md which are under workspace sync.

### 5.2 Session Search
**No changes needed.** FTS5 search works on the local SQLite copy restored from S3.

### 5.3 Immediate Sync on Skill Write

```python
# In tools/skill_manager_tool.py, after successful create/edit/patch:
if os.environ.get("AGENTCORE_MODE"):
    from bridge.workspace_sync import WorkspaceSync
    sync = WorkspaceSync()
    namespace = os.environ.get("AGENTCORE_USER_NAMESPACE", "default")
    sync.save_immediate(namespace)
```

### 5.4 External Memory Providers
**No changes needed.** Honcho and other API-based memory plugins work via VPC NAT gateway.

### Deliverable
Self-learning loop fully operational: background review, memory persistence, skill creation, cross-session search.

---

## Phase 6: CDK Infrastructure (Week 5-7)

### Goal
Full AWS infrastructure deployed via CDK with one-command deploy.

### 6.1 CDK Stacks

| Stack | Resources | Source |
|-------|-----------|--------|
| `HermesVpc` | VPC (2-AZ), subnets, NAT, 7 VPC endpoints | Fork `vpc_stack.py` |
| `HermesSecurity` | KMS CMK, Secrets Manager (7+ secrets), Cognito | Fork `security_stack.py` |
| `HermesGuardrails` | Bedrock Guardrails (content filter, PII) | Fork `guardrails_stack.py` |
| `HermesAgentCore` | IAM execution role, security groups, S3 bucket | Fork `agentcore_stack.py` |
| `HermesRouter` | Lambda, API Gateway, DynamoDB | Fork `router_stack.py` |
| `HermesCron` | EventBridge Scheduler, executor Lambda | Fork `cron_stack.py` |
| `HermesObservability` | CloudWatch dashboards, alarms, SNS | Fork `observability_stack.py` |
| `HermesTokenMonitoring` | Token usage analytics Lambda | Fork `token_monitoring_stack.py` |

### 6.2 IAM Execution Role Policy

```python
# 12+ policy statements (from sample's agentcore_stack.py):
# - bedrock:InvokeModel, bedrock:InvokeModelWithResponseStream (ConverseStream)
# - bedrock:ApplyGuardrail
# - s3:GetObject, s3:PutObject, s3:ListBucket (user files bucket)
# - secretsmanager:GetSecretValue (bot tokens, API keys)
# - cognito-idp:* (user pool operations)
# - sts:AssumeRole (self-assume for scoped credentials)
# - logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
# - ecr:GetAuthorizationToken, ecr:BatchGetImage
# - kms:Decrypt, kms:GenerateDataKey
# - cloudwatch:PutMetricData
# - dynamodb:GetItem, dynamodb:PutItem, dynamodb:Query (identity table)
```

### 6.3 Configuration (`cdk.json`)

```json
{
  "app": "python3 app.py",
  "context": {
    "project_name": "hermes-agentcore",
    "default_model_id": "global.anthropic.claude-opus-4-6-v1",
    "warmup_model_id": "global.anthropic.claude-sonnet-4-6-v1",
    "session_idle_timeout": 1800,
    "session_max_lifetime": 28800,
    "workspace_sync_interval_seconds": 300,
    "enable_guardrails": true,
    "enable_token_monitoring": true,
    "channels": ["telegram", "slack", "discord"],
    "vpc_cidr": "10.0.0.0/16",
    "az_count": 2
  }
}
```

### 6.4 Three-Phase Deploy Script (`scripts/deploy.sh`)

```bash
#!/bin/bash
set -euo pipefail

PHASE="${1:-all}"

# Phase 1: CDK foundation stacks
if [[ "$PHASE" == "all" || "$PHASE" == "phase1" ]]; then
    echo "=== Phase 1: CDK Foundation ==="
    cdk deploy HermesVpc HermesSecurity HermesGuardrails HermesAgentCore HermesObservability \
        --require-approval never
fi

# Phase 2: AgentCore Starter Toolkit (build + deploy runtime)
if [[ "$PHASE" == "all" || "$PHASE" == "phase2" ]]; then
    echo "=== Phase 2: AgentCore Runtime ==="
    pip install bedrock-agentcore-toolkit

    # Configure runtime
    agentcore configure --name hermes_agent

    # Build ARM64 Docker image and push to ECR
    agentcore deploy

    # Extract runtime ID and endpoint ID, write back to cdk.json
    RUNTIME_ID=$(agentcore status --json | jq -r '.runtimeId')
    ENDPOINT_ID=$(agentcore status --json | jq -r '.endpointId')

    # Update cdk.json with runtime IDs
    jq ".context.agentcore_runtime_id = \"$RUNTIME_ID\" | \
        .context.agentcore_endpoint_id = \"$ENDPOINT_ID\"" \
        cdk.json > cdk.json.tmp && mv cdk.json.tmp cdk.json
fi

# Phase 3: CDK dependent stacks (need runtime IDs from Phase 2)
if [[ "$PHASE" == "all" || "$PHASE" == "phase3" ]]; then
    echo "=== Phase 3: CDK Dependent Stacks ==="
    cdk deploy HermesRouter HermesCron HermesTokenMonitoring \
        --require-approval never
fi

echo "=== Deploy complete ==="
```

### Deliverable
Full AWS infrastructure deployed via CDK. One-command deploy with `./scripts/deploy.sh`.

---

## Phase 7: Advanced Features (Week 7-8)

### 7.1 Progressive Streaming

hermes-agent supports streaming via `stream_callback`. Wire through contract server:

```python
# In contract.py, for streaming responses:
# Use chunked transfer encoding to stream agent output
# Router Lambda can forward chunks as typing indicators
```

### 7.2 Multi-Model Routing on Bedrock

| hermes model config | Bedrock model ID |
|---------------------|------------------|
| `claude-opus-4` | `global.anthropic.claude-opus-4-6-v1` |
| `claude-sonnet-4` | `global.anthropic.claude-sonnet-4-6-v1` |
| `claude-haiku-3.5` | `global.anthropic.claude-haiku-4-5-20251001-v1` |
| Non-Bedrock models | Via NAT → external APIs (keys in Secrets Manager) |

### 7.3 AgentCore Browser API

AgentCore provides managed headless Chromium. Replace hermes-agent's local Playwright with AgentCore Browser:

```python
# In tools/browser_tool.py, detect AgentCore environment:
if os.environ.get("AGENTCORE_MODE"):
    # Use AgentCore Browser API (CDP endpoint provided by runtime)
    browser_ws = os.environ.get("AGENTCORE_BROWSER_WS")
    browser = await playwright.chromium.connect_over_cdp(browser_ws)
```

### 7.4 Sub-Agent Delegation

hermes-agent's `delegate_tool` spawns child agents in threads. On AgentCore, these run within the same container (same microVM). No changes needed.

### 7.5 Web UI

```
Browser → CloudFront → API Gateway WebSocket → Lambda → AgentCore → hermes-agent
                │
           Cognito auth
```

### 7.6 Guardrails Integration

```python
# In run_agent.py, wrap LLM calls with Bedrock Guardrails:
if os.environ.get("AGENTCORE_GUARDRAIL_ID"):
    response = bedrock.apply_guardrail(
        guardrailIdentifier=os.environ["AGENTCORE_GUARDRAIL_ID"],
        guardrailVersion=os.environ.get("AGENTCORE_GUARDRAIL_VERSION", "DRAFT"),
        source="OUTPUT",
        content=[{"text": {"text": llm_response}}],
    )
```

### Deliverable
Production-ready deployment with streaming, multi-model, browser automation, and web UI.

---

## Quick Start: 3-Day PoC

### Day 1: Contract Server

```bash
# Create bridge directory
mkdir -p bridge/
# Write contract.py, warmup_agent.py, entrypoint.sh (from Phase 1 above)
# Write Dockerfile

# Local test
docker build -t hermes-agentcore -f bridge/Dockerfile .
docker run -p 8080:8080 \
  -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
  -e BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6-v1 \
  hermes-agentcore

# Test endpoints
curl http://localhost:8080/ping
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"action":"chat","userId":"test_user","message":"hello"}'
```

### Day 2: Deploy to AgentCore

```bash
# Install toolkit
pip install bedrock-agentcore-toolkit

# Configure (generates .bedrock_agentcore.yaml)
agentcore configure --name hermes_agent

# Build and deploy (ARM64, pushes to ECR, creates runtime)
agentcore deploy

# Test invocation
agentcore invoke '{"action":"chat","userId":"test_001","actorId":"test:1","channel":"test","message":"What can you do?"}'
```

### Day 3: Telegram Integration

```bash
# Deploy minimal Router Lambda (CDK or SAM)
# Set Telegram webhook to API Gateway URL
# Test: send message on Telegram → Lambda → AgentCore → hermes-agent → response
```

---

## Risk Matrix

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Cold start timeout (Python 10-30s) | **High** | **High** | Warm-up agent handles messages during startup |
| SQLite data loss on container update | **High** | **High** | S3 backup + immediate sync on critical writes |
| Bedrock rate limits | **Medium** | **Medium** | Use cross-region inference profiles (`global.*`) |
| Workspace sync race condition | **Medium** | **Medium** | SQLite hot-copy + file locking |
| Per-user cost explosion | **Medium** | **High** | Aggressive idle timeout (30 min); token budgets |
| FTS5 on large databases | **Medium** | **Low** | Periodic compaction; 90-day retention |
| NAT Gateway fixed cost | **High** | **Low** | $30-45/mo regardless of usage; acceptable |
| Telegram 3s webhook timeout | **High** | **Medium** | Lambda returns 200 immediately; async dispatch |

---

## File Structure (Target)

```
sample-host-harmesagent-on-amazon-bedrock-agentcore/
├── README.md
├── cdk.json
├── app.py                          # CDK app entry point
├── requirements.txt
│
├── bridge/                         # AgentCore bridge components
│   ├── contract.py                 # HTTP contract server (port 8080)
│   ├── warmup_agent.py             # Lightweight warm-up agent
│   ├── workspace_sync.py           # S3 workspace persistence
│   ├── scoped_credentials.py       # Per-user STS credentials
│   ├── bedrock_provider.py         # litellm Bedrock configuration
│   ├── entrypoint.sh               # Container entrypoint
│   └── Dockerfile                  # ARM64 multi-stage build
│
├── lambda/                         # Lambda functions
│   ├── router/
│   │   └── index.py                # Channel webhook → AgentCore
│   ├── cron/
│   │   └── index.py                # EventBridge → AgentCore
│   └── token_metrics/
│       └── index.py                # Token usage analytics
│
├── stacks/                         # CDK stacks
│   ├── vpc_stack.py
│   ├── security_stack.py
│   ├── guardrails_stack.py
│   ├── agentcore_stack.py
│   ├── router_stack.py
│   ├── cron_stack.py
│   ├── observability_stack.py
│   └── token_monitoring_stack.py
│
├── scripts/
│   ├── deploy.sh                   # Three-phase deploy
│   ├── setup_telegram.sh           # Telegram bot setup
│   └── setup_slack.sh              # Slack app setup
│
├── docs/
│   ├── ARCHITECTURE.md             # System architecture
│   ├── MIGRATION_PLAN.md           # This document
│   ├── DEPLOYMENT_GUIDE.md         # Step-by-step deployment
│   └── AGENTCORE_CONTRACT.md       # Contract protocol spec
│
└── tests/
    ├── test_contract.py
    ├── test_workspace_sync.py
    └── test_router.py
```
