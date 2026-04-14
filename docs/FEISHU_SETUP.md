# 飞书 (Feishu/Lark) 配置指南

> **Webhook Endpoint**: `{API_URL}webhook/feishu`
> **模式**: 飞书事件订阅（接收消息事件）
> **架构**: API Gateway → Router Lambda → AgentCore

飞书集成使用 **事件订阅** 模式。当用户给机器人发消息时，飞书服务器将事件推送到 API Gateway，Router Lambda 调用 AgentCore 处理后通过飞书 API 回复消息。

---

## 前置条件

- Phase 3 已部署完成（API Gateway + Router Lambda 就绪）
- 拥有飞书管理员或应用创建权限
- 已获取 API Gateway URL（部署输出中的 `ApiUrl`）

获取 API URL：

```bash
API_URL=$(aws cloudformation describe-stacks \
  --stack-name hermes-agentcore-router \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text)
echo "飞书 Webhook: ${API_URL}webhook/feishu"
```

---

## Step 1: 创建飞书应用

1. 打开 [飞书开放平台](https://open.feishu.cn)，登录你的飞书账号
2. 点击 **创建自建应用**
3. 输入应用名称（例如 `Hermes Agent`），选择图标，点击 **创建**
4. 进入应用详情页

---

## Step 2: 获取应用凭证

在应用详情页获取以下三个凭证：

| 凭证 | 位置 | 说明 |
|------|------|------|
| **App ID** | 凭证与基础信息 | 应用唯一标识，格式如 `cli_a5xxxxx` |
| **App Secret** | 凭证与基础信息 | 应用密钥，用于获取 `tenant_access_token` |
| **Verification Token** | 安全设置 | 用于验证事件请求来源 |

> **注意：** Verification Token 在 **安全设置** 页面，不在凭证页面。如果你的应用使用 v2.0 事件模式，代码已兼容从 `header.token` 中读取。

---

## Step 3: 存入 AWS Secrets Manager

将凭证存入 Secrets Manager（Lambda 运行时从此读取）：

```bash
# App ID
aws secretsmanager create-secret \
  --name "hermes/feishu-app-id" \
  --secret-string "你的App_ID" \
  --region us-west-2

# App Secret
aws secretsmanager create-secret \
  --name "hermes/feishu-app-secret" \
  --secret-string "你的App_Secret" \
  --region us-west-2

# Verification Token
aws secretsmanager create-secret \
  --name "hermes/feishu-verification-token" \
  --secret-string "你的Verification_Token" \
  --region us-west-2
```

> 如果 Secret 已存在，使用 `update-secret` 替代 `create-secret`。

验证：

```bash
aws secretsmanager get-secret-value \
  --secret-id hermes/feishu-app-id \
  --query SecretString --output text --region us-west-2
```

---

## Step 4: 开启机器人能力

1. 应用详情 → 左侧栏 **添加应用能力**
2. 开启 **机器人** 能力

---

## Step 5: 配置权限

**权限管理** 页面，搜索并添加以下权限：

| 权限标识 | 权限名称 | 用途 |
|---------|---------|------|
| `im:message` | 获取与发送消息 | 读取用户发送的消息 |
| `im:message:send_as_bot` | 以应用的身份发消息 | 机器人回复消息 |

---

## Step 6: 配置事件订阅

1. 左侧栏 **事件与回调** → **事件订阅**
2. **订阅方式** 选择 **将事件发送至 开发者服务器**
3. **请求地址** 填写：

```
{API_URL}webhook/feishu
```

例如：

```
https://y2byqip0wj.execute-api.us-west-2.amazonaws.com/webhook/feishu
```

4. 飞书会发送一个 `url_verification` 请求验证地址。Lambda 自动响应 challenge，验证通过后地址变为绿色。

5. 点击 **添加事件**，订阅以下事件：

| 事件 | 事件标识 | 说明 |
|------|---------|------|
| 接收消息 | `im.message.receive_v1` | **必须** — 用户发送消息时触发 |

> 其他事件（如 `im.chat.access_event.bot_p2p_chat_entered_v1`）为可选，当前代码仅处理 `im.message.receive_v1`。

---

## Step 7: 发布应用

1. 左侧栏 **版本管理与发布**
2. 点击 **创建版本**，填写版本号和更新说明
3. 提交审核

> 企业自建应用通常由管理员审核，几分钟内即可通过。发布后用户才能搜索和使用机器人。

---

## Step 8: 获取用户 open_id 并添加白名单

Router Lambda 通过 DynamoDB 白名单控制访问权限。需要将用户的飞书 `open_id` 添加到白名单。

### 方式 A：从日志获取（推荐）

先不添加白名单，直接给机器人发一条消息，然后从 Lambda 日志中获取被拦截的用户 ID：

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/hermes-agentcore-router \
  --filter-pattern "feishu" \
  --region us-west-2
```

日志中会显示：

```
Blocked message from feishu:ou_xxxxxxxxxxxxxxxxxxxx
```

复制 `ou_xxx` 部分。

### 方式 B：通过飞书 API 查询

用邮箱或手机号批量查询 open_id：

```bash
# 获取 tenant_access_token
APP_ID="你的App_ID"
APP_SECRET="你的App_Secret"

TOKEN=$(curl -s -X POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal \
  -H "Content-Type: application/json" \
  -d "{\"app_id\":\"${APP_ID}\",\"app_secret\":\"${APP_SECRET}\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['tenant_access_token'])")

# 用邮箱查 open_id
curl -s -X POST https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"emails":["user@example.com"]}'
```

### 方式 C：飞书管理后台

飞书管理后台 → 组织架构 → 成员详情页 → 可查看 open_id。

### 添加白名单

```bash
FEISHU_OPEN_ID="ou_xxxxxxxxxxxxxxxxxxxx"

