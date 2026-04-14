# ECS Fargate 统一网关方案：微信 + 飞书 → AgentCore

> **状态**: 设计方案（未实现）
> **日期**: 2026-04-14
> **目标**: 为微信（iLink 个人微信）和飞书/Lark 添加持久连接网关，复用现有 AgentCore 后端

---

## 1. 问题背景

当前架构使用 **Router Lambda + API Gateway** 接收 webhook 并转发到 AgentCore。这对 Telegram、Discord、Slack 等 webhook 推送模式的平台工作良好，但无法支持：

| 平台 | 连接模式 | 为什么 Lambda 不够 |
|------|---------|-------------------|
| **微信 iLink** | Long-poll（客户端主动拉取，35s 超时循环） | Lambda 最长 15 分钟，且无法维持常驻循环 |
| **飞书/Lark** | WebSocket（持久双向连接，默认模式） | Lambda 无法保持 WebSocket 长连接 |

两者都需要一个**常驻进程**来维持与平台的连接。

### 微信 iLink 的特殊限制

微信 iLink 是**个人微信**协议，不是企业微信：

- **1:1 绑定**: 一个 QR 扫码 = 一个微信账号 = 只能与扫码者 1:1 通信
- **不是多用户 Bot**: 不像 Telegram/Discord Bot 那样一个 token 服务所有人
- **Token 过期**: `bot_token` 会过期（`errcode=-14`），需要重新扫码
- **仅私聊**: `chat_type_routes()` 只返回 `("weixin_dm", ConversationKind::Private)`

**多人微信方案**: 每个用户需要独立的微信 iLink 实例（独立 QR 登录、独立 token、独立 long-poll 循环）。网关需要管理多个并发实例。

### 飞书/Lark 的优势

飞书天然支持多用户：

- **一个 Bot 服务所有人**: App ID + App Secret 创建一个 bot，所有授权用户都可以使用
- **WebSocket 默认模式**: `FEISHU_CONNECTION_MODE=websocket`，无需公网入站端口
- **也支持 Webhook**: 可选 `webhook` 模式，与 Lambda 兼容（但 WebSocket 更简单）

---

## 2. 整体架构

```
  ┌────────────────────────────────────────────────────────────┐
  │                     用户入口                                │
  │                                                            │
  │  Telegram  Discord  Slack     微信 iLink      飞书/Lark    │
  │  (webhook) (webhook) (webhook)  (long-poll)   (WebSocket)  │
  └─────┬────────┬───────┬──────────────┬──────────────┬───────┘
        │        │       │              │              │
        │   Webhook 模式  │        持久连接模式         │
        │   (现有架构)     │        (新增)              │
        ▼        ▼       ▼              ▼              ▼
  ┌──────────────────┐    ┌─────────────────────────────────┐
  │  API Gateway     │    │  ECS Fargate 统一网关            │
  │  + Router Lambda │    │  (常驻运行)                      │
  │  (无状态)         │    │                                 │
  │                  │    │  ┌──────────┐  ┌──────────────┐ │
  │  /webhook/tg     │    │  │ 微信      │  │ 飞书          │ │
  │  /webhook/slack  │    │  │ Adapter  │  │ Adapter      │ │
  │  /webhook/discord│    │  │ × N 实例  │  │ (单实例)      │ │
  │                  │    │  └────┬─────┘  └──────┬───────┘ │
  └────────┬─────────┘    │       └───────┬───────┘         │
           │              │               │                 │
           │              │       ┌───────▼──────────┐      │
           │              │       │  AgentCore       │      │
           │              │       │  Dispatcher      │      │
           │              │       │  (boto3 调用)     │      │
           │              │       └───────┬──────────┘      │
           │              └───────────────┼─────────────────┘
           │                              │
           ▼                              ▼
  ┌──────────────────────────────────────────────────────────┐
  │          invoke_agent_runtime()                           │
  │          (agentRuntimeArn, runtimeSessionId,              │
  │           runtimeUserId, payload)                         │
  └─────────────────────────┬────────────────────────────────┘
                            │
  ┌─────────────────────────▼────────────────────────────────┐
  │              Amazon Bedrock AgentCore                      │
  │                                                           │
  │   Per-User Firecracker MicroVM                            │
  │   ┌─────────────────────────────────────────────────┐    │
  │   │  Contract Server → hermes-agent (40+ tools)      │    │
  │   │  Bedrock Claude (SigV4 auth)                     │    │
  │   │  /mnt/workspace/.hermes/ (S3 sync)               │    │
  │   └─────────────────────────────────────────────────┘    │
  └───────────────────────────────────────────────────────────┘
```

