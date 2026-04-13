# Discord 配置指南

> **Webhook Endpoint**: `https://f4dn6ml831.execute-api.us-west-2.amazonaws.com/webhook/discord`
> **模式**: Discord Interactions（斜杠命令）
> **架构**: API Gateway → Router Lambda → AgentCore

Phase 3 的 Discord 集成使用 **Discord Interactions Endpoint**（斜杠命令），而非传统的 WebSocket 长连接机器人。用户通过 `/ask` 斜杠命令发送消息，Lambda 调用 AgentCore 并直接返回响应。

---

## Step 1: 创建 Discord Application

1. 打开 [Discord Developer Portal](https://discord.com/developers/applications)，登录你的 Discord 帐号
2. 点击右上角 **New Application**
3. 输入名称（例如 `Hermes Agent`），接受开发者条款，点击 **Create**
4. 在 **General Information** 页面记录：
   - **Application ID** — 后续用于构建邀请链接和注册命令
   - **Public Key** — Discord 用于验证交互签名

---

## Step 2: 创建 Bot 并获取 Token

1. 左侧栏点击 **Bot**
2. 在 **Authorization Flow** 下：
   - **Public Bot** → **ON**
   - **Require OAuth2 Code Grant** → **OFF**
3. 在 **Privileged Gateway Intents** 下启用：

| Intent | 用途 | 必须？ |
|--------|------|--------|
| Presence Intent | 查看用户在线状态 | 可选 |
| **Server Members Intent** | 访问成员列表、解析用户名 | **必须** |
| **Message Content Intent** | 读取消息文本内容 | **必须** |

4. 点击 **Save Changes**
5. 回到 **Token** 区域，点击 **Reset Token**，复制 Bot Token

> ⚠️ Token 只显示一次。丢失后需重新生成。切勿公开分享或提交到 Git。

---

## Step 3: 设置 Interactions Endpoint URL

1. 左侧栏点击 **General Information**
2. 在 **Interactions Endpoint URL** 填入：

```
https://f4dn6ml831.execute-api.us-west-2.amazonaws.com/webhook/discord
```

3. 点击 **Save Changes**

Discord 会立即向该 URL 发送一个 Ping 验证请求（`type=1`），Lambda 返回 `{"type": 1}` 完成握手。如果保存成功，说明 Endpoint 验证通过。

---

## Step 4: 注册斜杠命令

Lambda 从交互 payload 的 `options` 中提取 `name="message"` 参数，需注册对应的斜杠命令。

### 全局命令（所有服务器，约 1 小时生效）

```bash
APP_ID="你的Application_ID"
BOT_TOKEN="你的Bot_Token"

curl -X POST "https://discord.com/api/v10/applications/${APP_ID}/commands" \
  -H "Authorization: Bot ${BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ask",
    "description": "Ask Hermes Agent a question",
    "type": 1,
    "options": [
      {
        "name": "message",
        "description": "Your message to Hermes",
        "type": 3,
        "required": true
      }
    ]
  }'
```

### Guild 命令（仅指定服务器，几分钟内生效，适合测试）

```bash
APP_ID="你的Application_ID"
BOT_TOKEN="你的Bot_Token"
GUILD_ID="你的服务器ID"

curl -X POST "https://discord.com/api/v10/applications/${APP_ID}/guilds/${GUILD_ID}/commands" \
  -H "Authorization: Bot ${BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ask",
    "description": "Ask Hermes Agent a question",
    "type": 1,
    "options": [
      {
        "name": "message",
        "description": "Your message to Hermes",
        "type": 3,
        "required": true
      }
    ]
  }'
```

> **获取 Guild ID**：在 Discord 开启开发者模式（设置 → 高级 → 开发者模式），右键服务器名称 → **Copy Server ID**

### 验证命令注册

```bash
# 查看全局命令
curl "https://discord.com/api/v10/applications/${APP_ID}/commands" \
  -H "Authorization: Bot ${BOT_TOKEN}"

# 查看 Guild 命令
curl "https://discord.com/api/v10/applications/${APP_ID}/guilds/${GUILD_ID}/commands" \
  -H "Authorization: Bot ${BOT_TOKEN}"
```

---

## Step 5: 邀请 Bot 到服务器

用以下 URL 邀请（替换 `YOUR_APP_ID`）：

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=274878286912
```

包含的权限：

| 权限 | 说明 |
|------|------|
| View Channels | 查看频道 |
| Send Messages | 发送消息 |
| Send Messages in Threads | 在线程中发送消息 |
| Embed Links | 富文本链接 |
| Attach Files | 发送文件 |
| Read Message History | 读取历史消息 |
| Add Reactions | 添加表情反应 |

邀请步骤：

1. 在浏览器打开上述 URL
2. 在 **Add to Server** 下拉菜单选择你的服务器
3. 点击 **Continue** → **Authorize**
4. 完成验证码（如有）

---

## Step 6: 获取 Discord User ID

1. 打开 Discord 客户端（桌面或网页版）
2. 进入 **设置** → **高级** → 开启 **开发者模式**
3. 关闭设置
4. 右键点击你的用户名 → **Copy User ID**

你会得到一个类似 `284102345871466496` 的数字。

---

## Step 7: 添加用户到白名单

Router Lambda 通过 DynamoDB `hermes-agentcore-identity` 表检查用户权限。将你的 Discord User ID 加入白名单：

```bash
DISCORD_USER_ID="你的Discord用户ID"

aws dynamodb put-item \
  --region us-west-2 \
  --table-name hermes-agentcore-identity \
  --item '{
    "PK": {"S": "ALLOW#discord:'"${DISCORD_USER_ID}"'"},
    "SK": {"S": "ALLOW"},
    "userId": {"S": "discord:'"${DISCORD_USER_ID}"'"},
    "platform": {"S": "discord"},
    "createdAt": {"N": "'$(date +%s)'"}
  }'
