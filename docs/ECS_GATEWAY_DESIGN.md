# ECS Fargate Gateway: WeChat + Feishu (Optional Phase 4)

> **Status**: Implemented (CDK + container code ready, pending deployment)
> **Updated**: 2026-04-15
> **Approach**: Run hermes-agent's native gateway on ECS; agent execution remains on AgentCore microVMs

---

## 1. Overview

```
  WeChat iLink (long-poll)  ──┐
                               ├──►  ECS Fargate                    ──►  AgentCore microVM
  Feishu/Lark (WebSocket)   ──┘     hermes-agent gateway                 hermes-agent (AI)
                                    (protocol only, no AI logic)          (per-user isolation)
```

- **Gateway (ECS)**: Runs hermes-agent's native gateway module — handles WeChat long-poll and Feishu WebSocket protocols
- **Agent (AgentCore)**: Existing AgentCore deployment unchanged — per-user Firecracker microVM isolation
- **Bridge**: Monkey-patch `AIAgent` → `AgentCoreProxyAgent`, converting `run_conversation()` calls into `invoke_agent_runtime()` API calls

### Why reuse hermes-agent's native gateway

| Dimension | Custom thin gateway | **Reuse hermes-agent gateway** |
|-----------|--------------------|---------------------------------|
| WeChat protocol code | Rewrite ~1,800 lines | **0 lines** (use `weixin.py` as-is) |
| Feishu protocol code | Rewrite ~3,950 lines | **0 lines** (use `feishu.py` as-is) |
| Features | Message forwarding only | Typing indicators, media encryption, message splitting, dedup, group policies |
| Maintenance | Must track upstream protocol changes | `git pull` to sync upstream |
| New code required | ~2,000 lines | **~100 lines** (proxy agent only) |

---

## 2. Architecture

```
  ┌────────────────────────────────────────────────────────────┐
  │                      User Channels                          │
  │                                                            │
  │  Telegram  Discord  Slack       WeChat iLink   Feishu/Lark │
  │  (webhook) (webhook) (webhook)  (long-poll)    (WebSocket)  │
  └─────┬────────┬───────┬──────────────┬──────────────┬───────┘
        │        │       │              │              │
        │   Existing Phase 1-3          │   New Phase 4
        ▼        ▼       ▼              ▼              ▼
  ┌──────────────────┐    ┌─────────────────────────────────────┐
  │  API Gateway     │    │  ECS Fargate                         │
  │  + Router Lambda │    │  hermes-agent gateway                │
  │                  │    │                                      │
  │                  │    │  ┌──────────┐  ┌──────────────────┐ │
  │                  │    │  │ weixin.py │  │ feishu.py        │ │
  │                  │    │  │ long-poll │  │ WebSocket        │ │
  │                  │    │  └─────┬────┘  └──────┬───────────┘ │
  │                  │    │        └──────┬───────┘             │
  │                  │    │               ▼                     │
  │                  │    │  ┌──────────────────────────────┐   │
  │                  │    │  │ AgentCoreProxyAgent          │   │
  │                  │    │  │ (monkey-patch replaces       │   │
  │                  │    │  │  AIAgent)                    │   │
  │                  │    │  │                              │   │
  │                  │    │  │ run_conversation(msg)        │   │
  │                  │    │  │   → invoke_agent_runtime()   │   │
  │                  │    │  │   → parse SSE response       │   │
  │                  │    │  └──────────────┬───────────────┘   │
  └────────┬─────────┘    └────────────────┼───────────────────┘
           │                               │
           ▼                               ▼
  ┌──────────────────────────────────────────────────────────────┐
  │                invoke_agent_runtime()                         │
  │                (agentRuntimeArn, sessionId, userId, payload)  │
  └──────────────────────────┬───────────────────────────────────┘
                             │
  ┌──────────────────────────▼───────────────────────────────────┐
  │                Amazon Bedrock AgentCore                       │
  │                                                               │
  │   Per-User Firecracker microVM                                │
  │   ┌─────────────────────────────────────────────────────┐    │
  │   │  contract.py → hermes-agent AIAgent (40+ tools)      │    │
  │   │  Monkey-patch → AnthropicBedrock (SigV4)             │    │
  │   │  /mnt/workspace/.hermes/ (S3 sync)                   │    │
  │   └─────────────────────────────────────────────────────┘    │
  └───────────────────────────────────────────────────────────────┘
```