aws dynamodb put-item \
  --region us-west-2 \
  --table-name hermes-agentcore-identity \
  --item '{
    "PK": {"S": "ALLOW#feishu:'"${FEISHU_OPEN_ID}"'"},
    "SK": {"S": "ALLOW"},
    "userId": {"S": "feishu:'"${FEISHU_OPEN_ID}"'"},
    "platform": {"S": "feishu"},
    "createdAt": {"N": "'$(date +%s)'"}
  }'
```

### 添加多个用户

```bash
for uid in ou_aaaaaaaaaaaa ou_bbbbbbbbbbbb ou_cccccccccccc; do
  aws dynamodb put-item \
    --region us-west-2 \
    --table-name hermes-agentcore-identity \
    --item '{
      "PK": {"S": "ALLOW#feishu:'"${uid}"'"},
      "SK": {"S": "ALLOW"},
      "userId": {"S": "feishu:'"${uid}"'"},
      "platform": {"S": "feishu"},
      "createdAt": {"N": "'$(date +%s)'"}
    }'
done
```

### 查看飞书白名单

```bash
aws dynamodb scan \
  --region us-west-2 \
  --table-name hermes-agentcore-identity \
  --filter-expression "begins_with(PK, :prefix)" \
  --expression-attribute-values '{":prefix": {"S": "ALLOW#feishu:"}}'
```

### 移除用户

```bash
FEISHU_OPEN_ID="要移除的open_id"

aws dynamodb delete-item \
  --region us-west-2 \
  --table-name hermes-agentcore-identity \
  --key '{
    "PK": {"S": "ALLOW#feishu:'"${FEISHU_OPEN_ID}"'"},
    "SK": {"S": "ALLOW"}
  }'
```

---

## Step 9: 测试

### 预热 Agent

```bash
agentcore invoke "ping" --stream --runtime hermes
```

### 在飞书中测试

在飞书中搜索机器人名称，打开对话，发送一条消息。

### 检查 Lambda 日志

```bash
aws logs tail /aws/lambda/hermes-agentcore-router \
  --region us-west-2 \
  --follow
