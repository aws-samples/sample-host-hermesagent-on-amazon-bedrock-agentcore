# WeChat & Feishu Setup Guide (Phase 4 — ECS Gateway)

> **Prerequisite**: Phase 4 ECS Gateway deployed via `./scripts/deploy.sh phase4`

---

## 1. WeChat (iLink Bot API)

WeChat personal account integration uses Tencent's iLink Bot API. Login requires an interactive QR scan, which must be done locally before injecting credentials into ECS.

### 1.1 Local QR Login

```bash
cd ~/hermes-agent
pip install aiohttp cryptography qrcode   # if not already installed

python3 -c "
import asyncio
from gateway.platforms.weixin import qr_login
creds = asyncio.run(qr_login('$HOME/.hermes'))
if creds:
    print()
    print('=== Save these values ===')
    print(f'WEIXIN_ACCOUNT_ID={creds[\"account_id\"]}')
    print(f'WEIXIN_TOKEN={creds[\"token\"]}')
"
```

A QR code will appear in the terminal. Scan it with WeChat and confirm on your phone. On success, you will see:

```
微信连接成功，account_id=BOT_XXXXXX

=== Save these values ===
WEIXIN_ACCOUNT_ID=BOT_XXXXXX
WEIXIN_TOKEN=ey...long_token_string...
```

Save both values for the next step.

### 1.2 Inject Credentials into ECS

**Option A — Environment variables (recommended for quick testing)**

```bash
# Export current task definition
aws ecs describe-task-definition \
  --task-definition hermes-agentcore-gateway \
  --query 'taskDefinition.{
    family:family,
    taskRoleArn:taskRoleArn,
    executionRoleArn:executionRoleArn,
    networkMode:networkMode,
    containerDefinitions:containerDefinitions,
    cpu:cpu,
    memory:memory,
    runtimePlatform:runtimePlatform,
    requiresCompatibilities:requiresCompatibilities
  }' --no-cli-pager > /tmp/taskdef.json

# Add WeChat env vars to containerDefinitions[0].environment:
#   {"name": "WEIXIN_ACCOUNT_ID", "value": "<your_account_id>"}
#   {"name": "WEIXIN_TOKEN",      "value": "<your_token>"}
#
# You can use jq:
jq '.containerDefinitions[0].environment += [
  {"name":"WEIXIN_ACCOUNT_ID","value":"YOUR_ACCOUNT_ID"},
  {"name":"WEIXIN_TOKEN","value":"YOUR_TOKEN"}
]' /tmp/taskdef.json > /tmp/taskdef-updated.json

# Register new task definition revision
aws ecs register-task-definition \
  --cli-input-json file:///tmp/taskdef-updated.json \
  --no-cli-pager > /dev/null

# Deploy the new revision
aws ecs update-service \
  --cluster hermes-agentcore-gateway \
  --service hermes-agentcore-gateway \
  --force-new-deployment \
  --no-cli-pager > /dev/null

echo "ECS service updated. New task will start in ~60 seconds."
```

**Option B — Secrets Manager (recommended for production)**

```bash
aws secretsmanager create-secret \
  --name hermes/weixin/account-id \
  --secret-string 'YOUR_ACCOUNT_ID' \
  --no-cli-pager

aws secretsmanager create-secret \
  --name hermes/weixin/token \
  --secret-string 'YOUR_TOKEN' \
  --no-cli-pager
```

Then update the ECS task definition to read from Secrets Manager, or configure the gateway to fetch secrets at startup via `config.yaml`.

### 1.3 Verify Connection

```bash
# Check ECS task logs
aws logs tail /ecs/hermes-agentcore-gateway --since 5m --no-cli-pager

# Look for:
#   [weixin] Connected account=BOT_XXXX base=https://ilinkai.weixin.qq.com
```

### 1.4 Token Expiry

iLink tokens expire periodically (typically 24-72 hours). When expired, the gateway logs:

```
errcode=-14
```

To renew:
1. Re-run the QR login (Step 1.1)
2. Update the token in ECS (Step 1.2)

**Tip**: Set up a CloudWatch Logs metric filter for `errcode=-14` to get SNS alerts when the token expires.

### 1.5 WeChat Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `WEIXIN_ACCOUNT_ID` | Yes | Account ID from QR login |
| `WEIXIN_TOKEN` | Yes | Bot token from QR login |
| `WEIXIN_DM_POLICY` | No | `open` (default) or `disabled` |
| `WEIXIN_GROUP_POLICY` | No | `open` / `allowlist` / `disabled` (default) |
| `WEIXIN_ALLOWED_USERS` | No | Comma-separated user IDs for allowlist |
| `WEIXIN_SPLIT_MULTILINE_MESSAGES` | No | `true` or `false` (default) |

---

## 2. Feishu / Lark

Feishu integration uses the official Lark SDK with WebSocket mode (no inbound webhook needed).

### 2.1 Create Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn/app) (or [Lark Developer](https://open.larksuite.com/app) for international)
2. Create a new Custom App
3. Under **Credentials**, copy:
   - **App ID** (`cli_xxxxx`)
   - **App Secret**