**核心原则**: ECS 网关只负责**平台协议适配**（维持连接、收发消息），所有 AI 推理和工具执行仍然在 AgentCore microVM 中完成。网关是一个薄转发层。

---

## 3. 组件设计

### 3.1 AgentCore Dispatcher（核心转发模块）

网关的核心：接收来自微信/飞书的消息，调用 `invoke_agent_runtime()`，返回响应。

```
输入:
  - platform: "weixin" | "feishu"
  - actor_id: 平台用户标识 (微信 from_user_id / 飞书 open_id)
  - chat_id: 会话标识
  - text: 消息文本
  - media: 可选的媒体附件

处理:
  1. actor_id → DynamoDB 查询身份 (复用现有 identity table)
  2. 构造 session_id = f"{platform}:{actor_id}:{chat_id}"
  3. 构造 payload = {"action": "chat", "message": text, "channel": platform}
  4. 调用 invoke_agent_runtime(
       agentRuntimeArn=RUNTIME_ARN,
       runtimeSessionId=session_id,
       runtimeUserId=actor_id,
       payload=payload
     )
  5. 解析 SSE 响应 (data: "..." 格式)

输出:
  - 文本响应 → 返回给平台 adapter 发送
```

这与 Router Lambda 中的 `_invoke_agentcore()` 函数逻辑一致，只是运行环境从 Lambda 变成 ECS。

### 3.2 微信 Adapter

管理多个微信 iLink 实例，每个实例对应一个扫码用户。

```
┌──────────────────────────────────────────────────────┐
│  微信 Adapter Manager                                 │
│                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │ 实例 A       │  │ 实例 B       │  │ 实例 C      │ │
│  │ account: wx1 │  │ account: wx2 │  │ account: wx3│ │
│  │ token: t1    │  │ token: t2    │  │ token: t3   │ │
│  │ long-poll ↻  │  │ long-poll ↻  │  │ long-poll ↻ │ │
│  │ user: Alice  │  │ user: Bob    │  │ user: Carol │ │
│  └──────┬───────┘  └──────┬───────┘  └──────┬──────┘ │
│         └─────────────┬───┴─────────────────┘        │
│                       ▼                               │
│              AgentCore Dispatcher                     │
└──────────────────────────────────────────────────────┘
```

**实例生命周期**:

| 阶段 | 说明 |
|------|------|
| 注册 | 管理员通过 Web API 触发 QR 登录，获取 account_id + bot_token |
| 持久化 | 凭证加密存储到 Secrets Manager: `hermes/weixin/{account_id}` |
| 启动 | 网关启动时从 Secrets Manager 加载所有注册账号，启动 long-poll 循环 |
| 运行 | 每个实例独立 long-poll，收到消息后通过 Dispatcher 转发到 AgentCore |
| 过期 | `errcode=-14` 触发 token 过期告警 → CloudWatch Alarm → 通知管理员重新扫码 |
| 重新登录 | 通过 Web API 发起新的 QR 登录流程，更新 Secrets Manager |

**QR 登录流程**:

```
管理员                    ECS 网关                    微信 iLink API
  │                          │                            │
  │  POST /admin/weixin/qr   │                            │
  │ ────────────────────────► │                            │
  │                          │  GET get_bot_qrcode        │
  │                          │ ──────────────────────────► │
  │                          │ ◄────── qrcode_img_content  │
  │  ◄──── QR image URL      │                            │
  │                          │                            │
  │  (微信扫码)               │  poll get_qrcode_status    │
  │                          │ ──────────────────────────► │
  │                          │ ◄────── status: confirmed   │
  │                          │         bot_token, base_url │
  │                          │                            │
  │                          │  保存到 Secrets Manager     │
  │                          │  启动 long-poll 实例        │
  │  ◄──── 连接成功           │                            │
```

