# Hermes Agent on AgentCore — 调用指南

> **Runtime ARN**: `arn:aws:bedrock-agentcore:us-west-2:YOUR_ACCOUNT_ID:runtime/YOUR_RUNTIME_ID`
> **Region**: `us-west-2`
> **Model**: `us.anthropic.claude-sonnet-4-6`

---

## 1. AgentCore CLI（推荐）

最简单的调用方式，需先进入项目目录：

```bash
cd /home/ubuntu/sample-host-harmesagent-on-amazon-bedrock-agentcore
```

### 单次调用

```bash
agentcore invoke "你好，请介绍一下你自己" --stream --runtime hermes
```

### 多轮对话（保持上下文）

通过 `--session-id` 将多次调用关联到同一会话：

```bash
agentcore invoke "我叫 Steven，请记住" --stream --runtime hermes --session-id session-001
agentcore invoke "我叫什么名字？" --stream --runtime hermes --session-id session-001
```

### 指定 User ID

```bash
agentcore invoke "你好" --stream --runtime hermes --user-id user-steven
```

### JSON 输出

```bash
agentcore invoke "1+1等于几" --runtime hermes --json
```

### 交互模式（TUI）

不带参数启动交互式终端界面：

```bash
agentcore invoke
```

### 查看日志

```bash
# 实时流式日志
agentcore logs --runtime hermes

# 最近 30 分钟的日志
agentcore logs --runtime hermes --since 30m

# 仅看错误
agentcore logs --runtime hermes --level error
```

### 查看状态

```bash
agentcore status
agentcore status --json
```

---

## 2. AWS CLI

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --region us-west-2 \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-west-2:YOUR_ACCOUNT_ID:runtime/YOUR_RUNTIME_ID" \
  --payload '{"prompt": "你好，请用中文回答"}' \
  /dev/stdout
```

### 带 Session ID

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --region us-west-2 \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-west-2:YOUR_ACCOUNT_ID:runtime/YOUR_RUNTIME_ID" \
  --runtime-session-id "my-session-001" \
  --payload '{"prompt": "记住我的名字是 Steven"}' \
  /dev/stdout
```

### 带 User ID

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --region us-west-2 \
  --agent-runtime-arn "arn:aws:bedrock-agentcore:us-west-2:YOUR_ACCOUNT_ID:runtime/YOUR_RUNTIME_ID" \
  --runtime-user-id "user-steven" \
  --payload '{"prompt": "你好"}' \
  /dev/stdout
```

---

## 3. Python SDK (boto3)

### 安装依赖

```bash
pip install boto3
```

### 基本调用

```python
import boto3
import json

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:YOUR_ACCOUNT_ID:runtime/YOUR_RUNTIME_ID"

client = boto3.client("bedrock-agentcore", region_name="us-west-2")

response = client.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_ARN,
    payload=json.dumps({"prompt": "你好，请用中文介绍你自己"}).encode("utf-8"),
)

# 读取流式响应
for event in response["body"]:
    chunk = event.get("chunk", {}).get("bytes", b"")
    if chunk:
        print(chunk.decode("utf-8"), end="")
print()
```

### 多轮对话

```python
import boto3
import json

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:YOUR_ACCOUNT_ID:runtime/YOUR_RUNTIME_ID"
SESSION_ID = "python-session-001"

client = boto3.client("bedrock-agentcore", region_name="us-west-2")


def invoke(prompt: str, session_id: str = SESSION_ID) -> str:
    """调用 Hermes Agent 并返回响应文本。"""
    response = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}).encode("utf-8"),
    )
    chunks = []
    for event in response["body"]:
        chunk = event.get("chunk", {}).get("bytes", b"")
        if chunk:
            chunks.append(chunk.decode("utf-8"))
    return "".join(chunks)


# 多轮对话示例
print(invoke("我叫 Steven，我是一名软件工程师"))
print(invoke("我叫什么？做什么工作？"))
```

### 封装为异步调用

```python
import asyncio
import boto3
import json
from concurrent.futures import ThreadPoolExecutor

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:YOUR_ACCOUNT_ID:runtime/YOUR_RUNTIME_ID"

executor = ThreadPoolExecutor(max_workers=4)
client = boto3.client("bedrock-agentcore", region_name="us-west-2")


