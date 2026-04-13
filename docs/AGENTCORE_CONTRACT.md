# AgentCore Contract Protocol Specification

> Technical specification for the HTTP contract between Amazon Bedrock AgentCore and the hermes-agent container.

## Overview

Amazon Bedrock AgentCore communicates with hosted containers via a simple HTTP contract on **port 8080**. The container must implement two endpoints:

1. `GET /ping` — Health check (polled regularly by AgentCore)
2. `POST /invocations` — Message dispatch (called when a user sends a message)

This is the **only** interface AgentCore requires. Everything else (LLM calls, tool execution, state management) happens inside the container.

---

## Health Check: `GET /ping`

### Request
```
GET /ping HTTP/1.1
Host: localhost:8080
```

### Response

```json
{"status": "Healthy"}
```

or

```json
{"status": "HealthyBusy"}
```

### Status Values

| Status | Meaning | AgentCore Behavior |
|--------|---------|-------------------|
| `Healthy` | Container is idle, ready for requests | May terminate after idle timeout |
| `HealthyBusy` | Container is actively processing | Will NOT terminate (resets idle timer) |

### Timing
- AgentCore polls `/ping` at regular intervals (~10s)
- First successful `/ping` marks the container as ready
- Container must respond within 5 seconds

### Implementation Notes
- Return `HealthyBusy` during:
  - Active `/invocations` request processing
  - Full agent initialization (background loading)
  - S3 workspace restore
  - Skill execution
- Return `Healthy` when idle
- **Critical**: If you always return `Healthy` during processing, AgentCore may terminate the container mid-request after idle timeout

---

## Invocation: `POST /invocations`

### Request

```
POST /invocations HTTP/1.1
Host: localhost:8080
Content-Type: application/json
Content-Length: ...
```

```json
{
  "action": "chat",
  "userId": "user_abc123def456",
  "actorId": "telegram:987654321",
  "channel": "telegram",
  "chatId": "123456789",
  "message": "Hello, what can you do?",
  "images": [
    {
      "s3Key": "user_abc123def456/uploads/image_001.jpg",
      "contentType": "image/jpeg"
    }
  ],
  "metadata": {
    "threadId": "thread_xyz",
    "replyToMessageId": "msg_456"
  }
}
```

### Actions

#### `chat` — User Message

The primary action. User sends a message, agent responds.

**Request fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | string | Yes | `"chat"` |
| `userId` | string | Yes | Hermes user ID (from DynamoDB identity table) |
| `actorId` | string | Yes | Channel-specific user ID (e.g., `telegram:123456`) |
| `channel` | string | Yes | Source channel (`telegram`, `slack`, `discord`, etc.) |
| `chatId` | string | No | Channel-specific chat/conversation ID |
| `message` | string | Yes | User's text message |
| `images` | array | No | Uploaded images (S3 references) |
| `metadata` | object | No | Channel-specific metadata (thread ID, reply context) |

**Response:**

```json
{
  "response": "I can help with many tasks! I have over 40 tools including...",
  "metadata": {
    "model": "claude-opus-4-6",
    "tokens_used": 1523,
    "tools_called": ["web_search"],
    "processing_time_ms": 4521
  }
}
```

#### `warmup` — Pre-warm Container

Sent by AgentCore or the Router Lambda to pre-initialize the container without a user message.

**Request:**
```json
{
  "action": "warmup",
  "userId": "user_abc123def456"
}
```

**Response:**
```json
{
  "status": "ready"
}
```

#### `cron` — Scheduled Task

Sent by the Cron Lambda via EventBridge Scheduler.

**Request:**
```json
{
  "action": "cron",
  "userId": "user_abc123def456",
  "jobId": "job_daily_summary",
  "config": {
    "prompt": "Summarize today's news about AI",
    "delivery": {
      "channel": "telegram",
      "chatId": "123456789"
    }
  }
}
```

**Response:**
```json
{
  "response": "Here's today's AI news summary:\n1. ...",
  "jobId": "job_daily_summary",
  "deliveryStatus": "pending"
}
```

#### `status` — Container Diagnostics

**Request:**
```json
{
  "action": "status"
}
```

**Response:**
```json
{
  "agent_ready": true,
  "uptime_seconds": 3456,
  "memory_usage_mb": 512,
  "active_tools": ["web_search", "terminal"],
  "workspace_last_sync": "2026-04-13T10:30:00Z",
  "model": "claude-opus-4-6",
  "hermes_version": "3.2.0"
}
```

---

## Session Management

### Session IDs

AgentCore uses `runtimeSessionId` to route requests to the correct container. Key constraints:

| Constraint | Value |
|------------|-------|
| Minimum length | **33 characters** |
| Character set | Alphanumeric + `:` + `-` + `_` |
| Uniqueness | Per user, per channel context |

**Recommended format:**
```
{userId}:{channel}:{uuid}
```

Example: `user_abc123def456:telegram:550e8400-e29b-41d4-a716`

### Session Lifecycle

```
1. First request for a user → AgentCore creates new microVM
2. Container starts → entrypoint.sh → contract.py
3. /ping returns Healthy → AgentCore marks container ready
4. Subsequent requests → routed to existing container
5. No requests for {idle_timeout} → AgentCore sends SIGTERM
6. Container saves state to S3 → exits
7. Next request → new container, restore from S3
```