**Both paths share the same AgentCore Runtime**:
- Telegram/Slack/Discord → Lambda → `invoke_agent_runtime()`
- WeChat/Feishu → ECS gateway → `invoke_agent_runtime()`

---

## 3. Core Component: AgentCoreProxyAgent

The hermes-agent gateway calls `agent.run_conversation(message)` at `gateway/run.py:8097`. We monkey-patch `AIAgent` with a proxy that forwards the call to AgentCore.

```python
# agentcore_proxy.py (~100 lines)

import json, os, time, logging, boto3
from botocore.exceptions import ClientError

RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "")

_client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-west-2"))
logger = logging.getLogger("agentcore_proxy")


class AgentCoreProxyAgent:
    """Drop-in replacement for AIAgent — forwards to AgentCore."""

    def __init__(self, *, session_id="", platform="", user_id="", **kwargs):
        # Ignore AIAgent's other params (model, tools, etc.)
        # Those are handled by the real AIAgent inside AgentCore.
        self.session_id = session_id
        self.platform = platform
        self.user_id = user_id

        # AgentCore session_id must be >= 33 characters
        self._ac_session_id = f"{platform}__{user_id}__{session_id}"
        if len(self._ac_session_id) < 33:
            self._ac_session_id += ":" + "0" * (33 - len(self._ac_session_id) - 1)
        self._ac_session_id = self._ac_session_id[:128]
        self._ac_user_id = f"{platform}:{user_id}"

    def run_conversation(self, message, conversation_history=None, task_id=None):
        """Call invoke_agent_runtime() and return an AIAgent-compatible result dict."""
        payload = json.dumps({
            "action": "chat",
            "message": message,
            "userId": self._ac_user_id,
            "channel": self.platform,
        })

        kwargs = {
            "agentRuntimeArn": RUNTIME_ARN,
            "runtimeSessionId": self._ac_session_id,
            "runtimeUserId": self._ac_user_id,
            "payload": payload,
        }
        if QUALIFIER:
            kwargs["qualifier"] = QUALIFIER

        text = self._invoke_with_retry(**kwargs)

        return {
            "final_response": text,
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": text},
            ],
            "api_calls": 1,
        }

    def _invoke_with_retry(self, max_retries=2, **kwargs):
        for attempt in range(max_retries + 1):
            try:
                response = _client.invoke_agent_runtime(**kwargs)
                return _parse_sse_response(response)
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("ThrottlingException", "ServiceUnavailableException") and attempt < max_retries:
                    time.sleep(2 ** attempt)
                    continue
                logger.error("AgentCore invocation failed: %s", e)
                return "Sorry, the service is temporarily busy. Please try again."
        return "Sorry, the service is temporarily busy. Please try again."

    # Attributes the gateway may access — safe defaults
    context_compressor = None
    session_prompt_tokens = 0
    session_completion_tokens = 0
    model = "agentcore-proxy"
    tools = []


def _parse_sse_response(response):
    """Parse AgentCore SSE response (data: "..." format)."""
    chunks = []
    result = response.get("response", "")
    if hasattr(result, "read"):
        result = result.read()
    if isinstance(result, bytes):
        result = result.decode("utf-8")
    result = result.strip()
    if result.startswith("data: "):
        result = result[6:]
    if result.startswith('"') and result.endswith('"'):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            pass
    return result


def patch_aiagent():
    """Call before gateway starts — replaces AIAgent with AgentCore proxy."""
    import run_agent
    run_agent.AIAgent = AgentCoreProxyAgent
```

### Entry point

```python
# main.py (ECS container entry)
from agentcore_proxy import patch_aiagent
patch_aiagent()

# Start hermes-agent gateway normally
from gateway.run import start_gateway
start_gateway()
```

The gateway code requires **zero modifications** — when it calls `AIAgent(session_id=..., platform=...)`, it gets an `AgentCoreProxyAgent` instead, and `run_conversation()` is transparently forwarded to AgentCore.

---

## 4. Message Flow

### 4.1 WeChat