```

### 添加多个用户

```bash
for uid in 284102345871466496 198765432109876543; do
  aws dynamodb put-item \
    --region us-west-2 \
    --table-name hermes-agentcore-identity \
    --item '{
      "PK": {"S": "ALLOW#discord:'"${uid}"'"},
      "SK": {"S": "ALLOW"},
      "userId": {"S": "discord:'"${uid}"'"},
      "platform": {"S": "discord"},
      "createdAt": {"N": "'$(date +%s)'"}
    }'
done
```

### 查看白名单

```bash
aws dynamodb scan \
  --region us-west-2 \
  --table-name hermes-agentcore-identity \
  --filter-expression "begins_with(PK, :prefix)" \
  --expression-attribute-values '{":prefix": {"S": "ALLOW#discord:"}}'
```

### 移除用户

```bash
DISCORD_USER_ID="要移除的用户ID"

aws dynamodb delete-item \
  --region us-west-2 \
  --table-name hermes-agentcore-identity \
  --key '{
    "PK": {"S": "ALLOW#discord:'"${DISCORD_USER_ID}"'"},
    "SK": {"S": "ALLOW"}
  }'
```

---

## Step 8: 测试

### 预热 Agent（避免冷启动超时）

```bash
agentcore invoke "ping" --stream --runtime hermes
```

### 在 Discord 中测试

输入斜杠命令：

```
/ask message:你好，请介绍一下你自己
```

Hermes Agent 会通过 AgentCore 处理并直接回复。

### 检查 Lambda 日志

```bash
aws logs tail /aws/lambda/hermes-agentcore-router \
  --region us-west-2 \
  --follow
```

---

## 架构说明

```
用户 ─── /ask 命令 ──→ Discord API
                          │
                    Interactions Endpoint
                          │
                          ▼
               API Gateway (HTTP API)
              POST /webhook/discord
                          │
                          ▼
                  Router Lambda
                    │         │
         DynamoDB   │         │  AgentCore
         白名单检查  │         │  调用 runtime
                    ▼         ▼
              hermes-agentcore-identity
                              │
                              ▼
                    AgentCore Runtime
                    (hermes-agent)
                              │
                              ▼
                    Bedrock Claude API
```

---

## 注意事项

1. **响应长度限制**：Discord 交互响应最大 **2000 字符**，超出部分会被截断。
2. **交互超时**：Discord 要求 3 秒内返回初始响应。当前 Lambda 是同步调用 AgentCore，如果 AgentCore 冷启动超过 3 秒，Discord 会显示 "This interaction failed"。解决方案：
   - 保持 Agent 预热（定期发送 ping）
   - 或改为 Deferred Response 模式（返回 `type: 5`，后续通过 Webhook 编辑消息）
3. **冷启动**：首次调用 AgentCore 约需 10–30 秒初始化。后续调用在同一容器内几乎即时响应。默认 30 分钟无请求后容器回收。
4. **Session 隔离**：每个 Discord 用户有独立的 AgentCore session，互不影响。
5. **Region**：所有 AWS 资源部署在 `us-west-2`。

---

## 故障排查

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| Interactions Endpoint URL 保存失败 | Lambda 未正确响应 Ping | 检查 Lambda 日志，确认 `/webhook/discord` 路由正常 |
| `/ask` 命令不显示 | 命令未注册或未生效 | 全局命令需约 1 小时，Guild 命令几分钟。用 API 检查命令列表 |
| "This interaction failed" | AgentCore 冷启动超时 | 先发送 `agentcore invoke "ping"` 预热 |
| "Access denied" | 用户不在白名单 | 检查 DynamoDB 中是否有对应的 `ALLOW#discord:{user_id}` 记录 |
| 响应被截断 | 超过 2000 字符限制 | Discord 交互限制，当前无法绕过 |
| Lambda 500 错误 | AgentCore 运行时异常 | 检查 Lambda 日志和 `agentcore logs --runtime hermes` |

---

## 快速参考

```bash
# 设置环境变量
export APP_ID="你的Application_ID"
export BOT_TOKEN="你的Bot_Token"
export GUILD_ID="你的服务器ID"
export DISCORD_USER_ID="你的Discord用户ID"

# 注册 Guild 命令（快速测试）
curl -X POST "https://discord.com/api/v10/applications/${APP_ID}/guilds/${GUILD_ID}/commands" \
  -H "Authorization: Bot ${BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"name":"ask","description":"Ask Hermes Agent a question","type":1,"options":[{"name":"message","description":"Your message to Hermes","type":3,"required":true}]}'

# 添加白名单
aws dynamodb put-item --region us-west-2 --table-name hermes-agentcore-identity \
  --item '{"PK":{"S":"ALLOW#discord:'"${DISCORD_USER_ID}"'"},"SK":{"S":"ALLOW"},"userId":{"S":"discord:'"${DISCORD_USER_ID}"'"},"platform":{"S":"discord"},"createdAt":{"N":"'$(date +%s)'"}}'

# 预热 + 测试
agentcore invoke "ping" --stream --runtime hermes
```