```

---

## 架构说明

```
用户 ─── 发送消息 ──→ 飞书服务器
                          │
                    事件订阅推送
                (im.message.receive_v1)
                          │
                          ▼
               API Gateway (HTTP API)
              POST /webhook/feishu
                          │
                          ▼
                  Router Lambda
                    │         │
         DynamoDB   │         │  Secrets Manager
         白名单检查  │         │  feishu-app-id/secret
                    ▼         ▼
              验证 token     获取 tenant_access_token
                              │
                              ▼
                    AgentCore Runtime
                    (hermes-agent)
                              │
                              ▼
                    Bedrock Claude API
                              │
                              ▼
                  飞书 Reply API
        (im/v1/messages/{message_id}/reply)
                              │
                              ▼
                    用户收到回复
```

---

## 注意事项

1. **消息类型**：当前仅支持 **文本消息**（`message_type: text`）。图片、文件、富文本等消息类型会被忽略。
2. **tenant_access_token 缓存**：Lambda 在容器级别缓存 token（约 100 分钟刷新一次），避免频繁调用飞书 API。如遇到 token 过期问题，等待 Lambda 容器回收即可。
3. **冷启动**：首次调用 AgentCore 约需 10–30 秒初始化。后续调用几乎即时响应。默认 30 分钟无请求后容器回收。
4. **Session 隔离**：每个飞书用户有独立的 AgentCore session，互不影响。
5. **回复方式**：使用 Reply API（`/reply`）以回复形式发送，保持消息上下文关联。
6. **企业自建应用**：仅企业内部成员可使用，无法跨租户。

---

## 故障排查

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 事件订阅地址验证失败 | Lambda 未正确响应 challenge | 用 `curl -X POST {URL} -d '{"type":"url_verification","challenge":"test"}'` 手动测试 |
| 机器人不回复 | 用户不在白名单 | 查看 Lambda 日志中的 `Blocked message from feishu:ou_xxx`，将其加入白名单 |
| 回复 "Invalid token" | Verification Token 不匹配 | 检查 Secrets Manager 中 `hermes/feishu-verification-token` 的值 |
| 回复 "Sorry, I couldn't process..." | AgentCore 调用失败 | 检查 AgentCore Runtime 状态：`agentcore status` |
| tenant_access_token 获取失败 | App ID 或 App Secret 错误 | 检查 Secrets Manager 中的凭证是否正确 |
| 权限不足 | 未添加 `im:message` 权限 | 在飞书开放平台添加权限并重新发布版本 |
| 非文本消息无响应 | 代码仅处理文本类型 | 预期行为，发送文本消息即可 |

---

## 快速参考

```bash
# 设置环境变量
export FEISHU_APP_ID="你的App_ID"
export FEISHU_APP_SECRET="你的App_Secret"
export FEISHU_VERIFICATION_TOKEN="你的Verification_Token"
export FEISHU_OPEN_ID="你的open_id"

# 存入 Secrets Manager
aws secretsmanager create-secret --name "hermes/feishu-app-id" \
  --secret-string "${FEISHU_APP_ID}" --region us-west-2
aws secretsmanager create-secret --name "hermes/feishu-app-secret" \
  --secret-string "${FEISHU_APP_SECRET}" --region us-west-2
aws secretsmanager create-secret --name "hermes/feishu-verification-token" \
  --secret-string "${FEISHU_VERIFICATION_TOKEN}" --region us-west-2

# 添加白名单
aws dynamodb put-item --region us-west-2 --table-name hermes-agentcore-identity \
  --item '{"PK":{"S":"ALLOW#feishu:'"${FEISHU_OPEN_ID}"'"},"SK":{"S":"ALLOW"},"userId":{"S":"feishu:'"${FEISHU_OPEN_ID}"'"},"platform":{"S":"feishu"},"createdAt":{"N":"'$(date +%s)'"}}'

# 预热 + 测试
agentcore invoke "ping" --stream --runtime hermes
```