```
WeChat User Alice        ECS Gateway                     AgentCore microVM
    │                        │                               │
    │  Send "Hello"          │                               │
    │                        │                               │
    │                  weixin.py long-poll:                   │
    │                  getupdates → receive message           │
    │                  parse from_user_id, text               │
    │                  send typing indicator                  │
    │                        │                               │
    │                  AgentCoreProxyAgent                    │
    │                  .run_conversation("Hello")             │
    │                        │                               │
    │                        │  invoke_agent_runtime(         │
    │                        │    ARN, sessionId, userId,     │
    │                        │    {action:"chat", message})   │
    │                        │ ─────────────────────────────► │
    │                        │                               │
    │                        │         hermes-agent executes  │
    │                        │         Bedrock Claude LLM     │
    │                        │         tool calls, memory     │
    │                        │                               │
    │                        │ ◄──── SSE response             │
    │                        │                               │
    │                  parse response                         │
    │                  weixin.py sendmessage()                │
    │                  (context_token, message splitting)     │
    │  ◄── receive reply     │                               │
```

### 4.2 Feishu

```
Feishu User Bob          ECS Gateway                     AgentCore microVM
    │                        │                               │
    │  @bot Hello            │                               │
    │                        │                               │
    │                  feishu.py WebSocket:                   │
    │                  receive im.message.receive_v1         │
    │                  parse open_id, chat_id, text          │
    │                  check group policy                    │
    │                        │                               │
    │                  AgentCoreProxyAgent                    │
    │                  .run_conversation("Hello")             │
    │                        │                               │
    │                        │  invoke_agent_runtime()        │
    │                        │ ─────────────────────────────► │
    │                        │ ◄──── SSE response             │
    │                        │                               │
    │                  feishu.py send rich text message       │
    │  ◄── receive reply     │                               │
```

### 4.3 Comparison with Lambda path

```
Lambda path (Telegram, etc.):
  webhook → Lambda handler → _invoke_agentcore() → AgentCore
  (Lambda implements message parsing and response delivery)

ECS Gateway path (WeChat, Feishu):
  platform → hermes-agent adapter → AgentCoreProxyAgent.run_conversation() → AgentCore
  (hermes-agent's native adapter handles all platform protocol)
```

Both paths call the same `invoke_agent_runtime()` API and share the same AgentCore Runtime.

---

## 5. Container Design

### 5.1 Dockerfile

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git libsqlite3-0 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install hermes-agent (gateway module requires full install)
COPY hermes-agent/ /app/hermes-agent/
RUN pip install --no-cache-dir /app/hermes-agent[cron,mcp]

# Feishu SDK + WeChat dependencies
RUN pip install --no-cache-dir lark-oapi aiohttp cryptography

# AgentCore invocation
RUN pip install --no-cache-dir boto3

# Proxy agent bridge
COPY agentcore_proxy.py /app/agentcore_proxy.py
COPY main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app:/app/hermes-agent
ENV HERMES_HOME=/opt/data
ENV HERMES_HEADLESS=1

CMD ["python", "/app/main.py"]
```

**Note**: Although AI inference runs in AgentCore, hermes-agent must be fully installed because the gateway module has import dependencies on the agent module. No LLM calls are made from the ECS container.

### 5.2 Process Model

```
main.py
  │
  ├── patch_aiagent()  ← monkey-patch AIAgent → AgentCoreProxyAgent
  │
  └── start_gateway()  ← hermes-agent native gateway startup
        │
        ├── WeixinAdapter (if WEIXIN_TOKEN is set)
        │     └── Long-poll loop (35s timeout)
        │
        ├── FeishuAdapter (if FEISHU_APP_ID is set)
        │     └── WebSocket client (lark-oapi SDK, auto-reconnect)
        │
        └── On message → AgentCoreProxyAgent.run_conversation()
                           → boto3 invoke_agent_runtime()
                           → AgentCore microVM executes
                           → return response → adapter sends
