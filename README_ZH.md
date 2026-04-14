# Hermes Agent on Amazon Bedrock AgentCore

[English](README.md) | 中文

在 **Amazon Bedrock AgentCore** 上部署 [Hermes Agent](https://github.com/NousResearch/hermes-agent) — 每用户独立 Firecracker 微虚拟机，自动弹性伸缩，原生 Bedrock Claude 模型，多频道消息接入。

## 架构

```
Telegram / Slack / Discord / 飞书
         │
    API Gateway
         │
    Router Lambda ──→ AgentCore Runtime (Firecracker 微虚拟机)
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
- **多频道接入** — Telegram、Slack、Discord、飞书 (Feishu/Lark) 通过 Webhook 集成
- **原生 Bedrock** — Claude 模型通过 SigV4 认证调用（无需 API Key）
- **基础设施即代码** — 8 个 CDK 栈，三阶段部署
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
```

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
| S3 + Secrets + CloudWatch | $10–20 |
| **合计** | **~$210–740** |

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