### Idle Timeout

| Setting | Default | Description |
|---------|---------|-------------|
| `session_idle_timeout` | 1800s (30 min) | Time after last request before SIGTERM |
| `session_max_lifetime` | 28800s (8 hours) | Maximum container lifetime |

---

## Container Requirements

### Port
- Must listen on **port 8080** (hardcoded by AgentCore)

### Architecture
- **ARM64** (linux/arm64) — AgentCore runs on Graviton

### Health Check Timing
- First `/ping` must succeed within **60 seconds** of container start
- If the container fails to respond within 60s, AgentCore kills it

### Graceful Shutdown
- AgentCore sends **SIGTERM** before termination
- Container has **10 seconds** to save state and exit
- After 10s, **SIGKILL** is sent

### Filesystem

| Path | Behavior | Use |
|------|----------|-----|
| `/mnt/workspace` | Persists across session stop/resume | Primary state storage |
| `/mnt/workspace` | **Wiped** on container image update | Must backup to S3 |
| `/tmp` | Ephemeral | Temporary files |

### Environment Variables

Set by AgentCore at container start:

| Variable | Description |
|----------|-------------|
| `AWS_REGION` | AWS region |
| `AWS_DEFAULT_REGION` | Same as above |
| (IAM role credentials) | Available via instance metadata |

Set by our entrypoint:

| Variable | Description |
|----------|-------------|
| `HERMES_HEADLESS` | `1` — disable in-process channels |
| `AGENTCORE_MODE` | `1` — enable AgentCore-specific behavior |
| `S3_BUCKET` | User files bucket name |
| `AGENTCORE_USER_NAMESPACE` | Per-user S3 prefix |
| `BEDROCK_MODEL_ID` | Default Bedrock model |
| `PORT` | `8080` |

---

## SDK Usage (Caller Side)

The Router Lambda calls AgentCore using the `bedrock-agentcore` SDK:

```python
import boto3
import json

agentcore = boto3.client("bedrock-agentcore")

response = agentcore.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock:us-east-1:123456789:agent-runtime/hermes_agent",
    qualifier="production",
    runtimeSessionId="user_abc123def456:telegram:550e8400-e29b-41d4",
    runtimeUserId="telegram:987654321",
    payload=json.dumps({
        "action": "chat",
        "userId": "user_abc123def456",
        "actorId": "telegram:987654321",
        "channel": "telegram",
        "message": "Hello!",
    }),
    contentType="application/json",
    accept="application/json",
)

result = json.loads(response["payload"].read())
print(result["response"])
```

---

## Error Handling

### Container-side errors

Return appropriate HTTP status codes:

| Status | When |
|--------|------|
| 200 | Success |
| 400 | Invalid action or missing required fields |
| 500 | Internal error (agent crash, LLM failure) |
| 503 | Agent not ready and no warm-up agent available |

### Error response format

```json
{
  "error": "Agent initialization failed",
  "code": "INIT_ERROR",
  "details": "Failed to restore SQLite database from S3"
}
```

### Retry behavior

AgentCore may retry failed invocations. The contract server should be idempotent:
- Chat messages are inherently non-idempotent (sending the same message twice is OK)
- Cron jobs should track execution in DynamoDB to prevent double-execution
- Warmup is naturally idempotent

---

## Sequence Diagrams

### First Message (Cold Start)

```
Router Lambda          AgentCore           Container
    │                      │                    │
    │  InvokeAgentRuntime  │                    │
    │─────────────────────►│                    │
    │                      │  Create microVM    │
    │                      │───────────────────►│
    │                      │                    │  entrypoint.sh
    │                      │                    │  ├── S3 restore
    │                      │                    │  ├── Start contract.py
    │                      │   GET /ping        │  ├── Init warm-up agent
    │                      │───────────────────►│  │
    │                      │   {"Healthy"}      │  │
    │                      │◄───────────────────│  │
    │                      │                    │  └── Background: load full agent
    │                      │  POST /invocations │
    │                      │───────────────────►│
    │                      │                    │  Warm-up agent handles
    │                      │   {"response":...} │
    │                      │◄───────────────────│
    │  response            │                    │
    │◄─────────────────────│                    │
```

### Subsequent Message (Warm)

```
Router Lambda          AgentCore           Container
    │                      │                    │
    │  InvokeAgentRuntime  │                    │
    │─────────────────────►│                    │
    │                      │  POST /invocations │
    │                      │───────────────────►│
    │                      │                    │  Full hermes-agent handles
    │                      │                    │  ├── Build prompt
    │                      │                    │  ├── Call Bedrock (litellm)
    │                      │                    │  ├── Execute tools
    │                      │                    │  └── Return response
    │                      │   {"response":...} │
    │                      │◄───────────────────│
    │  response            │                    │
    │◄─────────────────────│                    │
```

### Container Shutdown

```
AgentCore              Container
    │                      │
    │  (idle timeout)      │
    │                      │
    │  SIGTERM             │
    │─────────────────────►│
    │                      │  _sigterm_handler()
    │                      │  ├── Stop periodic sync
    │                      │  ├── Final S3 backup
    │                      │  │   ├── SQLite hot-copy → S3
    │                      │  │   ├── Memory files → S3
    │                      │  │   └── Skills → S3
    │                      │  └── Exit(0)
    │                      │
    │  (container stopped) │
```