```

---

## 6. Configuration

### 6.1 Environment Variables

**Required:**

| Variable | Description |
|----------|-------------|
| `AGENTCORE_RUNTIME_ARN` | AgentCore Runtime ARN (same as Phase 3) |
| `AGENTCORE_QUALIFIER` | AgentCore Qualifier (optional) |
| `AWS_REGION` | AWS region |

**WeChat (enable on demand):**

| Variable | Description |
|----------|-------------|
| `WEIXIN_ACCOUNT_ID` | WeChat iLink account identifier |
| `WEIXIN_TOKEN` | iLink bot_token (injected from Secrets Manager) |
| `WEIXIN_DM_POLICY` | `open` (default) or `disabled` |
| `WEIXIN_ALLOWED_USERS` | Authorized user IDs (comma-separated) |

**Feishu (enable on demand):**

| Variable | Description |
|----------|-------------|
| `FEISHU_APP_ID` | Feishu application ID |
| `FEISHU_APP_SECRET` | Feishu app secret (injected from Secrets Manager) |
| `FEISHU_CONNECTION_MODE` | `websocket` (default) or `webhook` |
| `FEISHU_DOMAIN` | `feishu` (default) or `lark` (international) |
| `FEISHU_GROUP_POLICY` | `open` / `allowlist` / `disabled` |

**General:**

| Variable | Description |
|----------|-------------|
| `HERMES_HOME` | Data directory (default `/opt/data`) |
| `HERMES_HEADLESS` | `1` (non-interactive mode) |
| `GATEWAY_ALLOW_ALL_USERS` | `true` / `false` |

### 6.2 Secrets Manager

```
hermes/weixin/token          # WeChat iLink bot_token
hermes/feishu/app-secret     # Feishu App Secret
```

Reuses Secrets Manager from Phase 1. Injected via ECS Task Definition `secrets` field — credentials never touch disk.

---

## 7. AWS Infrastructure

### 7.1 CDK Stack (Phase 4)

```
┌──────────────────────────────────────────────────────────────┐
│  hermes-agentcore-gateway (new CDK Stack)                     │
│                                                               │
│  ┌─────────────┐  ┌────────────────┐  ┌───────────────────┐  │
│  │ ECS Cluster  │  │ Fargate Service │  │ CloudWatch        │  │
│  │ (reuse VPC)  │  │ desiredCount=1  │  │ Log Group         │  │
│  └──────┬───────┘  └───────┬────────┘  └───────────────────┘  │
│         │                  │                                   │
│  ┌──────▼──────────────────▼──────────┐                       │
│  │ Task Definition                    │                       │
│  │ CPU: 512  Memory: 1024            │                       │
│  │ Platform: LINUX/ARM64 (Graviton)   │                       │
│  │                                    │                       │
│  │ Container: hermes-gateway          │                       │
│  │   Image: ECR                       │                       │
│  │   Port: 8080 (health check)       │                       │
│  │                                    │                       │
│  │ Environment:                       │                       │
│  │   AGENTCORE_RUNTIME_ARN            │                       │
│  │   HERMES_HOME, HERMES_HEADLESS     │                       │
│  │                                    │                       │
│  │ Secrets (from Secrets Manager):    │                       │
│  │   WEIXIN_TOKEN, FEISHU_APP_SECRET  │                       │
│  └────────────────────────────────────┘                       │
│                                                               │
│  IAM Task Role:                                               │
│  - bedrock-agentcore:InvokeAgentRuntime (scoped to ARN)       │
│  - secretsmanager:GetSecretValue (hermes/weixin/*, feishu/*)  │
│  - logs:CreateLogStream, logs:PutLogEvents                    │
└──────────────────────────────────────────────────────────────┘
```

**Note**: The gateway does NOT need `bedrock:InvokeModel` — LLM calls are made by the AgentCore container, not the gateway.

### 7.2 Networking

```
┌─────────────────── VPC (reuse Phase 1) ──────────────────┐
│                                                           │
│  Private Subnet                                           │
│  ┌─────────────────────┐                                  │
│  │ Fargate Task         │                                  │
│  │ hermes-agent gateway │                                  │
│  │                      │                                  │
│  │ Outbound only:       │                                  │
│  │  → WeChat iLink API  (HTTPS via NAT)                   │
│  │  → Feishu WebSocket   (WSS via NAT)                    │
│  │  → AgentCore API      (VPC Endpoint, bypasses NAT)     │
│  │  → Secrets Manager    (VPC Endpoint)                   │
│  │                      │                                  │
│  │ No inbound ports     │                                  │
│  └──────────────────────┘                                  │
│                                                           │
│  NAT Gateway (reuse) → external outbound                  │
│  VPC Endpoints (reuse) → AgentCore, Secrets Manager       │
└───────────────────────────────────────────────────────────┘
```

**No inbound ports required**: Both WeChat long-poll and Feishu WebSocket are outbound-only connections. No ALB needed.

### 7.3 Relationship to Existing Phases

```
Phase 1: VPC + Security + Guardrails + IAM          ← reuse VPC, Secrets Manager
Phase 2: AgentCore Runtime + Container Build         ← reuse (same Runtime)
Phase 3: Router Lambda + API GW + DynamoDB + Cron    ← unchanged
Phase 4: ECS Gateway (WeChat + Feishu)               ← new (optional)
```

Phase 4 is an incremental, optional deployment. It depends on Phase 1 (VPC) and Phase 2 (Runtime ARN).

### 7.4 What the Gateway Does NOT Need

| Resource | Lambda Router (Phase 3) | ECS Gateway (Phase 4) | Why |
|----------|------------------------|----------------------|-----|
| API Gateway | Required | **Not needed** | No webhook ingress |
| ALB | — | **Not needed** | No inbound ports |
| DynamoDB | Required (identity table) | **Not needed** | hermes-agent has built-in allowlist |
| EFS | — | **Not needed** | State lives in AgentCore microVM |
| Bedrock permissions | Not needed | **Not needed** | LLM runs inside AgentCore |

The gateway only needs: ECS + Fargate + IAM (`InvokeAgentRuntime`) + Secrets Manager read.

---

## 8. Persistence

### 8.1 The Gateway Is Stateless

- **Conversation history**: Stored in AgentCore microVM at `/mnt/workspace/.hermes/state.db`
- **Memory, skills**: Same — managed by the AgentCore container
- **Platform credentials**: Secrets Manager (injected as env vars)

On gateway restart:
- WeChat: Re-establishes long-poll (if token not expired)
- Feishu: WebSocket auto-reconnects
- User conversation context is not lost (lives in AgentCore microVM)

### 8.2 Ephemeral Storage Is Sufficient

| Content | Required? | Approach |
|---------|-----------|----------|
| gateway.log | Optional | CloudWatch Logs |
| Session files | Optional | Ephemeral storage (OK to lose on restart) |
| config.yaml | Optional | Configured via environment variables |

**No EFS needed.** All gateway runtime state is rebuildable. Fargate's 20GB ephemeral storage is sufficient.

---

## 9. Security Analysis

### 9.1 Threat Model

| Threat | Severity | New risk? | Analysis |
|--------|----------|-----------|----------|
| **Session ID spoofing** | Medium | **No** | ECS Task Role can construct arbitrary `runtimeSessionId` — but this is the same trust level as the Lambda Router. Both rely on IAM as the trust boundary. |
| **Payload tampering in contract.py** | Low | **No** (pre-existing) | `contract.py:128` trusts `body.userId` without validating against `runtimeSessionId`. This is an existing gap in the AgentCore container, not introduced by Phase 4. |
| **Credential exposure** | Low | **No** | Secrets injected via Secrets Manager → ECS Task Definition `secrets` field. Same pattern as Lambda reading bot tokens. |
| **Network attack surface** | None | **Reduced** | ECS container has outbound-only connections. No inbound ports, no ALB, no API Gateway. Smaller attack surface than existing Phase 3. |
| **AgentCore microVM escape** | Very Low | **No** | AgentCore's Firecracker isolation is unchanged. |

### 9.2 IAM Least-Privilege Requirements

The ECS Task Role must be scoped tightly:

```python
# REQUIRED — scoped to specific Runtime ARN
iam.PolicyStatement(
    actions=[
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:InvokeAgentRuntimeForUser",
    ],
    resources=[agentcore_runtime_arn],   # NOT "*"
)

# REQUIRED — scoped to gateway-specific secrets only
iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue"],
    resources=[
        f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/weixin/*",
        f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/feishu/*",
    ],
)

# REQUIRED — CloudWatch logs
iam.PolicyStatement(
    actions=["logs:CreateLogStream", "logs:PutLogEvents"],
    resources=[log_group_arn],
)
```

**Permissions NOT granted to the gateway:**

| Permission | Why not needed |
|------------|----------------|
| `bedrock:InvokeModel` | LLM calls run inside AgentCore container |
| `dynamodb:*` | Gateway uses hermes-agent's built-in allowlist, not DynamoDB |
| `s3:*` | Workspace sync runs inside AgentCore container |
| `kms:*` | No direct encryption/decryption |
| `lambda:InvokeFunction` | No Lambda interaction |

### 9.3 Session ID Integrity

The proxy agent constructs session IDs deterministically from platform-authenticated identities:

```python
# WeChat: from_user_id is assigned by iLink API after QR login
self._ac_session_id = f"weixin__{from_user_id}__{account_id}"

# Feishu: open_id comes from Feishu SDK after OAuth
self._ac_session_id = f"feishu__{open_id}__{chat_id}"
```

These values originate from platform-side authentication (WeChat QR scan, Feishu OAuth), not from user-supplied input. The same trust model applies as the Lambda Router (which derives session IDs from platform webhook payloads).

### 9.4 Recommendations for Future Hardening

These are not required for Phase 4 launch but would improve defense-in-depth:

1. **Envelope validation in contract.py**: Extract `runtimeSessionId` from AgentCore request context and verify it matches `payload.userId`. This would prevent any caller (Lambda or ECS) from sending mismatched payloads.

2. **AgentCore session ID namespace**: Use distinct prefixes for Lambda vs Gateway sessions (e.g., `gw:feishu:...` vs `lm:telegram:...`) to prevent cross-path session collisions.

3. **CloudWatch anomaly detection**: Alert on unusual patterns like a single gateway sending requests to many distinct session IDs (possible credential compromise).

---

## 10. Breaking Change Assessment

### 10.1 Impact on Existing Resources

| Existing Resource | Modified by Phase 4? | Impact |
|-------------------|---------------------|--------|
| VPC (Phase 1) | **No** — read-only use | ECS Task deploys into existing private subnets |
| Security/KMS (Phase 1) | **No** | Reads from Secrets Manager only; no new secrets created |
| Guardrails (Phase 1) | **No** | Not involved |
| AgentCore IAM Role (Phase 1) | **No** | ECS uses its own Task Role |
| S3 Bucket (Phase 1) | **No** | Workspace sync runs inside AgentCore container |
| AgentCore Runtime (Phase 2) | **Shared** | WeChat/Feishu messages go to the same Runtime as Telegram/Slack/Discord |
| Router Lambda (Phase 3) | **No** — not modified | Completely independent |
| API Gateway (Phase 3) | **No** | Not involved |
| DynamoDB (Phase 3) | **No** | Gateway does not read/write the identity table |
| CloudWatch (Phase 1) | **New log group only** | Separate `/ecs/hermes-gateway` log group |

### 10.2 Shared AgentCore Runtime — Concurrency Risk

The Lambda Router and ECS Gateway both call `invoke_agent_runtime()` on the **same Runtime ARN**. AgentCore allocates one microVM per unique `runtimeSessionId`.

**Risk**: If AgentCore has an account-level concurrency quota on active microVMs, adding WeChat/Feishu users may compete with Telegram/Slack/Discord users for microVM slots.

**Mitigations**:
- WeChat iLink is 1:1 binding — limited concurrency (typically 1-5 accounts)
- Feishu concurrent users are bounded by the bot's user base
- AgentCore default quotas are typically generous (100+ concurrent sessions)
- Monitor via CloudWatch and request quota increase if needed

### 10.3 Files That Need Modification

Only deployment scaffolding changes — no existing resource modifications:

| File | Change |
|------|--------|
| `app.py` | Add `HermesGatewayStack` import and instantiation |
| `scripts/deploy.sh` | Add `phase4` branch |
| `scripts/teardown.sh` | Add Phase 4 cleanup |
| `stacks/gateway_stack.py` | **New file** — CDK stack definition |
| `gateway/` | **New directory** — Dockerfile, main.py, agentcore_proxy.py |

**Conclusion: Phase 4 introduces zero breaking changes to existing Phase 1-3 resources.**

---

## 11. Error Handling and Reliability

### 11.1 Built-in Platform Reliability (hermes-agent)

| Scenario | hermes-agent built-in handling |
|----------|-------------------------------|
| WeChat token expired (`errcode=-14`) | Pauses long-poll, logs error |
| Feishu WebSocket disconnect | Random delay + auto-reconnect (30-120s) |
| Message deduplication | `_recent_message_ids` set |
| Oversized messages | Auto-split at 4,000 characters |
| Network timeout | Retry with exponential backoff |

### 11.2 AgentCore Call Reliability

The `AgentCoreProxyAgent` includes retry logic with exponential backoff for transient errors (`ThrottlingException`, `ServiceUnavailableException`). After retries exhausted, returns a user-friendly error message.

### 11.3 ECS-Level Reliability

| Mechanism | Description |
|-----------|-------------|
| Fargate Service `desiredCount=1` | Auto-restarts crashed containers |
| ECS Health Check | Replaces unhealthy tasks automatically |
| CloudWatch Logs | Centralized log collection |
| CloudWatch Alarm | Monitor token expiry, AgentCore call failures |

### 11.4 WeChat Token Expiry Recovery

```
hermes-agent log: "errcode=-14"
     │
     ▼
CloudWatch Logs Metric Filter (match "errcode=-14")
     │
     ▼
CloudWatch Alarm → SNS → Notify admin
     │
     ▼
Admin obtains new token → Update Secrets Manager → Restart ECS Task
```

---

## 12. Cost Estimate

### 12.1 Phase 4 Incremental Cost

| Resource | Configuration | Monthly Cost |
|----------|--------------|-------------|
| Fargate Task | 0.5 vCPU / 1 GB RAM, ARM64, 24×7 | ~$15 |
| NAT Gateway traffic | ~2 GB/month (WeChat long-poll + Feishu WS) | ~$10 |
| CloudWatch Logs | ~1 GB/month | ~$1 |
| **Phase 4 total** | | **~$26/month** |

No EFS ($0), no ALB ($0), no DynamoDB ($0).

### 12.2 Total Cost

| Component | Monthly Cost |
|-----------|-------------|
| Phase 1-3 (existing) | $200-600 |
| **Phase 4 ECS Gateway (new)** | **$26** |
| **Total** | **$226-626** |

---

## 13. deploy.sh Extension

```bash
phase4() {
    info "=== Phase 4: ECS Gateway (WeChat + Feishu) ==="

    RUNTIME_ARN=$(jq -r '.context.agentcore_runtime_arn // empty' cdk.json)
    if [ -z "$RUNTIME_ARN" ]; then
        error "agentcore_runtime_arn not set. Run Phase 2 first."
        exit 1
    fi

    # Copy hermes-agent source into build context
    if [ ! -d "$PROJECT_DIR/gateway/hermes-agent" ]; then
        if [ ! -d "$HOME/hermes-agent" ]; then
            info "hermes-agent not found — cloning …"
            git clone https://github.com/NousResearch/hermes-agent.git "$HOME/hermes-agent"
        fi
        rsync -a --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
            "$HOME/hermes-agent/" "$PROJECT_DIR/gateway/hermes-agent/"
    fi

    # Build and push container image
    info "Building gateway container …"
    docker build -t hermes-gateway:latest -f gateway/Dockerfile gateway/

    ECR_REPO="${PROJECT_NAME}-gateway"
    # (ECR create + login + tag + push logic)

    # CDK deploy
    $CDK deploy "${PROJECT_NAME}-gateway" --require-approval never

    info "Phase 4 complete."
}
```

---

## 14. Implementation Plan

### Phase 4a — Containerization + CDK (1 day)

- [ ] Create `gateway/agentcore_proxy.py` (~100 lines proxy agent)
- [ ] Create `gateway/main.py` (patch + start gateway)
- [ ] Create `gateway/Dockerfile`
- [ ] Create `stacks/gateway_stack.py` (CDK: ECS + IAM)
- [ ] Update `app.py` — add Phase 4 stack
- [ ] Update `scripts/deploy.sh` — add `phase4` command
- [ ] Update `scripts/teardown.sh` — add Phase 4 cleanup
- [ ] Local docker verification that monkey-patch works

### Phase 4b — Integration Testing (1 day)

- [ ] Deploy to ECS
- [ ] Feishu: WebSocket connection, DM, group @mention
- [ ] WeChat: long-poll, send/receive messages, typing, message splitting
- [ ] Verify: messages processed by AgentCore (check microVM logs)
- [ ] Verify: container restart auto-recovers connections

### Phase 4c — Production Hardening (0.5 day)

- [ ] CloudWatch Dashboard + Alarms
- [ ] Documentation updates (README, DEPLOYMENT_GUIDE)

**Total: ~2.5 days**

---

## 15. Comparison of Approaches

| Dimension | This approach (native gateway + AgentCore) | Custom thin gateway + AgentCore | Full hermes-agent in ECS (no AgentCore) |
|-----------|-------------------------------------------|--------------------------------|----------------------------------------|
| New code | ~100 lines (proxy agent) | ~2,000 lines | 0 lines |
| Per-user isolation | **Yes** (AgentCore microVM) | **Yes** | **No** |
| Feature completeness | **Full** (hermes-agent native) | Message forwarding only | **Full** |
| Maintenance cost | Low (git pull to sync) | High (track protocol changes) | Low |
| Monthly cost | ~$26 | ~$29 | ~$44 |
| AgentCore cold start | Yes (10-30s first message) | Yes | No |
