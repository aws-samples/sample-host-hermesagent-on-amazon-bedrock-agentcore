# Hermes Agent on Amazon Bedrock AgentCore

[English](README.md) | 中文

在 **Amazon Bedrock AgentCore** 上部署 [Hermes Agent](https://github.com/NousResearch/hermes-agent) — 每用户独立 Firecracker 微虚拟机，自动弹性伸缩，原生 Bedrock Claude 模型，多频道消息接入。

## 架构

```
Telegram / Slack / Discord / 飞书             微信 / 飞书 (长连接)
         │                                          │
    API Gateway                              ECS Fargate 网关
         │                                          │
    Router Lambda ──→ AgentCore Runtime ←── AgentCore Proxy
                           │
                    ┌──────────────┐
                    │  main.py     │  AgentCore 入口
                    │  Hermes Agent│  40+ 工具、技能、记忆
                    │  Bedrock API │  Claude (SigV4 认证)
                    └──────────────┘
```

## 核心特性

- **用户级隔离** — 每个用户独占一个 Firecracker 微虚拟机
- **全托管无服务器** — 无需管理服务器，自动弹性伸缩，按使用量付费
- **多频道接入** — Telegram、Slack、Discord、飞书通过 Webhook；微信通过 ECS 网关
- **原生 Bedrock** — Claude 模型通过 SigV4 认证调用（无需 API Key）
- **基础设施即代码** — 9 个 CDK 栈，四阶段部署
- **持久化状态** — S3 备份工作区，跨会话保留记忆和技能

## 工作原理

Hermes Agent 无需修改即可运行在 AgentCore 容器中。关键集成是 `app/hermes/main.py` 中的 **monkey-patch**，将 `anthropic.Anthropic` 透明替换为 `anthropic.AnthropicBedrock`，所有 API 调用自动通过 Bedrock SigV4 认证路由。这意味着：

- 无需 Anthropic API Key
- 所有请求使用 AWS IAM 凭证
- 模型访问受 Bedrock 策略管控
- Hermes Agent 源码保持不变

## 项目结构

```
├── app/hermes/              # 运行时应用
│   ├── main.py              # AgentCore 入口 (Anthropic → Bedrock monkey-patch)
│   ├── Dockerfile           # 多阶段容器构建
│   ├── entrypoint.sh        # 工作区初始化
│   └── pyproject.toml       # Python 依赖
├── bridge/                  # AgentCore 合约桥接层
│   ├── contract.py          # HTTP 服务器 (/ping, /invocations)
│   ├── workspace_sync.py    # S3 ↔ SQLite 同步
│   └── bedrock_provider.py  # Bedrock 模型配置
├── gateway/                 # ECS Fargate 网关 (Phase 4)
│   ├── main.py              # 网关入口
│   ├── agentcore_proxy.py   # AIAgent → AgentCore 代理 (monkey-patch)
│   ├── weixin_file_patch.py # 长文本自动转 .md 文件发送到微信
│   ├── healthcheck.py       # ECS 健康检查 HTTP 服务
│   └── Dockerfile           # 网关容器镜像
├── lambda/                  # AWS Lambda 函数
│   ├── router/              # 频道 Webhook → AgentCore 路由
│   ├── cron/                # 定时任务执行
│   └── token_metrics/       # 用量追踪
├── stacks/                  # CDK 栈定义
│   ├── vpc_stack.py         # VPC、子网、NAT Gateway
│   ├── security_stack.py    # KMS、Secrets Manager、Cognito
│   ├── guardrails_stack.py  # Bedrock Guardrails 内容过滤
│   ├── agentcore_stack.py   # IAM 角色、S3 工作区桶
│   ├── observability_stack.py   # CloudWatch 仪表盘与告警
│   ├── router_stack.py      # API Gateway + Router Lambda
│   ├── gateway_stack.py     # ECS Fargate 网关 (微信 + 飞书)
│   ├── cron_stack.py        # 定时调用
│   └── token_monitoring_stack.py # Token 用量分析
├── agentcore/               # AgentCore CLI 配置
│   ├── agentcore.json       # Runtime 定义（构建方式、入口、网络模式）
│   └── aws-targets.json     # 部署目标（AWS 账号 ID + 区域）
├── scripts/
│   └── deploy.sh            # 三阶段部署编排脚本
├── docs/                    # 文档
├── hermes-agent/            # Git submodule (hermes-agent 源码)
├── app.py                   # CDK 入口
├── cdk.json                 # CDK 配置
└── requirements.txt         # Python CDK 依赖
```

## 前置条件

- **AWS 账号**：已开通 Bedrock 模型访问（Claude Sonnet/Opus）
- **AWS CLI**：已配置凭证
- **Node.js** >= 18（AWS CDK 依赖）
- **Python** >= 3.10
- **Docker**（容器构建，需支持 ARM64 buildx）
- **AgentCore CLI**：`npm install -g @aws/agentcore`
- **TypeScript**：`npm install -g typescript@5`

## 部署

### 快速开始

```bash
# 克隆项目
git clone https://github.com/stevensu1977/sample-host-hermesagent-on-amazon-bedrock-agentcore.git
cd sample-host-hermesagent-on-amazon-bedrock-agentcore

# 克隆 hermes-agent 源码
git clone https://github.com/NousResearch/hermes-agent.git ~/hermes-agent

# 初始化 Python 环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 安装 CDK 依赖
npm install

# 部署前检查：确认 AgentCore 配置中的 AWS 账号 ID
# 必须与当前凭证账号一致
aws sts get-caller-identity --query Account --output text
cat agentcore/aws-targets.json  # 确认 account 字段正确

# 一键三阶段部署
./scripts/deploy.sh all
```

### 分阶段部署

```bash
# Phase 1: 基础设施（VPC、安全、Guardrails、IAM、监控）
./scripts/deploy.sh phase1

# Phase 2: 构建并部署 Hermes Agent 容器到 AgentCore
./scripts/deploy.sh phase2

# Phase 3: Router Lambda、定时任务、Token 监控
./scripts/deploy.sh phase3

# Phase 4（可选）: ECS 网关，支持微信、飞书长连接
./scripts/deploy.sh phase4
```

### Phase 4: ECS 网关（可选）

Phase 4 部署一个 **ECS Fargate 网关**，运行 hermes-agent 的平台适配器（微信长轮询、飞书 WebSocket），以持久化容器方式运行。所有 AI 推理通过 `AgentCoreProxyAgent` 转发到 AgentCore — 网关只处理平台协议。

**需要 Phase 4 的场景：**
- 微信（企业微信）— 需要持久化长轮询连接
- 飞书 WebSocket — 比 Webhook 模式延迟更低

**部署内容：**
- ECR 镜像仓库
- ECS Fargate 集群 + 服务（单任务，自动重启）
- VPC 网络、安全组、IAM 角色
- Secrets Manager 集成，管理平台凭证

**部署后配置平台凭证：**

```bash
# 微信
aws secretsmanager put-secret-value --secret-id hermes/weixin/token --secret-string '你的Token'

# 飞书
aws secretsmanager put-secret-value --secret-id hermes/feishu/app-id --secret-string '你的AppID'
aws secretsmanager put-secret-value --secret-id hermes/feishu/app-secret --secret-string '你的AppSecret'
```

**特性：**
- 长文本自动转换为 `.md` 文件发送到微信（阈值可通过 `WEIXIN_FILE_THRESHOLD` 配置）
- 会话历史转发到 AgentCore，支持多轮对话上下文
- 冷启动重试，指数退避（5s → 10s → 20s）
- 健康检查端点 `http://localhost:8080/health`

### 关键配置文件

部署前需确认以下配置：

| 文件 | 作用 | 必须修改的字段 |
|------|------|---------------|
| `agentcore/aws-targets.json` | 部署目标 | `account`（你的 AWS 账号 ID）、`region` |
| `agentcore/agentcore.json` | Runtime 定义 | 通常无需修改 |
| `cdk.json` | CDK 基础设施配置 | `agentcore_runtime_arn`（Phase 2 自动填入） |

详见 [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) 中的「3.1 AgentCore 配置文件说明」。

## 调用方式

### AgentCore CLI

```bash
# 单次对话
agentcore invoke "你好，你是谁？" --stream --runtime hermes

# 多轮对话
agentcore invoke "我叫小明" --stream --runtime hermes --session-id s001
agentcore invoke "我叫什么名字？" --stream --runtime hermes --session-id s001
```

### Python SDK (boto3)

```python
import boto3, json

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:你的账号ID:runtime/YOUR_RUNTIME_ID"
client = boto3.client("bedrock-agentcore", region_name="us-west-2")

response = client.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_ARN,
    payload=json.dumps({"prompt": "你好！"}).encode("utf-8"),
)

result = response.get("response", "")
if hasattr(result, "read"):
    result = result.read().decode("utf-8")
print(result)
```

更多调用方式参见 [docs/INVOKE_GUIDE.md](docs/INVOKE_GUIDE.md)。

## 频道接入

### Telegram

```bash
./scripts/setup_telegram.sh
```

### Slack

```bash
./scripts/setup_slack.sh
```

### Discord

1. 在 [Discord 开发者门户](https://discord.com/developers/applications) 创建 Application
2. 设置 Interactions Endpoint URL 为 API Gateway 的 `/webhook/discord`
3. 注册 `/ask` 斜杠命令
4. 添加用户到 DynamoDB 白名单

详见 [docs/DISCORD_SETUP.md](docs/DISCORD_SETUP.md)。

### 微信（Phase 4 — ECS 网关）

微信通过 **iLink Bot API** (`ilinkai.weixin.qq.com`) 接入个人微信账号，需要持久化长轮询连接，仅通过 Phase 4 的 ECS 网关支持。

#### 第一步：获取微信 Token

Token 通过**扫码登录**获取，不是固定的 API Key。

在本地运行 hermes-agent 网关配置：

```bash
cd ~/hermes-agent
pip install -e ".[messaging]"
hermes gateway setup
```

选择 **Weixin**，配置流程会：
1. 从 iLink Bot API 请求登录二维码
2. 在终端显示二维码
3. **用微信扫描二维码**，在手机上确认授权
4. 返回凭证：`WEIXIN_ACCOUNT_ID` 和 `WEIXIN_TOKEN`

凭证保存在 `~/.hermes/.env`，也可在 `~/.hermes/weixin/accounts/{account_id}.json` 中找到。

> **注意：** Token 是**会话令牌**，非永久密钥。会话过期后需要重新扫码。

#### 第二步：部署并配置

```bash
# 部署 Phase 4
./scripts/deploy.sh phase4

# 将凭证存入 Secrets Manager
aws secretsmanager put-secret-value \
  --secret-id hermes/weixin/token \
  --secret-string '第一步获取的TOKEN'

aws secretsmanager put-secret-value \
  --secret-id hermes/weixin/account-id \
  --secret-string '第一步获取的ACCOUNT_ID'
```

#### 可选环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WEIXIN_DM_POLICY` | `open` | 私聊策略：`open`（开放）、`allowlist`（白名单）、`disabled`（禁用）、`pairing`（配对审批） |
| `WEIXIN_ALLOWED_USERS` | (空) | 白名单模式下允许的用户 ID，逗号分隔 |
| `WEIXIN_GROUP_POLICY` | `disabled` | 群聊策略：`open`、`allowlist`、`disabled` |
| `WEIXIN_FILE_THRESHOLD` | `2000` | 超过此字符数的回复自动转为 `.md` 文件发送 |

### 飞书 (Feishu/Lark)

1. 在 [飞书开放平台](https://open.feishu.cn) 创建自建应用
2. 开启机器人能力，配置权限（`im:message`、`im:message:send_as_bot`）
3. 事件订阅地址填写 `{API_URL}webhook/feishu`，订阅 `im.message.receive_v1` 事件
4. 将 App ID、App Secret、Verification Token 存入 Secrets Manager
5. 添加用户 open_id 到 DynamoDB 白名单

详见 [docs/FEISHU_SETUP.md](docs/FEISHU_SETUP.md)。

## 配置

`cdk.json` 中的关键配置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `default_model_id` | `global.anthropic.claude-opus-4-6-v1` | 主模型 |
| `warmup_model_id` | `global.anthropic.claude-sonnet-4-6-v1` | 预热模型（冷启动时使用） |
| `enable_guardrails` | `false` | Bedrock Guardrails 内容过滤 |
| `session_idle_timeout` | `1800` | 会话空闲超时（秒） |
| `daily_token_budget` | `2000000` | 每日 Token 预算 |
| `daily_cost_budget_usd` | `20` | 每日成本上限（美元） |

## 成本估算（10 个活跃用户）

| 组件 | 月费用 |
|------|--------|
| AgentCore Runtime | $50–150 |
| Bedrock Claude 模型 | $100–500 |
| VPC + NAT | $30–45 |
| Lambda + API GW + DynamoDB | $15–25 |
| ECS Fargate 网关 (Phase 4) | $15–30 |
| S3 + Secrets + CloudWatch | $10–20 |
| **合计** | **~$220–770** |

## 文档

| 文档 | 说明 |
|------|------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构设计与组件交互 |
| [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | 完整部署指南与故障排查 |
| [INVOKE_GUIDE.md](docs/INVOKE_GUIDE.md) | 所有调用方式（CLI、SDK、HTTP） |
| [DISCORD_SETUP.md](docs/DISCORD_SETUP.md) | Discord 机器人配置 |
| [FEISHU_SETUP.md](docs/FEISHU_SETUP.md) | 飞书机器人配置 |
| [AGENTCORE_CONTRACT.md](docs/AGENTCORE_CONTRACT.md) | HTTP 合约协议详情 |

## 参考

基于 [sample-host-openclaw-on-amazon-bedrock-agentcore](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore) 的架构模式。

## 许可

本项目为示例部署指南。Hermes Agent 由 [Nous Research](https://nousresearch.com/) 开发。
