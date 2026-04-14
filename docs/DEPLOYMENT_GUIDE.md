# Hermes-Agent on Amazon Bedrock AgentCore — 完整部署文档

> 从零开始将 hermes-agent 部署到 AWS Bedrock AgentCore 的完整操作手册。

---

## 目录

1. [架构概览](#1-架构概览)
2. [前置条件](#2-前置条件)
3. [项目结构说明](#3-项目结构说明)
   - [AgentCore 配置文件说明](#31-agentcore-配置文件说明)
4. [环境变量参考](#4-环境变量参考)
5. [快速验证（PoC，3 天路径）](#5-快速验证poc3-天路径)
   - [Day 1: 本地 Docker 验证](#day-1-本地-docker-验证)
   - [Day 2: 部署到 AgentCore](#day-2-部署到-agentcore)
   - [Day 3: Telegram 端到端集成](#day-3-telegram-端到端集成)
6. [完整生产部署](#6-完整生产部署)
   - [Step 1: 准备 AWS 账号](#step-1-准备-aws-账号)
   - [Step 2: 配置密钥](#step-2-配置密钥)
   - [Step 3: 配置 cdk.json](#step-3-配置-cdkjson)
   - [Step 4: 三阶段部署](#step-4-三阶段部署)
   - [Step 5: 配置频道 Webhook](#step-5-配置频道-webhook)
   - [Step 6: 添加用户白名单](#step-6-添加用户白名单)
   - [Step 7: 端到端验证](#step-7-端到端验证)
7. [CDK 栈详细说明](#7-cdk-栈详细说明)
8. [日常运维](#8-日常运维)
   - [更新 hermes-agent 版本](#更新-hermes-agent-版本)
   - [仅更新 CDK 配置](#仅更新-cdk-配置)
   - [添加定时任务（Cron）](#添加定时任务cron)
   - [管理用户白名单](#管理用户白名单)
9. [监控与告警](#9-监控与告警)
10. [故障排查](#10-故障排查)
11. [成本优化](#11-成本优化)
12. [安全加固清单](#12-安全加固清单)
13. [附录：AgentCore 合约协议](#13-附录agentcore-合约协议)

---

## 1. 架构概览

```
用户 (Telegram / Slack / Discord / 飞书)
        │
   API Gateway (HTTP API)
        │
   Router Lambda ──── DynamoDB (身份表 + 白名单)
        │
   InvokeAgentRuntime
        │
   ┌────▼─────────────────────────────────────┐
   │  AgentCore Runtime (Firecracker 微虚拟机)  │
   │                                           │
   │  contract.py (:8080)                      │
   │    ├── GET  /ping       → 健康检查         │
   │    └── POST /invocations → 消息分发        │
   │         │                                  │
   │    ┌────▼────┐     ┌──────────────┐       │
   │    │预热代理  │────►│ hermes-agent │       │
   │    │(Bedrock) │     │ (40+ 工具)   │       │
   │    └─────────┘     └──────┬───────┘       │
   │                           │                │
   │    litellm ← Bedrock ConverseStream       │
   │    S3 同步 ← /mnt/workspace/.hermes       │
   └───────────────────────────────────────────┘
        │                    │
   Amazon Bedrock       S3 (用户状态)
   (Claude 模型)         ├── state.db
                         ├── memories/
                         └── skills/
```

**关键流程：**

1. 用户在 Telegram 发消息 → Telegram Webhook → API Gateway
2. Router Lambda 验证签名、查 DynamoDB 白名单、解析用户身份
3. Lambda 调用 `InvokeAgentRuntime` → AgentCore 路由到用户专属容器
4. 容器内 `contract.py` 收到 POST `/invocations`
5. 若 hermes-agent 尚未就绪（冷启动 10-30s）→ 预热代理直接调 Bedrock 回复
6. 若已就绪 → 完整 hermes-agent 处理（40+ 工具、记忆、技能）
7. 响应原路返回 → Lambda → Telegram `sendMessage`

---

## 2. 前置条件

### 2.1 AWS 账号要求

| 项目 | 要求 |
|------|------|
| AWS 账号 | 已开通 Bedrock AgentCore 访问权限 |
| Bedrock 模型访问 | 至少启用一个 Claude 模型（Sonnet 4.6 推荐） |
| 跨区推理 | 如使用 `global.*` 模型 ID，需开启 Cross-Region Inference |
| IAM 权限 | 部署账号需有 CloudFormation、ECR、Lambda、DynamoDB、S3、VPC 等管理权限 |
| 区域 | 建议 `us-east-1` 或 `us-west-2`（AgentCore 可用区域） |

### 2.2 可用的 Bedrock 模型 ID

| 模型 | 模型 ID | 跨区 ID |
|------|---------|---------|
| Claude Opus 4.6 | `anthropic.claude-opus-4-6-v1` | `global.anthropic.claude-opus-4-6-v1` |
| Claude Sonnet 4.6 | `anthropic.claude-sonnet-4-6-v1` | `global.anthropic.claude-sonnet-4-6-v1` |
| Claude Haiku 4.5 | `anthropic.claude-haiku-4-5-20251001-v1` | `global.anthropic.claude-haiku-4-5-20251001-v1` |

验证模型访问：

```bash
aws bedrock list-foundation-models \
  --by-provider Anthropic \
  --query "modelSummaries[].modelId" \
  --output table
```

### 2.3 本地工具

```bash
# 1. AWS CLI v2
aws --version        # >= 2.15
aws sts get-caller-identity  # 确认已登录

# 2. AWS CDK
npm install -g aws-cdk
cdk --version        # >= 2.150

# 3. Python 3.11+
python3 --version    # >= 3.11

# 4. Docker（构建 ARM64 镜像）
docker --version
docker buildx version   # 需要 buildx 支持交叉编译

# 5. AgentCore Starter Toolkit
pip install bedrock-agentcore-toolkit
agentcore --version

# 6. jq（deploy 脚本依赖）
jq --version

# 7. rsync（复制 hermes-agent 源码）
rsync --version
```

**如果没有本地 ARM64 Docker 环境**（例如在 x86 Mac/Linux 上），可使用 CodeBuild 远程构建：
```bash
BUILD_MODE=codebuild ./scripts/deploy.sh phase2
```

### 2.4 频道 Bot Token

| 频道 | 需要的凭证 | 获取方式 |
|------|-----------|----------|
| Telegram | Bot Token | 与 [@BotFather](https://t.me/BotFather) 对话创建 bot |
| Slack | Bot Token + Signing Secret | [Slack API 控制台](https://api.slack.com/apps) 创建 App |
| Discord | Bot Token + Application ID | [Discord 开发者门户](https://discord.com/developers/applications) 创建 Application |
| 飞书 | App ID + App Secret + Verification Token | [飞书开放平台](https://open.feishu.cn) 创建自建应用 |

---

## 3. 项目结构说明

```
sample-host-harmesagent-on-amazon-bedrock-agentcore/
│
├── app.py                          # CDK 入口，声明所有 Stack 及依赖关系
├── cdk.json                        # CDK 配置（模型 ID、预算、VPC CIDR 等）
├── requirements.txt                # Python 依赖（CDK + boto3 + litellm）
├── .gitignore
├── README.md
│
├── bridge/                         # 容器桥接层（运行在 AgentCore 微虚拟机内）
│   ├── __init__.py
│   ├── contract.py                 # HTTP 合约服务器 — /ping + /invocations
│   ├── warmup_agent.py             # 预热代理 — 冷启动时快速回复
│   ├── workspace_sync.py           # S3 工作区持久化 — 周期同步 + 热备份
│   ├── scoped_credentials.py       # 每用户 STS 范围凭证
│   ├── bedrock_provider.py         # litellm Bedrock 模型映射
│   ├── Dockerfile                  # ARM64 多阶段镜像构建
│   └── entrypoint.sh              # 容器入口脚本
│
├── lambda/                         # Lambda 函数
│   ├── router/index.py             # 频道 Webhook → AgentCore 路由
│   ├── cron/index.py               # EventBridge 定时任务 → AgentCore
│   └── token_metrics/index.py      # Token 用量聚合与预算告警
│
├── stacks/                         # CDK 基础设施栈
│   ├── __init__.py
│   ├── vpc_stack.py                # VPC、子网、NAT、VPC Endpoints
│   ├── security_stack.py           # KMS、Secrets Manager、Cognito
│   ├── guardrails_stack.py         # Bedrock Guardrails 内容过滤
│   ├── agentcore_stack.py          # IAM 执行角色、S3 桶、安全组
│   ├── router_stack.py             # Lambda、API Gateway、DynamoDB
│   ├── cron_stack.py               # EventBridge Scheduler、Cron Lambda
│   ├── observability_stack.py      # CloudWatch Dashboard、告警、SNS
│   └── token_monitoring_stack.py   # Token 监控 Lambda + 定时规则
│
├── scripts/                        # 部署与配置脚本
│   ├── deploy.sh                   # 三阶段一键部署
│   ├── setup_telegram.sh           # Telegram Webhook 配置
│   └── setup_slack.sh              # Slack App 配置指引
│
├── tests/                          # 单元测试（25 个）
│   ├── test_contract.py
│   ├── test_workspace_sync.py
│   └── test_router.py
│
└── docs/                           # 文档
    ├── ARCHITECTURE.md
    ├── MIGRATION_PLAN.md
    ├── AGENTCORE_CONTRACT.md
    └── DEPLOYMENT_GUIDE.md         # ← 本文档
```

### 3.1 AgentCore 配置文件说明

Phase 2 部署由 AgentCore CLI 驱动，其配置文件位于 `agentcore/` 目录下。**部署前必须确认以下两个文件的配置正确。**

#### `agentcore/agentcore.json` — 定义「部署什么」

```jsonc
{
  "$schema": "https://schema.agentcore.aws.dev/v1/agentcore.json",
  "name": "hermes",                    // 项目名称，用于生成 Runtime 名称
  "version": 1,
  "managedBy": "CDK",
  "runtimes": [
    {
      "name": "hermes",                // Runtime 名称，与 project name 组合后成为 Runtime ID
      "build": "Container",            // 构建方式：Container（Docker 镜像）
      "entrypoint": "main.py",         // 容器入口文件
      "codeLocation": "app/hermes/",   // 源码目录（相对于项目根目录）
      "runtimeVersion": "PYTHON_3_13", // Python 版本
      "networkMode": "PUBLIC",         // 网络模式：PUBLIC（允许出站访问）
      "protocol": "HTTP"               // 合约协议：HTTP（/ping + /invocations）
    }
  ]
}
```

**需要修改的场景：**

| 字段 | 何时需要修改 | 说明 |
|------|-------------|------|
| `name` | 同一账号部署多个实例时 | 修改后会生成不同的 Runtime ID，避免冲突 |
| `runtimes[].name` | 同上 | 与 `name` 配合使用 |
| `runtimes[].codeLocation` | 调整项目目录结构时 | 必须指向包含 Dockerfile 的目录 |
| `runtimes[].runtimeVersion` | 升级 Python 版本时 | 需与 Dockerfile 中的基础镜像一致 |
| `runtimes[].networkMode` | 仅使用 Bedrock 不需外网时 | 改为 `PRIVATE` 可增强隔离 |

#### `agentcore/aws-targets.json` — 定义「部署到哪里」

```jsonc
[
  {
    "name": "default",
    "description": "Default deployment target",
    "account": "687912291502",   // ← 改为你的 AWS 账号 ID
    "region": "us-west-2"        // ← 改为目标区域
  }
]
```

**⚠️ 重要：** 项目初始值为示例账号，**必须修改为你自己的 AWS 账号 ID**。获取方式：

```bash
aws sts get-caller-identity --query Account --output text
```

如果 `account` 与当前 AWS 凭证的账号不匹配，Phase 2 部署会报 `sts:AssumeRole` 错误。

---

## 4. 环境变量参考

### 4.1 容器环境变量（bridge/ 组件使用）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8080` | 合约服务器监听端口（AgentCore 固定 8080） |
| `HERMES_HEADLESS` | `1` | 禁用 hermes-agent 的频道适配器 |
| `AGENTCORE_MODE` | `1` | 启用 AgentCore 特定行为 |
| `HERMES_HOME` | `/mnt/workspace/.hermes` | hermes-agent 主目录 |
| `BEDROCK_MODEL_ID` | `anthropic.claude-sonnet-4-6-v1` | 完整代理使用的 Bedrock 模型 |
| `WARMUP_MODEL_ID` | 同 `BEDROCK_MODEL_ID` | 预热代理使用的模型（可设更便宜的） |
| `HERMES_PROVIDER` | `anthropic` | LLM 提供商标识 |
| `HERMES_BASE_URL` | _(空)_ | 自定义 LLM API 地址（使用 litellm 代理时） |
| `S3_BUCKET` | _(必填)_ | 用户文件 S3 桶名 |
| `AGENTCORE_USER_NAMESPACE` | _(必填)_ | 用户级 S3 前缀（如 `user_abc123`） |
| `WORKSPACE_PATH` | `/mnt/workspace/.hermes` | 本地工作区路径 |
| `WORKSPACE_SYNC_INTERVAL` | `300` | S3 同步间隔（秒） |
| `EXECUTION_ROLE_ARN` | _(必填)_ | IAM 执行角色 ARN（用于 STS 范围凭证） |
| `HERMES_UID` | `10000` | 非 root 用户 UID |

### 4.2 Lambda 环境变量（由 CDK 自动设置）

| 变量 | 组件 | 说明 |
|------|------|------|
| `AGENTCORE_RUNTIME_ARN` | Router / Cron | AgentCore 运行时 ARN |
| `AGENTCORE_QUALIFIER` | Router / Cron | 运行时端点标识 |
| `IDENTITY_TABLE` | Router | DynamoDB 身份表名 |
| `S3_BUCKET` | Router | 用户文件桶（图片上传） |
| `DAILY_TOKEN_BUDGET` | Token Metrics | 每日 Token 预算 |
| `DAILY_COST_BUDGET_USD` | Token Metrics | 每日成本预算（美元） |
| `ALARM_SNS_TOPIC_ARN` | Token Metrics | 告警 SNS 主题 |

---

## 5. 快速验证（PoC，3 天路径）

> 目标：用最少步骤验证架构可行性。

### Day 1: 本地 Docker 验证

#### 1.1 克隆项目

```bash
git clone <repository-url> sample-host-harmesagent-on-amazon-bedrock-agentcore
cd sample-host-harmesagent-on-amazon-bedrock-agentcore
```

#### 1.2 准备 hermes-agent 源码

Dockerfile 需要 hermes-agent 源码在构建上下文中：

```bash
# 方式 A：符号链接（不会被 git 跟踪）
ln -s ~/hermes-agent ./hermes-agent

# 方式 B：复制
rsync -a --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
  ~/hermes-agent/ ./hermes-agent/
```

#### 1.3 构建 Docker 镜像

```bash
# ARM64 构建（用于部署到 AgentCore）
docker buildx build --platform linux/arm64 \
  -t hermes-agentcore:latest \
  -f bridge/Dockerfile .

# 本地测试用（x86 平台）
docker build -t hermes-agentcore:local -f bridge/Dockerfile .
```

> 首次构建约 5-10 分钟（安装 hermes-agent 依赖）。后续构建利用缓存，约 1-2 分钟。

#### 1.4 本地运行

```bash
docker run -p 8080:8080 \
  -e AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  -e AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  -e AWS_SESSION_TOKEN="$AWS_SESSION_TOKEN" \
  -e AWS_DEFAULT_REGION=us-east-1 \
  -e BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-6-v1 \
  hermes-agentcore:local
```

日志输出应依次显示：
```
[agentcore.warmup]    INFO WarmupAgent initialised (model=anthropic.claude-sonnet-4-6-v1)
[agentcore.contract]  INFO Lightweight warm-up agent ready
[agentcore.contract]  INFO Loading full hermes-agent …
[agentcore.contract]  INFO AgentCore contract server listening on port 8080
... (10-30 秒后)
[agentcore.contract]  INFO Full hermes-agent ready (model=anthropic.claude-sonnet-4-6-v1)
```

#### 1.5 测试端点

在另一个终端：

```bash
# 健康检查
curl -s http://localhost:8080/ping | python3 -m json.tool
# 预期: {"status": "Healthy"}  (agent 就绪后)
# 或:   {"status": "HealthyBusy"}  (加载中)

# 聊天 — 预热代理（如果 agent 还在加载中）
curl -s -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "action": "chat",
    "userId": "test_user",
    "actorId": "test:1",
    "channel": "test",
    "message": "你好，你是谁？"
  }' | python3 -m json.tool

# 聊天 — 完整代理（agent 就绪后）
curl -s -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "action": "chat",
    "userId": "test_user",
    "actorId": "test:1",
    "channel": "test",
    "message": "你有哪些工具可以使用？"
  }' | python3 -m json.tool

# 容器状态
curl -s -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"action": "status"}' | python3 -m json.tool
# 预期: {"agent_ready": true, "lightweight_ready": true, "uptime_seconds": ..., "busy_count": 0}
```

**Day 1 检查点：** `/ping` 返回 Healthy，`/invocations` 返回正常回复 ✅

---

### Day 2: 部署到 AgentCore

#### 2.1 配置 AWS 凭证

```bash
# 方式 A：Access Key
aws configure

# 方式 B：SSO
aws sso login --profile your-profile
export AWS_PROFILE=your-profile

# 验证
aws sts get-caller-identity
```

#### 2.2 安装 AgentCore Toolkit

```bash
pip install bedrock-agentcore-toolkit
agentcore --version
```

#### 2.3 配置 Runtime

```bash
agentcore configure --name hermes_agent
```

这会生成：
- `.bedrock_agentcore.yaml` — Runtime 配置
- `.bedrock_agentcore/hermes_agent/` — 构建目录

检查生成的配置：
```bash
cat .bedrock_agentcore.yaml
```

确认以下字段：
```yaml
runtime_name: hermes_agent
port: 8080
architecture: arm64
```

将我们的 Dockerfile 复制到 Toolkit 期望的位置：
```bash
cp bridge/Dockerfile .bedrock_agentcore/hermes_agent/Dockerfile
```

#### 2.4 构建并部署

```bash
# 确保 hermes-agent 源码在项目目录下
ls hermes-agent/pyproject.toml  # 应该存在

# 构建 ARM64 镜像 + 推送 ECR + 创建/更新 Runtime
agentcore deploy

# 如果本地没有 ARM64 Docker，使用 CodeBuild：
# BUILD_MODE=codebuild agentcore deploy
```

部署过程：
1. 构建 ARM64 Docker 镜像（约 5-10 分钟）
2. 创建 ECR 仓库并推送镜像
3. 创建或更新 AgentCore Runtime
4. 创建 Runtime Endpoint

#### 2.5 查看 Runtime 状态

```bash
agentcore status
# 或 JSON 格式：
agentcore status --json | python3 -m json.tool
```

记下输出中的：
- `runtimeArn` — 后续 CDK 配置需要
- `qualifier` (或 `endpointId`) — 后续 CDK 配置需要

#### 2.6 测试直接调用

```bash
# 通过 Toolkit 调用
agentcore invoke '{
  "action": "chat",
  "userId": "test_user_001",
  "actorId": "test:1",
  "channel": "test",
  "message": "Hello! What can you do?"
}'

# 通过 AWS CLI 调用
RUNTIME_ARN=$(agentcore status --json | jq -r '.runtimeArn')
QUALIFIER=$(agentcore status --json | jq -r '.qualifier')

aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$RUNTIME_ARN" \
  --qualifier "$QUALIFIER" \
  --runtime-session-id "test_user_001:test:00000000-0000" \
  --runtime-user-id "test:1" \
  --payload '{"action":"chat","userId":"test_user_001","message":"hello"}' \
  --content-type "application/json" \
  --accept "application/json" \
  /dev/stdout
```

**Day 2 检查点：** `agentcore invoke` 返回正常回复 ✅

---

### Day 3: Telegram 端到端集成

#### 3.1 创建 Python 虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### 3.2 Bootstrap CDK

```bash
cdk bootstrap
```

#### 3.3 存入 Telegram Bot Token

```bash
aws secretsmanager create-secret \
  --name "hermes/telegram-bot-token" \
  --secret-string "YOUR_TELEGRAM_BOT_TOKEN_HERE"
```

> 用 [@BotFather](https://t.me/BotFather) 创建 bot 后获取 token。

#### 3.4 更新 cdk.json（填入 Runtime ID）

```bash
RUNTIME_ARN=$(agentcore status --json | jq -r '.runtimeArn')
QUALIFIER=$(agentcore status --json | jq -r '.qualifier')

# 自动写入 cdk.json
TMP=$(mktemp)
jq ".context.agentcore_runtime_arn = \"$RUNTIME_ARN\" | \
    .context.agentcore_qualifier = \"$QUALIFIER\"" \
  cdk.json > "$TMP" && mv "$TMP" cdk.json

# 验证
jq '.context | {agentcore_runtime_arn, agentcore_qualifier}' cdk.json
```

#### 3.5 部署 Phase 1 + Phase 3 栈

```bash
# 部署所有 CDK 栈（跳过 Phase 2，因为 Runtime 已经部署了）
./scripts/deploy.sh cdk-only
```

这会依次部署：
- Phase 1: `hermes-agentcore-vpc`, `hermes-agentcore-security`, `hermes-agentcore-guardrails`, `hermes-agentcore-agentcore`, `hermes-agentcore-observability`
- Phase 3: `hermes-agentcore-router`, `hermes-agentcore-cron`, `hermes-agentcore-token-monitoring`

> 首次部署约 10-15 分钟（VPC 创建较慢）。

#### 3.6 配置 Telegram Webhook

```bash
./scripts/setup_telegram.sh
```

脚本会自动：
1. 从 CloudFormation 输出获取 API Gateway URL
2. 从 Secrets Manager 读取 Bot Token
3. 调用 Telegram `setWebhook` API
4. 显示 `getWebhookInfo` 验证结果

手动验证：
```bash
# 获取 API URL
API_URL=$(aws cloudformation describe-stacks \
  --stack-name hermes-agentcore-router \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)
echo "API URL: $API_URL"
echo "Telegram webhook: ${API_URL}webhook/telegram"
```

#### 3.7 添加测试用户到白名单

```bash
# 替换 YOUR_TELEGRAM_USER_ID 为你的 Telegram 数字 ID
# （给 @userinfobot 发消息可查看自己的 ID）
aws dynamodb put-item \
  --table-name hermes-agentcore-identity \
  --item '{
    "PK": {"S": "ALLOW#telegram:YOUR_TELEGRAM_USER_ID"},
    "SK": {"S": "ALLOW"},
    "addedBy": {"S": "admin"},
    "addedAt": {"N": "'$(date +%s)'"}
  }'
```

#### 3.8 端到端测试

在 Telegram 中向你的 bot 发消息。完整链路：

```
你的 Telegram 消息
  → Telegram 服务器
  → API Gateway (POST /webhook/telegram)
  → Router Lambda
     ├── 验证 Telegram 签名
     ├── 查 DynamoDB 白名单 (ALLOW#telegram:xxx)
     ├── 解析/创建用户身份 (CHANNEL#telegram:xxx → USER#user_xxx)
     └── InvokeAgentRuntime(sessionId, payload)
  → AgentCore
  → 用户专属 Firecracker 微虚拟机
  → contract.py POST /invocations
  → hermes-agent run_conversation()
  → 响应原路返回
  → Router Lambda
  → Telegram sendMessage
  → 你收到回复
```

**Day 3 检查点：** Telegram bot 回复正常 ✅

---

## 6. 完整生产部署

### Step 1: 准备 AWS 账号

```bash
# 确认身份和区域
aws sts get-caller-identity
aws configure get region

# 确认 Bedrock 模型可用
aws bedrock list-foundation-models \
  --by-provider Anthropic \
  --query "modelSummaries[].modelId" \
  --output table
```

### Step 2: 配置密钥

将所有敏感凭证存入 AWS Secrets Manager：

```bash
# ---- 必需 ----

# Telegram Bot Token
aws secretsmanager create-secret \
  --name "hermes/telegram-bot-token" \
  --secret-string "123456:ABC-DEF..."

# ---- 可选（按需启用的频道） ----

# Slack
aws secretsmanager create-secret \
  --name "hermes/slack-bot-token" \
  --secret-string "xoxb-..."
aws secretsmanager create-secret \
  --name "hermes/slack-signing-secret" \
  --secret-string "abc123..."

# Discord
aws secretsmanager create-secret \
  --name "hermes/discord-bot-token" \
  --secret-string "MTIz..."

# 飞书 (Feishu/Lark)
aws secretsmanager create-secret \
  --name "hermes/feishu-app-id" \
  --secret-string "cli_xxxxx"
aws secretsmanager create-secret \
  --name "hermes/feishu-app-secret" \
  --secret-string "xxxxx"
aws secretsmanager create-secret \
  --name "hermes/feishu-verification-token" \
  --secret-string "xxxxx"

# ---- 可选（非 Bedrock 模型的外部 API） ----

# OpenAI（如需通过 NAT 调用 GPT）
aws secretsmanager create-secret \
  --name "hermes/openai-api-key" \
  --secret-string "sk-..."

# OpenRouter
aws secretsmanager create-secret \
  --name "hermes/openrouter-api-key" \
  --secret-string "sk-or-..."
```

> 密钥名必须以 `hermes/` 开头，Lambda 的 IAM 策略按此前缀授权。

### Step 3: 配置 cdk.json

编辑 `cdk.json` 中的 `context` 部分：

```jsonc
{
  "context": {
    // ---- 项目标识 ----
    "project_name": "hermes-agentcore",     // 所有资源名前缀

    // ---- 模型选择 ----
    "default_model_id": "global.anthropic.claude-opus-4-6-v1",    // 完整代理模型
    "warmup_model_id": "global.anthropic.claude-sonnet-4-6-v1",   // 预热代理模型（可选更便宜的）

    // ---- 会话管理 ----
    "session_idle_timeout": 1800,           // 空闲超时（秒），30 分钟后容器被回收
    "session_max_lifetime": 28800,          // 最大生命周期（秒），8 小时强制回收

    // ---- 状态同步 ----
    "workspace_sync_interval_seconds": 300, // S3 同步间隔（秒），每 5 分钟

    // ---- 安全 ----
    "enable_guardrails": true,              // 启用 Bedrock Guardrails 内容过滤

    // ---- 成本控制 ----
    "enable_token_monitoring": true,
    "daily_token_budget": 2000000,          // 每日 Token 预算
    "daily_cost_budget_usd": 20,            // 每日成本预算（美元）

    // ---- 频道 ----
    "channels": ["telegram", "slack", "discord", "feishu"],

    // ---- 网络 ----
    "vpc_cidr": "10.0.0.0/16",
    "az_count": 2,

    // ---- 告警 ----
    "alarm_email": "ops@example.com",       // 告警邮件（留空则不发）

    // ---- AgentCore Runtime ID（Phase 2 自动填入） ----
    "agentcore_runtime_arn": "",
    "agentcore_qualifier": ""
  }
}
```

### Step 4: 三阶段部署

#### 方式 A：一键全量部署

```bash
./scripts/deploy.sh all
```

自动执行：
1. **Phase 1** — CDK 基础栈（VPC、安全、Guardrails、IAM、监控）
2. **Phase 2** — AgentCore Toolkit（构建 Docker、推送 ECR、创建 Runtime、回写 Runtime ID 到 cdk.json）
3. **Phase 3** — CDK 依赖栈（Router Lambda、Cron、Token 监控）

#### 方式 B：分阶段部署（更可控）

```bash
# Phase 1: 基础设施
./scripts/deploy.sh phase1

# Phase 2: 容器运行时（需等 Phase 1 完成）
./scripts/deploy.sh phase2

# Phase 3: 应用层（需等 Phase 2 完成）
./scripts/deploy.sh phase3
```

#### 方式 C：仅 CDK（Runtime 已存在）

```bash
# 跳过 Phase 2（不重新构建容器）
./scripts/deploy.sh cdk-only
```

### Step 5: 配置频道 Webhook

#### Telegram

```bash
./scripts/setup_telegram.sh
```

#### Slack

```bash
./scripts/setup_slack.sh
# 这会打印 Slack App 配置指引，按步骤在 Slack API 控制台操作
```

主要步骤：
1. 在 [Slack API](https://api.slack.com/apps) 创建 App
2. Event Subscriptions → Enable → Request URL 填 `{API_URL}webhook/slack`
3. Subscribe to bot events: `message.im`, `message.channels`
4. OAuth & Permissions → Scopes: `chat:write`, `channels:history`, `im:history`
5. Install to Workspace → 复制 Bot Token
6. 将 Token 和 Signing Secret 存入 Secrets Manager

#### Discord

Discord 使用 Interactions Endpoint URL 而非 Webhook：
1. 在 [Discord 开发者门户](https://discord.com/developers/applications) 创建 Application
2. General Information → Interactions Endpoint URL 填 `{API_URL}webhook/discord`
3. Bot → 复制 Token 存入 Secrets Manager
4. OAuth2 → URL Generator → 选 `bot` scope + `Send Messages` permission → 生成邀请链接 → 邀请到服务器

#### 飞书 (Feishu/Lark)

##### 1. 创建飞书应用

1. 登录 [飞书开放平台](https://open.feishu.cn)，点击 **创建自建应用**
2. 填写应用名称（如 `Hermes Agent`），创建完成后进入应用详情

##### 2. 获取凭证

在应用详情页面获取以下三个值：

| 凭证 | 位置 | Secrets Manager 名称 |
|------|------|---------------------|
| App ID | 凭证与基础信息 → App ID | `hermes/feishu-app-id` |
| App Secret | 凭证与基础信息 → App Secret | `hermes/feishu-app-secret` |
| Verification Token | 安全设置 → Verification Token | `hermes/feishu-verification-token` |

> **注意：** 如果应用使用 v2.0 事件模式，Verification Token 可能位于 **安全设置** 页面而非事件订阅页面。若找不到，代码已兼容 v2.0 header 中的 token 字段。

存入 Secrets Manager：

```bash
aws secretsmanager create-secret \
  --name "hermes/feishu-app-id" \
  --secret-string "你的App_ID"

aws secretsmanager create-secret \
  --name "hermes/feishu-app-secret" \
  --secret-string "你的App_Secret"

aws secretsmanager create-secret \
  --name "hermes/feishu-verification-token" \
  --secret-string "你的Verification_Token"
```

##### 3. 开启机器人能力

应用详情 → **添加应用能力** → 开启 **机器人**

##### 4. 配置权限

**权限管理** 页面，添加以下权限：

| 权限 | 权限名称 | 用途 |
|------|---------|------|
| `im:message` | 获取与发送消息 | 读取和发送消息 |
| `im:message:send_as_bot` | 以应用的身份发消息 | 机器人回复消息 |

##### 5. 配置事件订阅

**事件与回调** → **事件订阅**：

1. **订阅方式** 选择 **将事件发送至 开发者服务器**
2. **请求地址** 填写：
   ```
   {API_URL}webhook/feishu
   ```
   例如：`https://y2byqip0wj.execute-api.us-west-2.amazonaws.com/webhook/feishu`
3. 点击 **添加事件**，订阅以下事件：
   - `im.message.receive_v1` — 接收消息

##### 6. 发布应用

**版本管理与发布** → 创建版本 → 提交审核并发布

> 企业自建应用通常可由管理员直接审核通过。

##### 7. 添加用户白名单

飞书用户的 `open_id` 获取方式：

**方式 A（推荐）：从日志获取** — 先不加白名单，给机器人发消息，查看 CloudWatch 日志中被拦截的 `actor_id`：

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/hermes-agentcore-router \
  --filter-pattern "feishu" --since 5m
```

日志中会显示 `Blocked message from feishu:ou_xxxxxxxxxxxx`，复制该 `ou_xxx` 即可。

**方式 B：通过 API 查询** — 用邮箱或手机号批量查询：

```bash
# 获取 tenant_access_token
TOKEN=$(curl -s -X POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal \
  -H "Content-Type: application/json" \
  -d '{"app_id":"你的APP_ID","app_secret":"你的APP_SECRET"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['tenant_access_token'])")

# 用邮箱查 open_id
curl -s -X POST https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"emails":["user@example.com"]}'
```

添加到白名单：

```bash
aws dynamodb put-item \
  --table-name hermes-agentcore-identity \
  --item '{
    "PK": {"S": "ALLOW#feishu:ou_xxxxxxxxxxxx"},
    "SK": {"S": "ALLOW"},
    "addedBy": {"S": "admin"},
    "addedAt": {"N": "'$(date +%s)'"}
  }'
```

##### 8. 验证

在飞书中找到机器人，发送一条消息，完整链路：

```
飞书消息
  → 飞书服务器
  → API Gateway (POST /webhook/feishu)
  → Router Lambda
     ├── 验证 Verification Token
     ├── 查 DynamoDB 白名单 (ALLOW#feishu:ou_xxx)
     ├── 解析/创建用户身份
     └── InvokeAgentRuntime(sessionId, payload)
  → AgentCore → hermes-agent
  → Router Lambda
  → 飞书 reply API (im/v1/messages/{message_id}/reply)
  → 你收到回复
```

### Step 6: 添加用户白名单

```bash
# ---- Telegram 用户 ----
aws dynamodb put-item \
  --table-name hermes-agentcore-identity \
  --item '{
    "PK": {"S": "ALLOW#telegram:123456789"},
    "SK": {"S": "ALLOW"},
    "addedBy": {"S": "admin"},
    "addedAt": {"N": "'$(date +%s)'"}
  }'

# ---- Slack 用户 ----
aws dynamodb put-item \
  --table-name hermes-agentcore-identity \
  --item '{
    "PK": {"S": "ALLOW#slack:U0ABCDEF1"},
    "SK": {"S": "ALLOW"},
    "addedBy": {"S": "admin"},
    "addedAt": {"N": "'$(date +%s)'"}
  }'

# ---- Discord 用户 ----
aws dynamodb put-item \
  --table-name hermes-agentcore-identity \
  --item '{
    "PK": {"S": "ALLOW#discord:987654321"},
    "SK": {"S": "ALLOW"},
    "addedBy": {"S": "admin"},
    "addedAt": {"N": "'$(date +%s)'"}
  }'

# ---- 飞书用户 ----
aws dynamodb put-item \
  --table-name hermes-agentcore-identity \
  --item '{
    "PK": {"S": "ALLOW#feishu:ou_xxxxxxxxxxxx"},
    "SK": {"S": "ALLOW"},
    "addedBy": {"S": "admin"},
    "addedAt": {"N": "'$(date +%s)'"}
  }'

# ---- 批量添加（脚本示例） ----
for uid in 111111 222222 333333; do
  aws dynamodb put-item \
    --table-name hermes-agentcore-identity \
    --item "{\"PK\":{\"S\":\"ALLOW#telegram:$uid\"},\"SK\":{\"S\":\"ALLOW\"}}"
done
```

### Step 7: 端到端验证

```bash
# 1. 检查 AgentCore Runtime 状态
agentcore status

# 2. 检查 API Gateway URL
aws cloudformation describe-stacks \
  --stack-name hermes-agentcore-router \
  --query "Stacks[0].Outputs" \
  --output table

# 3. 检查 Health 端点
API_URL=$(aws cloudformation describe-stacks \
  --stack-name hermes-agentcore-router \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)
curl -s "${API_URL}health" | python3 -m json.tool

# 4. 检查 Lambda 日志
aws logs tail /aws/lambda/hermes-agentcore-router --follow --since 5m

# 5. 检查 CloudWatch Dashboard
echo "打开: https://console.aws.amazon.com/cloudwatch/home#dashboards:name=hermes-agentcore-dashboard"

# 6. 在 Telegram/Slack/Discord 发消息测试
```

**验证清单：**

- [ ] `agentcore status` 显示 Runtime 正常运行
- [ ] `curl {API_URL}health` 返回 `{"status": "healthy"}`
- [ ] Lambda 日志无错误
- [ ] Telegram bot 回复正常
- [ ] Slack bot 回复正常（如已配置）
- [ ] 飞书机器人回复正常（如已配置）
- [ ] CloudWatch Dashboard 可见指标

---

## 7. CDK 栈详细说明

### 部署顺序与依赖关系

```
Phase 1 (独立，无需 Runtime ID):
  hermes-agentcore-vpc
       │
       ▼
  hermes-agentcore-security
       │
       ▼
  hermes-agentcore-agentcore ──── 依赖 vpc + security
  hermes-agentcore-guardrails
  hermes-agentcore-observability

Phase 2 (CDK 外部):
  agentcore deploy ──── 产出: runtimeArn, qualifier → 写入 cdk.json

Phase 3 (需要 Phase 1 + Phase 2):
  hermes-agentcore-router  ──── 依赖 agentcore (IAM, S3)
  hermes-agentcore-cron
  hermes-agentcore-token-monitoring ──── 依赖 observability (SNS topic)
```

### 各栈创建的资源

| 栈 | 资源 | 预计耗时 |
|-----|------|---------|
| **vpc** | VPC, 2 Public + 2 Private 子网, NAT Gateway, S3/DynamoDB Gateway Endpoint, 6 个 Interface Endpoint (Bedrock, SecretsManager, STS, ECR, ECR Docker, CloudWatch Logs) | 3-5 分钟 |
| **security** | KMS CMK (自动轮转), 6 个 Secrets Manager Secret, Cognito User Pool + Client | 1-2 分钟 |
| **guardrails** | Bedrock Guardrail (6 类内容过滤 + 5 类 PII 脱敏) + Version | <1 分钟 |
| **agentcore** | IAM Execution Role (12 条策略), S3 Bucket (版本化, 90 天旧版本过期), Security Group | 1-2 分钟 |
| **observability** | SNS Topic, 3 个 CloudWatch Alarms (Token 预算、Lambda 错误率、Lambda 延迟), Dashboard (4 个面板) | <1 分钟 |
| **router** | DynamoDB Table (PAY_PER_REQUEST + GSI + PITR), Lambda (Python 3.13, 256MB), HTTP API Gateway (4 路由) | 1-2 分钟 |
| **cron** | Lambda (Python 3.13, 256MB, 5min 超时), EventBridge Scheduler IAM Role | <1 分钟 |
| **token-monitoring** | Lambda (Python 3.13, 128MB), EventBridge Rule (每 15 分钟) | <1 分钟 |

---

## 8. 日常运维

### 更新 hermes-agent 版本

```bash
# 1. 更新源码
cd ~/hermes-agent && git pull

# 2. 回到项目目录
cd ~/sample-host-harmesagent-on-amazon-bedrock-agentcore

# 3. 同步源码
rsync -a --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
  ~/hermes-agent/ ./hermes-agent/

# 4. 重新构建并部署
./scripts/deploy.sh phase2
```

> **注意：** `agentcore deploy` 会更新容器镜像。已运行的容器在空闲超时后自动替换为新版本。
> 用户状态通过 S3 同步保留，不会丢失。

### 仅更新 CDK 配置

修改 `cdk.json` 后：

```bash
# 仅更新 CDK 栈（不重建容器）
./scripts/deploy.sh cdk-only

# 或更新单个栈
cdk deploy hermes-agentcore-router
```

### 添加定时任务（Cron）

通过 EventBridge Scheduler 创建定时任务：

```bash
aws scheduler create-schedule \
  --name "hermes-daily-summary" \
  --schedule-expression "cron(0 9 * * ? *)" \
  --flexible-time-window '{"Mode": "OFF"}' \
  --target '{
    "Arn": "'$(aws cloudformation describe-stacks \
      --stack-name hermes-agentcore-cron \
      --query "Stacks[0].Outputs[?OutputKey=='"'"'CronFunctionArn'"'"'].OutputValue" \
      --output text)'",
    "RoleArn": "'$(aws cloudformation describe-stacks \
      --stack-name hermes-agentcore-cron \
      --query "Stacks[0].Outputs[?OutputKey=='"'"'SchedulerRoleArn'"'"'].OutputValue" \
      --output text)'",
    "Input": "{\"jobId\":\"daily_summary\",\"userId\":\"user_abc123\",\"prompt\":\"Summarize today AI news\",\"delivery\":{\"channel\":\"telegram\",\"chatId\":\"123456789\"}}"
  }'
```

### 管理用户白名单

```bash
# 查询某用户是否在白名单
aws dynamodb get-item \
  --table-name hermes-agentcore-identity \
  --key '{"PK":{"S":"ALLOW#telegram:123456"},"SK":{"S":"ALLOW"}}'

# 移除用户白名单
aws dynamodb delete-item \
  --table-name hermes-agentcore-identity \
  --key '{"PK":{"S":"ALLOW#telegram:123456"},"SK":{"S":"ALLOW"}}'

# 查看所有用户
aws dynamodb scan \
  --table-name hermes-agentcore-identity \
  --filter-expression "begins_with(PK, :prefix)" \
  --expression-attribute-values '{":prefix":{"S":"ALLOW#"}}' \
  --query "Items[].PK.S" --output text
```

---

## 9. 监控与告警

### CloudWatch Dashboard

部署完成后自动创建 Dashboard：

```
https://console.aws.amazon.com/cloudwatch/home#dashboards:name=hermes-agentcore-dashboard
```

| 面板 | 指标 | 周期 |
|------|------|------|
| Token Usage | `Hermes/AgentCore` → `TotalTokens` Sum | 5 分钟 |
| Estimated Cost | `Hermes/AgentCore` → `EstimatedCostUSD` Sum | 5 分钟 |
| Token Budget % | `Hermes/AgentCore` → `TokenBudgetUtilization` Max | 15 分钟 |
| Router Errors | `AWS/Lambda` → `Errors` (hermes-agentcore-router) | 5 分钟 |
| Router P99 Latency | `AWS/Lambda` → `Duration` P99 | 5 分钟 |

### 告警规则

| 告警名 | 触发条件 | 动作 |
|--------|---------|------|
| `hermes-agentcore-token-budget-exceeded` | TokenBudgetUtilization > 100% | SNS → 邮件 |
| `hermes-agentcore-router-errors` | Router Lambda Errors > 5 次 (连续 2 个周期) | SNS → 邮件 |
| `hermes-agentcore-router-latency` | Router P99 延迟 > 30s (连续 3 个周期) | SNS → 邮件 |

### 日志查看

```bash
# 容器日志（AgentCore Runtime）
aws logs tail /aws/agentcore/hermes_agent --follow --since 10m

# Router Lambda 日志
aws logs tail /aws/lambda/hermes-agentcore-router --follow --since 5m

# Cron Lambda 日志
aws logs tail /aws/lambda/hermes-agentcore-cron --follow --since 5m

# Token Metrics Lambda 日志
aws logs tail /aws/lambda/hermes-agentcore-token-metrics --follow --since 15m

# 按关键词过滤
aws logs filter-log-events \
  --log-group-name /aws/lambda/hermes-agentcore-router \
  --filter-pattern "ERROR" \
  --start-time $(date -d '1 hour ago' +%s000)
```

---

## 10. 故障排查

### 问题：容器启动失败

**症状：** `agentcore status` 显示 Runtime 不健康；用户发消息无响应。

**排查步骤：**

```bash
# 1. 查看容器日志
aws logs filter-log-events \
  --log-group-name /aws/agentcore/hermes_agent \
  --start-time $(date -d '30 minutes ago' +%s000) \
  --filter-pattern "ERROR"

# 2. 检查 Runtime 状态
agentcore status --verbose

# 3. 手动触发预热
agentcore invoke '{"action":"warmup","userId":"test"}'
```

**常见原因与解决：**

| 原因 | 日志特征 | 解决方案 |
|------|---------|---------|
| `/ping` 超 60s 未响应 | 无 "contract server listening" 日志 | 检查 entrypoint.sh 权限；检查 Python 依赖是否安装完整 |
| ARM64 兼容性问题 | `exec format error` | 确保 `docker buildx build --platform linux/arm64` |
| IAM 权限不足 | `AccessDeniedException` | 检查 `hermes-agentcore-execution-role` 策略 |
| Bedrock 模型未授权 | `ResourceNotFoundException` | 在 Bedrock 控制台启用模型访问 |
| 内存不足 | `MemoryError` / OOM | 联系 AWS 增加 AgentCore 内存配额 |

### 问题：用户消息无响应

**排查步骤：**

```bash
# 1. 检查 Lambda 是否被触发
aws logs tail /aws/lambda/hermes-agentcore-router --since 5m

# 2. 检查用户白名单
aws dynamodb get-item \
  --table-name hermes-agentcore-identity \
  --key '{"PK":{"S":"ALLOW#telegram:USER_ID"},"SK":{"S":"ALLOW"}}'

# 3. 检查 Webhook 状态（Telegram）
TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id hermes/telegram-bot-token \
  --query SecretString --output text)
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | python3 -m json.tool

# 4. 手动调用 API Gateway 健康端点
API_URL=$(aws cloudformation describe-stacks \
  --stack-name hermes-agentcore-router \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)
curl -s "${API_URL}health"
```

**常见原因与解决：**

| 原因 | 解决方案 |
|------|---------|
| 用户不在白名单 | 添加 `ALLOW#telegram:{user_id}` 到 DynamoDB |
| Webhook URL 错误 | 重新运行 `./scripts/setup_telegram.sh` |
| Telegram Webhook pending_update_count 堆积 | 清除挂起更新: `curl "https://api.telegram.org/bot${TOKEN}/setWebhook?url=${WEBHOOK_URL}&drop_pending_updates=true"` |
| Lambda 超时 | 检查 AgentCore 是否需要冷启动；增加 Lambda timeout |
| Session ID < 33 字符 | 代码 bug — 检查 `_build_session_id()` |

### 问题：用户状态丢失

**排查步骤：**

```bash
# 1. 检查 S3 备份
aws s3 ls s3://hermes-agentcore-user-files-{ACCOUNT}-{REGION}/user_abc123/.hermes/ --recursive

# 2. 检查 workspace sync 日志
aws logs filter-log-events \
  --log-group-name /aws/agentcore/hermes_agent \
  --filter-pattern "workspace"

# 3. 强制恢复
agentcore invoke '{"action":"warmup","userId":"user_abc123"}'
```

**常见原因：**
- 容器在 S3 同步前被终止 → SIGTERM handler 应触发最终保存
- S3 权限不足 → 检查 STS scoped credentials
- SQLite 数据库损坏 → 自动删除重建（`_verify_sqlite`）

### 问题：冷启动延迟过高

**期望时间线：**
```
0-2s    预热代理就绪（"Lightweight warm-up agent ready"）
10-30s  完整代理就绪（"Full hermes-agent ready"）
```

**优化方案：**

| 方案 | 效果 |
|------|------|
| 预热代理（已实现） | 前 10-30s 由轻量代理响应 |
| 提前 warmup | 在用户活跃时段提前调用 `{"action":"warmup"}` |
| 减少 Python 依赖 | 精简 Dockerfile 中的 extras |
| 预编译 .pyc | Dockerfile 中已包含 `compileall` |
| 使用 `uv` 替代 `pip` | 更快的包安装 |

---

## 11. 成本优化

### 月度成本估算（10 个活跃用户）

| 组件 | 月费用 | 说明 |
|------|--------|------|
| AgentCore Runtime | $50-150 | 取决于用户会话时长 |
| Bedrock Claude 模型 | $100-500 | 取决于调用频率和模型选择 |
| VPC + NAT Gateway | $30-45 | NAT Gateway 固定 ~$32/月 |
| Lambda + API Gateway | $5-15 | 按请求计费 |
| DynamoDB | $5-10 | 按需模式 |
| S3 | $1-5 | 取决于技能/记忆大小 |
| Secrets Manager | $2-5 | 每个密钥 $0.40/月 |
| CloudWatch | $5-10 | 日志和指标 |
| **合计** | **$200-740** | |

### 降本策略

#### 1. 调整空闲超时

```jsonc
// cdk.json
"session_idle_timeout": 900    // 15 分钟 → 容器回收更快，省钱但冷启动更频繁
```

| 场景 | 推荐值 |
|------|--------|
| 高频用户（每日使用） | `1800` (30 分钟) |
| 中频用户 | `900` (15 分钟) |
| 演示/测试 | `300` (5 分钟) |

#### 2. 选择更便宜的模型

```jsonc
"default_model_id": "global.anthropic.claude-sonnet-4-6-v1",  // 比 Opus 便宜 5x
"warmup_model_id": "global.anthropic.claude-haiku-4-5-20251001-v1"  // 比 Sonnet 便宜 6x
```

| 模型 | 输入价格 (1M tokens) | 输出价格 (1M tokens) | 相对成本 |
|------|---------------------|---------------------|----------|
| Claude Opus 4.6 | $15 | $75 | 5x |
| Claude Sonnet 4.6 | $3 | $15 | 1x |
| Claude Haiku 4.5 | $0.80 | $4 | 0.25x |

#### 3. 去掉 NAT Gateway（纯 Bedrock）

如果不需要访问外部 API（OpenAI、Google 等），可以移除 NAT Gateway，仅使用 VPC Endpoint 访问 Bedrock。节省 ~$32/月。

在 `stacks/vpc_stack.py` 中设 `nat_gateways=0`。

#### 4. Token 预算告警

```jsonc
"daily_token_budget": 1000000,
"daily_cost_budget_usd": 10
```

Token Monitoring Lambda 每 15 分钟检查一次，超预算时通过 SNS 告警。

---

## 12. 安全加固清单

### 基础设施安全

- [ ] **KMS CMK** — 所有 Secrets Manager 密钥使用 KMS 加密（security_stack 自动配置）
- [ ] **VPC 隔离** — AgentCore 容器运行在私有子网，无公网直接访问
- [ ] **VPC Endpoints** — Bedrock、STS、SecretsManager、ECR 通过 VPC Endpoint 访问（不走公网）
- [ ] **STS 范围凭证** — 每个用户容器只能访问自己的 S3 命名空间
- [ ] **容器非 root** — 以 UID 10000 运行（Dockerfile 中配置）

### 应用安全

- [ ] **白名单** — 所有用户必须在 DynamoDB `ALLOW#` 条目中才能使用
- [ ] **密钥管理** — 所有 Token/API Key 存在 Secrets Manager，不硬编码
- [ ] **Telegram 签名** — Router Lambda 验证 Telegram Webhook 签名（可在 bot settings 配置 secret token）
- [ ] **Slack 签名** — Router Lambda 验证 Slack request signing (v0)
- [ ] **Bedrock Guardrails** — 内容过滤（暴力、色情、仇恨等）+ PII 脱敏（邮箱、电话、SSN 等）

### 运维安全

- [ ] **CloudWatch 告警** — 异常错误率、延迟、预算超支自动告警
- [ ] **S3 版本化** — 用户文件桶启用版本控制，防误删
- [ ] **DynamoDB PITR** — 身份表启用时间点恢复
- [ ] **日志保留** — Lambda 日志保留 30 天（可调整）
- [ ] **无密钥硬编码** — `.gitignore` 已排除 `.env`、`*.pem`、`credentials.json`

---

## 13. 附录：AgentCore 合约协议

### 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/ping` | GET | 健康检查，AgentCore 每 ~10 秒轮询一次 |
| `/invocations` | POST | 消息分发，接收所有用户请求 |

### `/ping` 响应

```json
{"status": "Healthy"}       // 空闲，可接受新请求。AgentCore 可能在空闲超时后终止容器。
{"status": "HealthyBusy"}   // 忙碌中。AgentCore 不会终止容器。
```

### `/invocations` 请求格式

```json
{
  "action": "chat|warmup|cron|status",
  "userId": "user_abc123",
  "actorId": "telegram:987654321",
  "channel": "telegram",
  "chatId": "123456789",
  "message": "用户消息内容",
  "images": [{"s3Key": "...", "contentType": "image/jpeg"}],
  "jobId": "daily_summary",
  "config": {"prompt": "...", "delivery": {"channel": "telegram", "chatId": "..."}}
}
```

### Session ID 约束

| 约束 | 值 |
|------|-----|
| 最小长度 | 33 字符 |
| 字符集 | `[a-zA-Z0-9:_-]` |
| 格式 | `{userId}:{channel}:{padding}` |

### 容器生命周期

```
创建 → entrypoint.sh → 工作区初始化 → S3 恢复
  → contract.py 启动 → /ping 返回 Healthy (< 60s 必须)
  → 接受 /invocations → 处理消息
  → ... (空闲)
  → SIGTERM (空闲超时或最大生命周期到)
  → 最终 S3 备份 → Exit(0)
  → (10s 后若未退出) → SIGKILL
```

---

*文档版本: 2026-04-14 | 适用于 hermes-agent v0.8.0 + AgentCore GA*
