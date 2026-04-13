# Architecture: Hermes-Agent on Amazon Bedrock AgentCore

> System architecture for hosting hermes-agent as a managed AgentCore runtime.

## Overview

This project deploys [hermes-agent](https://github.com/nousresearch/hermes-agent) (a general-purpose self-improving AI agent) on **Amazon Bedrock AgentCore**, following the same pattern as [sample-host-openclaw-on-amazon-bedrock-agentcore](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore).

AgentCore provides per-user **Firecracker microVMs** that run a containerized agent runtime. Each user gets an isolated container with its own filesystem, credentials, and session lifecycle.

---

## High-Level Architecture

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                        User Channels                            │
  │  Telegram  Discord  Slack  WhatsApp  Matrix  Web UI  DingTalk   │
  └──────┬────────┬───────┬──────┬────────┬───────┬────────┬────────┘
         │        │       │      │        │       │        │
         └────────┴───────┴──────┴────────┴───────┴────────┘
                                 │
                    ┌────────────▼────────────┐
                    │     API Gateway          │
                    │  (HTTP API, per-channel  │
                    │   webhook routes)        │
                    └────────────┬─────────────┘
                                │
                    ┌────────────▼────────────┐
                    │    Router Lambda         │  ← Webhook validation
                    │    (Python 3.13)         │    Identity resolution
                    │                          │    Session management
                    │    + DynamoDB            │    Async dispatch
                    │      (identity table)    │
                    └────────────┬─────────────┘
                                │
                    InvokeAgentRuntime(
                      runtimeArn, qualifier,
                      runtimeSessionId,
                      runtimeUserId, payload)
                                │
  ┌─────────────────────────────▼─────────────────────────────────┐
  │              Amazon Bedrock AgentCore                          │
  │                                                                │
  │   ┌──────────────────────────────────────────────────────┐    │
  │   │          Per-User Firecracker MicroVM                 │    │
  │   │                                                       │    │
  │   │  ┌─────────────────────────────────────────────┐     │    │
  │   │  │  Contract Server (port 8080)                 │     │    │
  │   │  │  GET  /ping        → health check            │     │    │
  │   │  │  POST /invocations → message dispatch        │     │    │
  │   │  └──────────┬──────────────────────────────────┘     │    │
  │   │             │                                         │    │
  │   │    ┌────────▼────────┐    ┌──────────────────┐       │    │
  │   │    │  Warm-up Agent  │    │  hermes-agent    │       │    │
  │   │    │  (lightweight,  │───►│  (full agent,    │       │    │
  │   │    │   fast startup) │    │   40+ tools)     │       │    │
  │   │    └────────┬────────┘    └──────┬───────────┘       │    │
  │   │             │                     │                   │    │
  │   │    ┌────────▼─────────────────────▼──────────┐       │    │
  │   │    │         LLM Provider Layer              │       │    │
  │   │    │  litellm (Bedrock ConverseStream +      │       │    │
  │   │    │          external API fallback)          │       │    │
  │   │    └─────────────────┬───────────────────────┘       │    │
  │   │                      │                                │    │
  │   │    ┌─────────────────▼───────────────────────┐       │    │
  │   │    │         State Layer                      │       │    │
  │   │    │  /mnt/workspace/.hermes/                 │       │    │
  │   │    │    ├── state.db  (SQLite + FTS5)         │       │    │
  │   │    │    ├── memories/ (MEMORY.md, USER.md)    │       │    │
  │   │    │    ├── skills/   (learned skills)        │       │    │
  │   │    │    └── config.yaml                       │       │    │
  │   │    │                                          │       │    │
  │   │    │  Workspace Sync (↔ S3 every 5 min)       │       │    │
  │   │    └──────────────────────────────────────────┘       │    │
  │   │                                                       │    │
  │   └───────────────────────────────────────────────────────┘    │
  │                                                                │
  └────────────────────────────────────────────────────────────────┘
                    │                           │
       ┌────────────▼──────────┐   ┌────────────▼──────────┐
       │  Amazon Bedrock       │   │  S3 User Files        │
       │  ConverseStream API   │   │  {user_ns}/           │
       │  + Guardrails         │   │    .hermes/state.db   │
       └───────────────────────┘   │    .hermes/memories/  │
                                   │    .hermes/skills/    │
       ┌───────────────────────┐   └───────────────────────┘
       │  EventBridge Scheduler│
       │  + Cron Lambda        │   ┌───────────────────────┐
       │  (replaces hermes     │   │  CloudWatch           │
       │   cron daemon)        │   │  Dashboards + Alarms  │
       └───────────────────────┘   └───────────────────────┘
```

---

## Component Breakdown

### 1. Contract Server (`bridge/contract.py`)

The only interface AgentCore requires from the container:

| Endpoint | Method | Request | Response |
|----------|--------|---------|----------|
| `/ping` | GET | — | `{"status": "Healthy"}` or `{"status": "HealthyBusy"}` |
| `/invocations` | POST | `{action, userId, actorId, channel, message, images?}` | `{response, metadata?}` |

**Actions:**

| Action | Purpose | Handler |
|--------|---------|---------|
| `chat` | User message → agent response | `handle_chat()` → warm-up or full agent |
| `warmup` | Pre-warm container (trigger lazy init) | `ensure_agent()` |
| `cron` | Scheduled task execution | `handle_cron()` |
| `status` | Container diagnostics | Return health + uptime + memory |

**Health States:**
- `Healthy` — idle, ready for requests
- `HealthyBusy` — processing a request (prevents premature idle termination)

### 2. Dual-Agent Warm-up Pattern

Python + hermes-agent dependency loading takes **10-30 seconds**. During this time, a lightweight agent handles messages:

```
Timeline:
  0s      Contract server starts (fast, minimal deps)
  0-2s    Lightweight agent ready (boto3 + basic tools)
  2-30s   Messages handled by lightweight agent
  10-30s  Full hermes-agent loaded (AIAgent class ready)
  30s+    Messages handled by full hermes-agent
```

**Lightweight agent capabilities:**
- Web search, web fetch
- Memory read (from pre-loaded files)
- Simple Q&A via Bedrock direct call
- "I'm still warming up" transparency for complex tool requests

**Transition:**
- Contract server tracks `agent_ready: bool`
- Once `AIAgent` is initialized, all subsequent requests go to full agent
- In-flight lightweight responses are completed before switching

### 3. Router Lambda

Replaces hermes-agent's in-process channel adapters (`gateway/platforms/`):

```python
def handler(event, context):
    # 1. Parse webhook (Telegram/Slack/Discord/etc.)
    # 2. Validate signature/token
    # 3. Return 200 immediately (async processing)
    # 4. Resolve user identity (DynamoDB lookup)
    # 5. Get or create AgentCore session
    # 6. InvokeAgentRuntime(payload)
    # 7. Send response back to channel API
```

**Why Lambda instead of in-container channels:**
- Webhooks need fast response (Telegram: <3s, Slack: <3s)
- Container may be cold (30s startup)
- Per-user isolation means container doesn't have all users' tokens
- Lambda handles fan-out naturally

### 4. State Persistence Layer

```
Container filesystem (/mnt/workspace)
        ↕ symlink
    ~/.hermes/
        ↕ periodic sync (5 min)
    S3 bucket (s3://{bucket}/{user_namespace}/)
        ↕ restore on container init
    Container filesystem (next session)
```

**Critical writes trigger immediate S3 sync:**
- Skill creation/update
- Memory file writes
- Session database after conversation end

**SIGTERM handler:**
- AgentCore sends SIGTERM before container termination
- Handler triggers final S3 backup of all state
- Graceful shutdown of SQLite connections

### 5. LLM Provider Layer

hermes-agent supports 200+ models. On AgentCore, model routing:

| Model Request | Route | Protocol |
|---------------|-------|----------|
| `claude-*` (Anthropic) | Bedrock ConverseStream | IAM auth, no API key |
| `gpt-*` (OpenAI) | NAT → OpenAI API | API key from Secrets Manager |
| `gemini-*` (Google) | NAT → Google API | API key from Secrets Manager |
| Local / custom | NAT → configured endpoint | API key from Secrets Manager |

**Implementation:** `litellm` as the unified proxy layer. hermes-agent already uses OpenAI-compatible format; litellm translates to Bedrock ConverseStream natively.

### 6. Credential Isolation

```python
# Per-user STS scoped credentials
sts.assume_role(
    RoleArn=execution_role_arn,
    RoleSessionName=f"hermes-{user_namespace[:32]}",
    Policy=json.dumps({
        "Statement": [{
            "Effect": "Allow",
            "Action": ["s3:*"],
            "Resource": [
                f"arn:aws:s3:::{bucket}/{user_namespace}/*"
            ]
        }]
    }),
    DurationSeconds=3600
)
# Refresh every 45 minutes
```

Each container can only access its own user's S3 namespace. Combined with Firecracker VM isolation, this provides defense-in-depth.

---

## Data Flow: End-to-End Message

```
1. User sends "research quantum computing" on Telegram
2. Telegram → webhook → API Gateway → Router Lambda
3. Lambda validates bot token signature
4. Lambda queries DynamoDB: CHANNEL#telegram:12345 → USER#user_abc
5. Lambda calls InvokeAgentRuntime(sessionId="user_abc:tg:uuid-xxx", payload={...})
6. AgentCore routes to existing microVM or spins up new one
7. Contract server receives POST /invocations {action:"chat", message:"research quantum computing"}
8. Contract server sets health → HealthyBusy
9. If agent not ready → lightweight agent responds with basic search
   If agent ready → AIAgent.run_conversation() executes:
     a. Builds system prompt (SOUL.md + context)
     b. Calls Bedrock Claude via litellm
     c. Model returns tool_calls: [web_search, web_fetch]
     d. Tools execute, results appended
     e. Model synthesizes final response
10. Response returned to contract server
11. Contract server sets health → Healthy
12. Response returned to Lambda via InvokeAgentRuntime response
13. Lambda calls Telegram sendMessage API
14. User sees response in Telegram
```

---

## AWS Service Map

| Service | Purpose | Estimated Cost (10 users/mo) |
|---------|---------|------------------------------|
| **Bedrock AgentCore** | Per-user Firecracker microVMs | $50-150 |
| **Bedrock (Claude)** | LLM inference via ConverseStream | $100-500 |
| **Bedrock Guardrails** | Content filtering, PII redaction | $5-20 |
| **ECR** | Container image registry (ARM64) | $1-3 |
| **VPC** | 2-AZ, private/public subnets, NAT | $30-45 |
| **API Gateway** | HTTP API for channel webhooks | $3-5 |
| **Lambda** | Router + Cron executor + Token monitor | $5-10 |
| **DynamoDB** | Identity table, session state | $5-10 |
| **S3** | User workspace persistence | $1-5 |
| **Secrets Manager** | API keys, bot tokens (7+ secrets) | $2-5 |
| **KMS** | Customer-managed encryption key | $1 |
| **EventBridge Scheduler** | Cron job scheduling | $1-3 |
| **CloudWatch** | Logs, metrics, dashboards, alarms | $5-10 |
| **SNS** | Alarm notifications | $0-1 |
| **Cognito** | Web UI authentication (optional) | $0-5 |
| **Total** | | **~$210-770/month** |

---

## Comparison: hermes-agent Standalone vs AgentCore

| Aspect | Standalone (current) | AgentCore (target) |
|--------|----------------------|---------------------|
| Isolation | Single process, all users | Per-user Firecracker microVM |
| Scaling | Manual (add servers) | Automatic (containers spin up/down) |
| State | Local SQLite + files | S3-backed SQLite + workspace sync |
| Channels | In-process adapters | Router Lambda + API Gateway |
| LLM | 200+ models via API keys | Bedrock primary + external via NAT |
| Cron | In-process daemon | EventBridge Scheduler + Lambda |
| Security | Config-based allowlist | IAM + STS + VPC + KMS |
| Cost model | Fixed (server running 24/7) | Pay-per-use (idle → terminated) |
| Cold start | None (always running) | 10-30s (mitigated by warm-up agent) |
| Browser | Local Playwright | AgentCore Browser API (managed Chromium) |
| Deployment | Docker / systemd / SSH | CDK + AgentCore Toolkit |

---

## Key Design Decisions

### 1. litellm over custom Bedrock proxy
The sample uses a Node.js proxy (`agentcore-proxy.js`) to translate OpenAI-compatible → Bedrock ConverseStream. For hermes-agent (Python), `litellm` provides the same translation natively and supports fallback to non-Bedrock models. This avoids maintaining a separate proxy process.

### 2. SQLite kept (not replaced with DynamoDB)
hermes-agent's session storage uses SQLite with FTS5 full-text search. Replacing this with DynamoDB would require rewriting the entire session/search layer. Instead, we keep SQLite locally and sync to S3 — same pattern as the sample's `workspace-sync.js`.

### 3. Channels extracted to Lambda (not kept in container)
hermes-agent's platform adapters (Telegram, Discord, Slack, etc.) use long-running connections (polling, WebSocket). These don't fit the per-user container model. Lambda webhook handlers are stateless and respond within Telegram's 3-second requirement.

### 4. ARM64 (Graviton) target
AgentCore runs on Graviton (ARM64). This is ~20% cheaper than x86 and the sample mandates ARM64 builds. hermes-agent's Python code is architecture-agnostic; only native dependencies (SQLite, Playwright) need ARM64 builds.

### 5. Warm-up agent is mandatory (not optional)
Python cold start + dependency loading + S3 restore = 10-30 seconds. Without a warm-up agent, the first message in a new session would time out. The lightweight agent provides immediate responsiveness while the full agent loads in the background.
