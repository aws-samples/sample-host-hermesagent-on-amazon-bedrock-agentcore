# Hermes Agent on Amazon Bedrock AgentCore

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on **Amazon Bedrock AgentCore** — per-user Firecracker microVMs with automatic scaling, native Bedrock Claude models, and multi-channel messaging.

## Architecture

```
Telegram / Slack / Discord
         │
    API Gateway
         │
    Router Lambda ──→ AgentCore Runtime (Firecracker microVM)
                           │
                    ┌──────────────┐
                    │  main.py     │  AgentCore entrypoint
                    │  Hermes Agent│  40+ tools, skills, memory
                    │  Bedrock API │  Claude via SigV4 auth
                    └──────────────┘
```

## Key Features

- **Per-user isolation** — Firecracker microVMs, one per session
- **Serverless** — No servers to manage, auto-scaling, pay-per-use
- **Multi-channel** — Telegram, Slack, Discord via webhook integration
- **Native Bedrock** — Claude models via SigV4 auth (no API keys needed)
- **Infrastructure as Code** — 8 CDK stacks, three-phase deployment
- **Persistent state** — S3-backed workspace for memory and sessions

## How It Works

Hermes Agent runs unmodified inside AgentCore containers. The key integration is a **monkey-patch** in `app/hermes/main.py` that transparently replaces `anthropic.Anthropic` with `anthropic.AnthropicBedrock`, routing all API calls through Bedrock with SigV4 authentication. This means:

- No Anthropic API key needed
- All requests use AWS IAM credentials
- Model access governed by Bedrock policies
- Hermes Agent source code remains unchanged

## Project Structure

```
├── app/hermes/              # Runtime application
│   ├── main.py              # AgentCore entrypoint (Anthropic → Bedrock monkey-patch)
│   ├── Dockerfile           # Multi-stage container build
│   ├── entrypoint.sh        # Workspace initialization
│   └── pyproject.toml       # Python dependencies
├── bridge/                  # AgentCore contract bridge
│   ├── contract.py          # HTTP server (/ping, /invocations)
│   ├── workspace_sync.py    # S3 ↔ SQLite sync
│   └── bedrock_provider.py  # Bedrock model configuration
├── lambda/                  # AWS Lambda functions
│   ├── router/              # Channel webhook → AgentCore dispatcher
│   ├── cron/                # Scheduled task execution
│   └── token_metrics/       # Usage tracking
├── stacks/                  # CDK stack definitions
│   ├── vpc_stack.py         # VPC, subnets, NAT Gateway
│   ├── security_stack.py    # KMS, Secrets Manager, Cognito
│   ├── guardrails_stack.py  # Bedrock Guardrails
│   ├── agentcore_stack.py   # IAM roles, S3 workspace bucket
│   ├── observability_stack.py   # CloudWatch dashboards & alarms
│   ├── router_stack.py      # API Gateway + Router Lambda
│   ├── cron_stack.py        # Scheduled invocations
│   └── token_monitoring_stack.py # Token usage analytics
├── scripts/
│   └── deploy.sh            # Three-phase deployment orchestrator
├── docs/                    # Documentation
├── hermes-agent/            # Git submodule (hermes-agent source)
├── app.py                   # CDK entry point
├── cdk.json                 # CDK configuration
└── requirements.txt         # Python CDK dependencies
```

## Prerequisites

- **AWS Account** with Bedrock model access enabled (Claude Sonnet/Opus)
- **AWS CLI** configured with credentials
- **Node.js** >= 18 (for AWS CDK)
- **Python** >= 3.10
- **Docker** (for container builds)
- **AgentCore CLI**: `npm install -g @aws/agentcore`

## Deployment

### Quick Start

```bash
# Clone
git clone https://github.com/aws-samples/sample-host-hermesagent-on-amazon-bedrock-agentcore.git
cd sample-host-hermesagent-on-amazon-bedrock-agentcore

# Setup Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install CDK
npm install

# Deploy all three phases
./scripts/deploy.sh all
```

### Phase-by-Phase

```bash
# Phase 1: Foundation (VPC, security, guardrails, IAM)
./scripts/deploy.sh phase1

# Phase 2: Build & deploy Hermes Agent container to AgentCore
./scripts/deploy.sh phase2

# Phase 3: Router Lambda, cron, monitoring
./scripts/deploy.sh phase3
```

## Invocation

### AgentCore CLI

```bash
# Single message
agentcore invoke "Hello, who are you?" --stream --runtime hermes

# Multi-turn conversation
agentcore invoke "My name is Steven" --stream --runtime hermes --session-id s001
agentcore invoke "What's my name?" --stream --runtime hermes --session-id s001
```

### Python SDK (boto3)

```python
import boto3, json

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:ACCOUNT_ID:runtime/YOUR_RUNTIME_ID"
client = boto3.client("bedrock-agentcore", region_name="us-west-2")

response = client.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_ARN,
    payload=json.dumps({"prompt": "Hello!"}).encode("utf-8"),
)

result = response.get("response", "")
if hasattr(result, "read"):
    result = result.read().decode("utf-8")
print(result)
```

See [docs/INVOKE_GUIDE.md](docs/INVOKE_GUIDE.md) for AWS CLI, JavaScript SDK, and HTTP API examples.

## Channel Integration

### Discord

1. Create Discord Application at [Developer Portal](https://discord.com/developers/applications)
2. Set Interactions Endpoint URL to your API Gateway webhook endpoint
3. Register `/ask` slash command
4. Add users to DynamoDB allowlist

See [docs/DISCORD_SETUP.md](docs/DISCORD_SETUP.md) for step-by-step instructions.

### Telegram / Slack

Configure webhooks after Phase 3 deployment:

```bash
./scripts/setup_telegram.sh
./scripts/setup_slack.sh
```

## Configuration

Key settings in `cdk.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| `default_model_id` | `global.anthropic.claude-opus-4-6-v1` | Primary Bedrock model |
| `enable_guardrails` | `false` | Bedrock Guardrails toggle |
| `session_idle_timeout` | `1800` | Session idle timeout (seconds) |
| `daily_token_budget` | `2000000` | Daily token limit |
| `daily_cost_budget_usd` | `20` | Daily cost cap (USD) |

## Cost Estimate (10 active users)

| Component | Monthly |
|-----------|---------|
| AgentCore Runtime | $50–150 |
| Bedrock Claude | $100–500 |
| VPC + NAT | $30–45 |
| Lambda + API GW + DynamoDB | $15–25 |
| S3 + Secrets + CloudWatch | $10–20 |
| **Total** | **~$210–740** |

## Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and component interactions |
| [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Step-by-step setup and troubleshooting |
| [INVOKE_GUIDE.md](docs/INVOKE_GUIDE.md) | All invocation methods (CLI, SDK, HTTP) |
| [DISCORD_SETUP.md](docs/DISCORD_SETUP.md) | Discord bot configuration |
| [AGENTCORE_CONTRACT.md](docs/AGENTCORE_CONTRACT.md) | HTTP contract protocol details |

## Reference

Based on the patterns from [sample-host-openclaw-on-amazon-bedrock-agentcore](https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore).

## License

This project is provided as a sample deployment guide. Hermes Agent is developed by [Nous Research](https://nousresearch.com/).