### 3.3 飞书 Adapter

单实例，通过 WebSocket 服务所有授权用户。

```
┌──────────────────────────────────────────────────────┐
│  飞书 Adapter                                         │
│                                                       │
│  ┌────────────────────────────────────┐               │
│  │ WebSocket Client (lark-oapi)       │               │
│  │ App ID: cli_xxx                    │               │
│  │ App Secret: ***                    │               │
│  │ 自动重连 + Ping/Pong keepalive     │               │
│  └──────────────┬─────────────────────┘               │
│                 │                                      │
│  收到消息 → 解析 event → 提取 open_id + text           │
│                 │                                      │
│                 ▼                                      │
│        AgentCore Dispatcher                           │
│        (session_id = feishu:{open_id}:{chat_id})      │
└──────────────────────────────────────────────────────┘
```

**飞书多用户路由**:
- DM: `session_id = feishu:{open_id}:dm` → AgentCore 按 user 隔离
- 群聊: `session_id = feishu:{open_id}:{chat_id}` → 按群 + 用户隔离
- allowlist 通过 DynamoDB identity table 检查: `ALLOW#feishu:{open_id}`

### 3.4 管理 API

ECS 网关提供一个内部 HTTP API（仅通过 ALB 内网访问或 API Gateway 鉴权）：

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/admin/health` | GET | 网关健康状态、各 adapter 连接状态 |
| `/admin/weixin/accounts` | GET | 列出所有微信实例及状态 |
| `/admin/weixin/qr` | POST | 发起新的 QR 登录流程 |
| `/admin/weixin/qr/{id}/status` | GET | 轮询 QR 扫码状态 |
| `/admin/weixin/{account_id}` | DELETE | 移除微信实例 |
| `/admin/feishu/status` | GET | 飞书连接状态 |

管理 API 可选接入现有 Web UI Dashboard（hermes-agent v0.9.0 新增的 Web Dashboard）。

---

## 4. 身份与权限模型

### 4.1 复用现有 DynamoDB Identity Table

```
现有 (Router Lambda 使用):
  PK: ALLOW#telegram:{user_id}     SK: ALLOW
  PK: ALLOW#discord:{user_id}      SK: ALLOW

新增:
  PK: ALLOW#weixin:{from_user_id}  SK: ALLOW
  PK: ALLOW#feishu:{open_id}       SK: ALLOW
```

ECS 网关和 Router Lambda 共享同一张 `hermes-agentcore-identity` 表，统一权限管理。

### 4.2 微信的特殊身份映射

微信 iLink 的 `from_user_id` 是平台分配的不透明 ID，不是微信号。管理员在 QR 登录时需要记录：

```
# 微信账号元数据 (Secrets Manager)
hermes/weixin/{account_id}:
{
  "bot_token": "xxx",
  "base_url": "https://ilinkai.weixin.qq.com",
  "user_id": "ilink_user_xxx",        # 扫码者的 ilink_user_id
  "owner_name": "Alice",               # 管理员备注
  "registered_at": "2026-04-14T10:00:00Z"
}

# DynamoDB allowlist
PK: ALLOW#weixin:{from_user_id}  SK: ALLOW
  userId: weixin:{from_user_id}
  platform: weixin
  weixinAccountId: {account_id}       # 关联到哪个微信实例
```

### 4.3 Session ID 映射到 AgentCore

```python
# 微信: 每个微信实例本身就是1:1，session_id 直接用 account_id
session_id = f"weixin:{account_id}:dm"

# 飞书 DM: 一个 bot 多个用户，按 open_id 隔离
session_id = f"feishu:{open_id}:dm"