4. Under **Bot**, enable the bot capability
5. Under **Event Subscriptions**:
   - Enable `im.message.receive_v1` (receive messages)
   - Set subscription mode to **WebSocket** (long connection)
6. Under **Permissions**, add:
   - `im:message` (send messages)
   - `im:message.receive_v1` (receive messages)
   - `im:resource` (download files/images)
7. Publish the app version and have an admin approve it

### 2.2 Inject Credentials

**Option A — Environment variables**

```bash
# Same pattern as WeChat — update ECS task definition
jq '.containerDefinitions[0].environment += [
  {"name":"FEISHU_APP_ID","value":"cli_xxxxx"},
  {"name":"FEISHU_APP_SECRET","value":"YOUR_APP_SECRET"}
]' /tmp/taskdef.json > /tmp/taskdef-updated.json

aws ecs register-task-definition \
  --cli-input-json file:///tmp/taskdef-updated.json \
  --no-cli-pager > /dev/null

aws ecs update-service \
  --cluster hermes-agentcore-gateway \
  --service hermes-agentcore-gateway \
  --force-new-deployment \
  --no-cli-pager > /dev/null
```

**Option B — Secrets Manager**

```bash
aws secretsmanager create-secret \
  --name hermes/feishu/app-id \
  --secret-string 'cli_xxxxx' \
  --no-cli-pager

aws secretsmanager create-secret \
  --name hermes/feishu/app-secret \
  --secret-string 'YOUR_APP_SECRET' \
  --no-cli-pager
```

### 2.3 Verify Connection

```bash
aws logs tail /ecs/hermes-agentcore-gateway --since 5m --no-cli-pager

# Look for:
#   [feishu] WebSocket connected (mode=websocket)
```

### 2.4 Feishu Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FEISHU_APP_ID` | Yes | Feishu App ID (`cli_xxxxx`) |
| `FEISHU_APP_SECRET` | Yes | Feishu App Secret |
| `FEISHU_DOMAIN` | No | `feishu` (default, China) or `lark` (international) |
| `FEISHU_CONNECTION_MODE` | No | `websocket` (default) or `webhook` |
| `FEISHU_GROUP_POLICY` | No | `open` / `allowlist` (default) / `disabled` |
| `FEISHU_ALLOWED_USERS` | No | Comma-separated user open_ids |
| `FEISHU_BOT_NAME` | No | Bot display name (for @mention detection) |

---

## 3. Monitoring

### 3.1 View Logs

```bash
# Real-time logs
aws logs tail /ecs/hermes-agentcore-gateway --follow --no-cli-pager

# Last 30 minutes
aws logs tail /ecs/hermes-agentcore-gateway --since 30m --no-cli-pager

# Filter for errors
aws logs tail /ecs/hermes-agentcore-gateway --since 1h --no-cli-pager \
  --filter-pattern "ERROR"
```

### 3.2 ECS Service Status

```bash
aws ecs describe-services \
  --cluster hermes-agentcore-gateway \
  --services hermes-agentcore-gateway \
  --query 'services[0].{
    status:status,
    desired:desiredCount,
    running:runningCount,
    pending:pendingCount,
    lastEvent:events[0].message
  }' --no-cli-pager
```

### 3.3 ECS Exec (SSH into container)

```bash
TASK_ID=$(aws ecs list-tasks \
  --cluster hermes-agentcore-gateway \
  --service-name hermes-agentcore-gateway \
  --query 'taskArns[0]' --output text)

aws ecs execute-command \
  --cluster hermes-agentcore-gateway \
  --task "$TASK_ID" \
  --container hermes-gateway \
  --interactive \
  --command "/bin/bash"
```

---

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `WEIXIN_TOKEN is required` | Token not set in env | Inject via Step 1.2 |
| `errcode=-14` | WeChat token expired | Re-scan QR (Step 1.1) and update token |
| `WebSocket disconnected` | Feishu connection dropped | Auto-reconnects in 30-120s; check App Secret is correct |
| `AgentCore invocation failed` | Runtime not reachable | Verify `AGENTCORE_RUNTIME_ARN` is correct; check IAM permissions |
| `ResourceNotFoundException` | Secret doesn't exist | Create the secret first (Step 1.2 or 2.2) |
| Container keeps restarting | Missing required env vars | Check logs: `aws logs tail /ecs/hermes-agentcore-gateway --since 10m` |
| `ThrottlingException` | Too many AgentCore calls | Proxy retries automatically; consider quota increase |

---

## 5. Architecture Recap

```
WeChat (iLink long-poll)  -->  ECS Fargate                    -->  AgentCore microVM
Feishu (WebSocket)        -->  hermes-agent gateway            -->  hermes-agent (AI)
                               AgentCoreProxyAgent                  per-user isolation
                               (protocol only, no LLM calls)       (Firecracker)
```

- **Gateway (ECS)**: Handles platform protocols only
- **Agent (AgentCore)**: Runs all AI inference in isolated microVMs
- **Bridge**: `AgentCoreProxyAgent` monkey-patches `AIAgent` to forward `run_conversation()` to `invoke_agent_runtime()`