def _invoke_sync(prompt: str, session_id: str = "") -> str:
    kwargs = {
        "agentRuntimeArn": RUNTIME_ARN,
        "payload": json.dumps({"prompt": prompt}).encode("utf-8"),
    }
    if session_id:
        kwargs["runtimeSessionId"] = session_id

    response = client.invoke_agent_runtime(**kwargs)
    chunks = []
    for event in response["body"]:
        chunk = event.get("chunk", {}).get("bytes", b"")
        if chunk:
            chunks.append(chunk.decode("utf-8"))
    return "".join(chunks)


async def invoke_async(prompt: str, session_id: str = "") -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _invoke_sync, prompt, session_id)


# 使用示例
async def main():
    result = await invoke_async("你好！今天天气怎么样？")
    print(result)

asyncio.run(main())
```

---

## 4. JavaScript / TypeScript SDK

### 安装依赖

```bash
npm install @aws-sdk/client-bedrock-agentcore
```

### 调用示例

```typescript
import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";

const RUNTIME_ARN =
  "arn:aws:bedrock-agentcore:us-west-2:YOUR_ACCOUNT_ID:runtime/YOUR_RUNTIME_ID";

const client = new BedrockAgentCoreClient({ region: "us-west-2" });

async function invoke(prompt: string): Promise<string> {
  const command = new InvokeAgentRuntimeCommand({
    agentRuntimeArn: RUNTIME_ARN,
    payload: new TextEncoder().encode(JSON.stringify({ prompt })),
  });

  const response = await client.send(command);
  const chunks: string[] = [];

  if (response.body) {
    for await (const event of response.body) {
      if (event.chunk?.bytes) {
        chunks.push(new TextDecoder().decode(event.chunk.bytes));
      }
    }
  }

  return chunks.join("");
}

// 使用
const answer = await invoke("你好！");
console.log(answer);
```

---

## 5. HTTP API（curl + SigV4 签名）

AgentCore 使用 AWS SigV4 认证，需要签名请求。可通过 `awscurl` 工具简化：

### 安装 awscurl

```bash
pip install awscurl
```

### 调用

```bash
awscurl --service bedrock-agentcore \
  --region us-west-2 \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"prompt": "你好"}' \
  "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3AYOUR_ACCOUNT_ID%3Aruntime%2FYOUR_RUNTIME_ID/invocations"
```

---

## 6. Telegram / Slack / Discord（需部署 Phase 3）

部署 Phase 3 后，可通过聊天平台直接对话：

```bash
cd /home/ubuntu/sample-host-harmesagent-on-amazon-bedrock-agentcore
./scripts/deploy.sh phase3
```

部署完成后配置 Webhook：

```bash
# Telegram
./scripts/setup_telegram.sh

# Slack
./scripts/setup_slack.sh
```

Phase 3 创建的资源：
- **API Gateway** — 接收 Webhook 请求
- **Router Lambda** — 解析消息 → 调用 AgentCore → 回复用户
- **DynamoDB** — 用户身份映射与鉴权

---

## Payload 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 用户消息（必填） |
| `message` | string | `prompt` 的别名，二选一 |
| `channel` | string | 来源渠道（`agentcore` / `telegram` / `slack`） |
| `chatId` | string | 聊天 ID（可选，传递给 agent 的上下文） |

### 示例 Payload

```json
{
  "prompt": "帮我写一个 Python 快速排序",
  "channel": "agentcore"
}
```

---

## 注意事项

1. **冷启动**：首次调用会触发 agent 初始化，约需 10-30 秒。后续调用在同一容器内几乎即时响应。
2. **Session 隔离**：每个 `session-id` 对应独立的对话上下文，不同 session 之间互不影响。
3. **空闲超时**：默认 30 分钟无请求后容器会被回收，下次调用重新冷启动。
4. **Region**：当前部署在 `us-west-2`，所有 SDK 调用需指定该 region。
5. **认证**：所有调用需要有效的 AWS 凭证，且 IAM 策略需包含 `bedrock-agentcore:InvokeAgentRuntime` 权限。

---

## 快速验证

```bash
# 验证 agent 是否在线
agentcore status --json

# 发送测试消息
agentcore invoke "ping" --stream --runtime hermes
```