# 飞书群聊: 按群 + 用户隔离
session_id = f"feishu:{open_id}:{chat_id}"
```

AgentCore 会为每个唯一的 `runtimeSessionId` 分配独立的 microVM，确保用户隔离。

---

## 5. AWS 基础设施

### 5.1 新增资源（CDK Phase 4）

```
┌──────────────────────────────────────────────────────────┐
│  hermes-agentcore-gateway (新增 CDK Stack)                │
│                                                           │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ ECS Cluster │  │ Fargate      │  │ CloudWatch       │  │
│  │ (使用现有   │  │ Service      │  │ Log Group        │  │
│  │  VPC)       │  │ desiredCount │  │ /ecs/hermes-     │  │
│  │             │  │   = 1        │  │  gateway         │  │
│  └──────┬──────┘  └──────┬───────┘  └──────────────────┘  │
│         │                │                                 │
│  ┌──────▼────────────────▼────────┐                       │
│  │ Fargate Task Definition        │                       │
│  │ CPU: 512   Memory: 1024       │                       │
│  │ Platform: LINUX/ARM64         │                       │
│  │ (Graviton, 与 AgentCore 一致)  │                       │
│  │                                │                       │
│  │ Container: hermes-gateway      │                       │
│  │   Image: ECR (同一 registry)   │                       │
│  │   Port: 8080 (admin API)       │                       │
│  │                                │                       │
│  │ Environment:                   │                       │
│  │   AGENTCORE_RUNTIME_ARN        │                       │
│  │   IDENTITY_TABLE               │                       │
│  │   WEIXIN_SECRETS_PREFIX        │                       │
│  │   FEISHU_APP_ID                │                       │
│  │   FEISHU_APP_SECRET (from SM)  │                       │
│  └────────────────────────────────┘                       │
│                                                           │
│  IAM Role (Task Role):                                    │
│  - bedrock-agentcore:InvokeAgentRuntime                   │
│  - bedrock-agentcore:InvokeAgentRuntimeForUser            │
│  - dynamodb:Query/GetItem (identity table)                │
│  - secretsmanager:GetSecretValue (hermes/weixin/*)        │
│  - secretsmanager:PutSecretValue (hermes/weixin/*)        │
│  - logs:CreateLogStream/PutLogEvents                      │
└──────────────────────────────────────────────────────────┘
```

### 5.2 网络架构

```
┌─────────────────── VPC (复用 Phase 1) ───────────────────┐
│                                                           │
│  Private Subnet A           Private Subnet B              │
│  ┌─────────────────┐       ┌─────────────────┐           │
│  │ Fargate Task    │       │ (备用 AZ)        │           │
│  │ hermes-gateway  │       │                  │           │
│  │                 │       │                  │           │
│  │ ← 只需出站连接   │       │                  │           │
│  │   无入站端口     │       │                  │           │
│  └────────┬────────┘       └──────────────────┘           │
│           │                                               │
│           ▼                                               │
│  ┌─────────────────┐                                     │
│  │  NAT Gateway    │  → 微信 iLink API (HTTPS 出站)       │
│  │  (复用现有)      │  → 飞书 WebSocket (WSS 出站)         │
│  │                 │  → AgentCore API (内网 VPC Endpoint)  │
│  └─────────────────┘                                     │
│                                                           │
│  可选: VPC Endpoint for bedrock-agentcore                 │
│        (避免 AgentCore 调用走 NAT)                         │
└───────────────────────────────────────────────────────────┘
```

**关键网络特性**:
- **无入站端口**: 微信 long-poll 和飞书 WebSocket 都是**出站连接**
- **不需要 ALB**: 网关不接收外部请求（管理 API 可选通过内网访问）
- **复用现有 VPC + NAT**: 与 Phase 1 创建的基础设施共享

### 5.3 与现有 Phase 的关系

```
Phase 1: VPC + Security + Guardrails + IAM          ← 复用
Phase 2: AgentCore Runtime + Container Build         ← 复用（同一个 runtime）
Phase 3: Router Lambda + API GW + DynamoDB + Cron    ← 复用（共享 DynamoDB）
Phase 4: ECS Gateway for WeChat + Feishu             ← 新增
```

Phase 4 是增量部署，不修改任何现有资源。

---

## 6. 容器设计

### 6.1 Dockerfile

```
# 轻量级 Python 容器，只包含平台协议处理
# 不包含完整的 hermes-agent（AI 推理在 AgentCore 中执行）

FROM python:3.12-slim AS gateway

# 最小依赖集
# - aiohttp: 微信 iLink HTTP + 飞书 Webhook 备选
# - lark-oapi: 飞书 SDK (WebSocket + API)
# - websockets: 飞书 WebSocket 传输层
# - cryptography: 微信媒体 AES 加解密
# - boto3: AgentCore 调用 + DynamoDB + Secrets Manager

COPY gateway/ /app/gateway/
COPY requirements-gateway.txt /app/
RUN pip install -r /app/requirements-gateway.txt

WORKDIR /app
CMD ["python", "-m", "gateway.main"]
```

**镜像大小目标**: < 200 MB（无 ML 库、无 Playwright、无 hermes-agent 完整依赖）

### 6.2 进程模型

```
main.py (asyncio event loop)
  │
  ├── WeixinAdapterManager
  │     ├── WeixinInstance("account_1")  →  asyncio.Task (long-poll loop)
  │     ├── WeixinInstance("account_2")  →  asyncio.Task (long-poll loop)
  │     └── ...
  │
  ├── FeishuAdapter
  │     └── FeishuWSClient              →  threading.Thread (lark-oapi WS)
  │
  ├── AgentCoreDispatcher
  │     └── boto3.client("bedrock-agentcore")
  │
  ├── AdminAPI (aiohttp server, port 8080)
  │     └── /admin/health, /admin/weixin/*, /admin/feishu/*
  │
  └── HealthCheck (定期上报 CloudWatch 自定义指标)
```

---

## 7. 消息流详解

### 7.1 微信消息流

```
微信用户 Alice                ECS 网关                     AgentCore
    │                           │                            │
    │  发送 "你好"              │                            │
    │  ─────(微信服务器)────►   │                            │
    │                    getupdates 返回消息                  │
    │                           │                            │
    │                 提取 from_user_id, text                │
    │                 查询 DynamoDB allowlist                │
    │                 获取 context_token                     │
    │                           │                            │
    │                 session_id = weixin:acct1:dm           │
    │                 actor_id = weixin:{from_user_id}       │
    │                           │                            │
    │                           │  invoke_agent_runtime()    │
    │                           │ ──────────────────────────►│
    │                           │                            │
    │                           │  (AgentCore microVM 处理)  │
    │                           │  hermes-agent 执行         │
    │                           │  Bedrock Claude 推理       │
    │                           │                            │
    │                           │ ◄─── SSE response          │
    │                           │                            │
    │                 解析响应，发送回微信                     │
    │                 sendmessage(context_token=...)         │
    │  ◄───(微信服务器)────     │                            │
    │  收到回复                 │                            │
```

### 7.2 飞书消息流

```
飞书用户 Bob               ECS 网关                      AgentCore
    │                          │                            │
    │  @bot 你好               │                            │
    │  ─(飞书 WebSocket)──►    │                            │
    │                          │                            │
    │                 WebSocket 回调触发                     │
    │                 解析 event → open_id, chat_id, text   │
    │                 查询 DynamoDB allowlist               │
    │                          │                            │
    │                 session_id = feishu:{open_id}:dm      │
    │                 actor_id = feishu:{open_id}           │
    │                          │                            │
    │                          │  invoke_agent_runtime()    │
    │                          │ ─────────────────────────► │
    │                          │                            │
    │                          │ ◄─── SSE response          │
    │                          │                            │
    │                 通过 lark-oapi 发送富文本消息           │
    │  ◄──(飞书 WebSocket)──   │                            │
    │  收到回复                │                            │
```

---

## 8. 错误处理与可靠性

### 8.1 微信 Token 过期处理

```
getupdates 返回 errcode=-14
        │
        ▼
  暂停该实例 long-poll
        │
  发送 CloudWatch Custom Metric:
    WeixinTokenExpired = 1
    Dimensions: {account_id}
        │
  触发 CloudWatch Alarm
        │
  SNS → 通知管理员重新扫码
        │
  管理员调用 POST /admin/weixin/qr
        │
  完成扫码 → 自动恢复 long-poll
```

### 8.2 飞书断连重连

hermes-agent 的 `FeishuAdapter` 已内置：
- `ws_reconnect_nonce`: 随机延迟后重连（默认 30s）
- `ws_reconnect_interval`: 最大重连间隔（默认 120s）
- `ws_ping_interval` / `ws_ping_timeout`: WebSocket keepalive

### 8.3 AgentCore 调用失败

```python
async def invoke_with_retry(session_id, actor_id, payload, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.to_thread(
                _invoke_agentcore, session_id, actor_id, payload
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ThrottlingException", "ServiceUnavailableException"):
                await asyncio.sleep(2 ** attempt)
                continue
            raise
    # 所有重试失败 → 返回用户友好错误
    return "抱歉，服务暂时繁忙，请稍后再试。"
```

### 8.4 ECS Task 重启恢复

- Fargate Service `desiredCount=1` 确保始终运行一个实例
- 容器启动时从 Secrets Manager 加载所有微信账号凭证
- 飞书自动重连 WebSocket
- 微信恢复 long-poll（如 token 未过期）
- `get_updates_buf` (sync cursor) 持久化到 S3，重启后从上次位置继续

---

## 9. 成本估算

### 9.1 ECS Fargate 网关

| 资源 | 配置 | 月成本 |
|------|------|--------|
| Fargate Task | 0.5 vCPU / 1 GB RAM, ARM64, 24×7 | ~$15 |
| NAT Gateway 流量 | 微信 long-poll + 飞书 WebSocket (~2 GB/月) | ~$10 |
| CloudWatch Logs | 1 GB/月 | ~$1 |
| Secrets Manager | 5-10 secrets | ~$3 |
| **网关小计** | | **~$29/月** |

### 9.2 总成本（含现有架构）

| 组件 | 月成本 |
|------|--------|
| AgentCore Runtime (所有平台共享) | $50–150 |
| Bedrock Claude | $100–500 |
| VPC + NAT | $30–45 |
| Lambda + API GW + DynamoDB | $15–25 |
| **ECS Gateway (新增)** | **$29** |
| S3 + Secrets + CloudWatch | $10–20 |
| **总计** | **~$234–769/月** |

新增成本约 $29/月（主要是 Fargate 常驻和 NAT 流量）。

---

## 10. CDK Stack 设计

```python
# stacks/gateway_stack.py

class HermesGatewayStack(Stack):
    """Phase 4: ECS Fargate gateway for WeChat + Feishu."""

    def __init__(self, scope, id, *, vpc, identity_table, runtime_arn, **kwargs):
        # 1. ECS Cluster (use existing VPC private subnets)
        # 2. Task Definition (ARM64, 512 CPU / 1024 MEM)
        # 3. Container (ECR image, environment variables)
        # 4. IAM Task Role (AgentCore invoke, DynamoDB, Secrets Manager)
        # 5. Fargate Service (desiredCount=1, no ALB)
        # 6. CloudWatch Log Group + Custom Metric Alarms
        # 7. Optional: CloudWatch Alarm for WeixinTokenExpired
```

### 10.1 部署命令

```bash
# Phase 4: Deploy ECS Gateway
./scripts/deploy.sh phase4

# 或单独部署
cdk deploy HermesGatewayStack
```

---

## 11. 配置管理

### 11.1 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `AGENTCORE_RUNTIME_ARN` | 是 | AgentCore Runtime ARN |
| `IDENTITY_TABLE` | 是 | DynamoDB identity table 名称 |
| `WEIXIN_ENABLED` | 否 | 启用微信适配器 (默认 false) |
| `WEIXIN_SECRETS_PREFIX` | 否 | Secrets Manager 前缀 (默认 `hermes/weixin/`) |
| `FEISHU_ENABLED` | 否 | 启用飞书适配器 (默认 false) |
| `FEISHU_APP_ID` | 条件 | 飞书 App ID (启用飞书时必需) |
| `FEISHU_APP_SECRET_ARN` | 条件 | Secrets Manager ARN (启用飞书时必需) |
| `FEISHU_CONNECTION_MODE` | 否 | `websocket` (默认) 或 `webhook` |
| `FEISHU_DOMAIN` | 否 | `feishu` (默认) 或 `lark` (国际版) |

### 11.2 Secrets Manager 结构

```
hermes/weixin/account_001     # 微信实例 1 的凭证
hermes/weixin/account_002     # 微信实例 2 的凭证
hermes/feishu/app_secret      # 飞书 App Secret
```

---

## 12. 实施计划

### Phase 4a — 基础框架（1-2 天）

- [ ] 创建 `gateway/` 目录结构（在 sample 项目中）
- [ ] 实现 `AgentCoreDispatcher`（复用 Router Lambda 的 `_invoke_agentcore` 逻辑）
- [ ] 实现 Admin Health API
- [ ] CDK Stack: ECS Cluster + Task Definition + IAM Role
- [ ] Dockerfile 构建

### Phase 4b — 飞书集成（1 天）

- [ ] 从 hermes-agent 提取飞书 Adapter 核心逻辑（WebSocket 连接 + 消息解析 + 发送）
- [ ] 适配为转发模式（消息 → AgentCore Dispatcher，而非本地 agent loop）
- [ ] DynamoDB allowlist 集成
- [ ] 测试：飞书 DM + 群聊 @mention

### Phase 4c — 微信集成（1-2 天）

- [ ] 从 hermes-agent 提取微信 Adapter 核心逻辑（long-poll + QR 登录 + 消息收发）
- [ ] 实现 WeixinAdapterManager（多实例管理）
- [ ] QR 登录 Admin API
- [ ] Secrets Manager 凭证持久化
- [ ] Token 过期检测 + CloudWatch 告警
- [ ] 测试：QR 登录 → 收发消息 → token 过期恢复

### Phase 4d — 生产化（1 天）

- [ ] CloudWatch Dashboard（网关指标 + 平台连接状态）
- [ ] 健壮性测试（网络断开、容器重启、AgentCore 不可用）
- [ ] 文档更新（README、部署指南）

---

## 13. 替代方案对比

| 方案 | 优点 | 缺点 | 适合场景 |
|------|------|------|---------|
| **A: ECS 网关 + AgentCore 后端（本方案）** | 复用 AgentCore 隔离能力；网关轻量；统一身份管理 | 多一层转发延迟；需要维护网关代码 | 多平台、多用户、企业级 |
| **B: ECS 直接运行 hermes-agent gateway** | 最简单；hermes-agent 原生支持微信/飞书 | 不经过 AgentCore；失去 per-user 隔离；需要直接管理 API key | 个人使用、快速验证 |
| **C: EC2 + hermes-agent + Bedrock** | 传统部署；灵活 | 需要自己管理服务器；无自动扩缩；无 per-user 隔离 | 单用户自托管 |
| **D: 只使用飞书 Webhook 模式 + Lambda** | 无需 ECS；与现有架构一致 | 不支持微信；飞书 Webhook 需要公网入站 | 只需飞书且不需微信 |

---

## 14. 风险与限制

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 微信 iLink API 不稳定或变更 | 微信功能不可用 | 监控 errcode，告警通知；API 变更时更新 adapter |
| 微信 token 频繁过期 | 需要人工干预扫码 | CloudWatch 告警 + 文档化恢复流程；探索自动刷新可能性 |
| 微信 1:1 限制无法绕过 | 每个用户需要独立实例 | 明确文档说明；需要多用户时建议使用 WeCom |
| 飞书 WebSocket 被防火墙阻断 | 飞书不可用 | 自动降级到 Webhook 模式 |
| AgentCore 冷启动延迟 | 用户首条消息等待 10-30s | 微信/飞书发送 typing 指示器；考虑预热策略 |
| ECS 单点故障 | 微信/飞书全部断连 | Fargate Service 自动重启；可选扩展到多 AZ |
