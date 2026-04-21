#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Set up Feishu (Lark) bot — supports both Webhook and WebSocket modes.
#
# Feishu has two connection modes:
#
#   Webhook (Phase 3 — Lambda):
#     Feishu pushes events to API Gateway → Lambda router.
#     Requires a public URL and event subscription configuration.
#
#   WebSocket (Phase 4 — ECS Gateway):
#     ECS container connects to Feishu via persistent WebSocket.
#     No public URL needed. Lower latency, recommended for production.
#
# Prerequisites:
#   1. A Feishu app created at https://open.feishu.cn (or https://open.larksuite.com)
#   2. Bot capability enabled, with permissions: im:message, im:message:send_as_bot
#
# Usage:
#   ./scripts/setup_feishu.sh              # Interactive mode selection
#   ./scripts/setup_feishu.sh webhook      # Webhook mode (Phase 3)
#   ./scripts/setup_feishu.sh websocket    # WebSocket mode (Phase 4)
# --------------------------------------------------------------------------
set -euo pipefail

PROJECT_NAME="hermes-agentcore"
MODE="${1:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
header() { echo -e "\n${CYAN}$*${NC}"; }

# --------------------------------------------------------------------------
# Mode selection
# --------------------------------------------------------------------------
if [ -z "$MODE" ]; then
    header "============================================================"
    header " Feishu Bot Setup — Choose Connection Mode"
    header "============================================================"
    echo ""
    echo "  1) webhook    — Feishu pushes events to API Gateway (Phase 3)"
    echo "                   Requires public URL + event subscription config"
    echo ""
    echo "  2) websocket  — ECS Gateway connects to Feishu via WebSocket (Phase 4)"
    echo "                   No public URL needed, lower latency (recommended)"
    echo ""
    read -rp "Select mode [1/2]: " CHOICE
    case "$CHOICE" in
        1|webhook)   MODE="webhook" ;;
        2|websocket) MODE="websocket" ;;
        *) error "Invalid choice. Usage: $0 [webhook|websocket]"; exit 1 ;;
    esac
fi

# --------------------------------------------------------------------------
# Step 1: Create Feishu App (common for both modes)
# --------------------------------------------------------------------------
header "============================================================"
header " Step 1: Create Feishu App"
header "============================================================"
echo ""
echo "  1. Go to https://open.feishu.cn/app (飞书) or https://open.larksuite.com/app (Lark)"
echo "  2. Click 'Create Custom App' (创建自建应用)"
echo "  3. Fill in app name and description"
echo "  4. Under 'Capabilities' (添加应用能力), enable 'Bot' (机器人)"
echo "  5. Under 'Permissions' (权限管理), add:"
echo "     - im:message              (Read messages)"
echo "     - im:message:send_as_bot  (Send messages as bot)"
echo "  6. Copy the App ID and App Secret from 'Credentials' (凭证与基础信息)"
echo ""

# --------------------------------------------------------------------------
# Step 2: Collect credentials
# --------------------------------------------------------------------------
header "============================================================"
header " Step 2: Store Credentials"
header "============================================================"
echo ""

read -rp "Enter Feishu App ID: " APP_ID
if [ -z "$APP_ID" ]; then
    error "App ID is required."
    exit 1
fi

read -rp "Enter Feishu App Secret: " APP_SECRET
if [ -z "$APP_SECRET" ]; then
    error "App Secret is required."
    exit 1
fi

info "Storing App ID in Secrets Manager (hermes/feishu-app-id) …"
aws secretsmanager put-secret-value \
    --secret-id "hermes/feishu-app-id" \
    --secret-string "$APP_ID" \
    --no-cli-pager 2>/dev/null \
|| aws secretsmanager create-secret \
    --name "hermes/feishu-app-id" \
    --secret-string "$APP_ID" \
    --no-cli-pager

info "Storing App Secret in Secrets Manager (hermes/feishu-app-secret) …"
aws secretsmanager put-secret-value \
    --secret-id "hermes/feishu-app-secret" \
    --secret-string "$APP_SECRET" \
    --no-cli-pager 2>/dev/null \
|| aws secretsmanager create-secret \
    --name "hermes/feishu-app-secret" \
    --secret-string "$APP_SECRET" \
    --no-cli-pager

info "Credentials stored."

# --------------------------------------------------------------------------
# Step 3: Verify credentials by fetching tenant_access_token
# --------------------------------------------------------------------------
header "============================================================"
header " Step 3: Verify Credentials"
header "============================================================"

VERIFY_RESULT=$(curl -s -X POST "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal" \
    -H "Content-Type: application/json" \
    -d "{\"app_id\": \"$APP_ID\", \"app_secret\": \"$APP_SECRET\"}")

VERIFY_CODE=$(echo "$VERIFY_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('code','-1'))" 2>/dev/null || echo "-1")

if [ "$VERIFY_CODE" = "0" ]; then
    info "Credentials verified — tenant_access_token obtained successfully."
else
    VERIFY_MSG=$(echo "$VERIFY_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('msg','unknown'))" 2>/dev/null || echo "unknown")
    warn "Credential verification failed: code=$VERIFY_CODE, msg=$VERIFY_MSG"
    warn "Check your App ID and App Secret. Continuing anyway …"
fi

# --------------------------------------------------------------------------
# Mode-specific setup
# --------------------------------------------------------------------------
if [ "$MODE" = "webhook" ]; then
    # ------------------------------------------------------------------
    # Webhook Mode (Phase 3 — Lambda Router)
    # ------------------------------------------------------------------
    header "============================================================"
    header " Step 4: Configure Webhook Mode (Phase 3)"
    header "============================================================"

    info "Retrieving API Gateway URL …"
    API_URL=$(aws cloudformation describe-stacks \
        --stack-name "${PROJECT_NAME}-router" \
        --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
        --output text 2>/dev/null || echo "")

    if [ -z "$API_URL" ]; then
        error "Could not find API URL. Is Phase 3 (router stack) deployed?"
        error "Run: ./scripts/deploy.sh phase3"
        exit 1
    fi

    WEBHOOK_URL="${API_URL}webhook/feishu"

    echo ""
    echo "  Configure in Feishu Developer Console:"
    echo ""
    echo "  1. Go to 'Event Subscriptions' (事件订阅)"
    echo "     - Request URL: ${WEBHOOK_URL}"
    echo "     - Click 'Verify' — should succeed"
    echo ""
    echo "  2. Under 'Subscribed Events' (添加事件), subscribe to:"
    echo "     - im.message.receive_v1  (Receive messages)"
    echo ""
    echo "  3. (Optional) Copy the Verification Token and Encrypt Key"
    echo "     for enhanced security — not required for basic setup."
    echo ""
    echo "  4. Publish the app version (创建版本 → 申请发布)"
    echo ""

    # Optional: store verification token
    read -rp "Enter Verification Token (optional, press Enter to skip): " VERIFY_TOKEN
    if [ -n "$VERIFY_TOKEN" ]; then
        info "Storing Verification Token …"
        aws secretsmanager put-secret-value \
            --secret-id "hermes/feishu-verification-token" \
            --secret-string "$VERIFY_TOKEN" \
            --no-cli-pager 2>/dev/null \
        || aws secretsmanager create-secret \
            --name "hermes/feishu-verification-token" \
            --secret-string "$VERIFY_TOKEN" \
            --no-cli-pager
    fi

else
    # ------------------------------------------------------------------
    # WebSocket Mode (Phase 4 — ECS Gateway)
    # ------------------------------------------------------------------
    header "============================================================"
    header " Step 4: Configure WebSocket Mode (Phase 4)"
    header "============================================================"
    echo ""
    echo "  WebSocket mode does NOT require a public URL or event subscription."
    echo "  The ECS gateway container connects to Feishu's WebSocket endpoint."
    echo ""
    echo "  In Feishu Developer Console:"
    echo ""
    echo "  1. Publish the app version (创建版本 → 申请发布)"
    echo "  2. That's it — no webhook URL needed!"
    echo ""
    echo "  The ECS gateway reads FEISHU_APP_ID and FEISHU_APP_SECRET from"
    echo "  Secrets Manager and connects automatically via WebSocket."
    echo ""

    # Check Phase 4 deployment status.
    ECS_STATUS=$(aws ecs describe-services \
        --cluster "${PROJECT_NAME}-gateway" \
        --services "${PROJECT_NAME}-gateway" \
        --query "services[0].status" \
        --output text 2>/dev/null || echo "NOT_FOUND")

    if [ "$ECS_STATUS" = "ACTIVE" ]; then
        info "ECS Gateway is running. Feishu WebSocket will connect on next task restart."
        echo ""
        echo "  To pick up the new credentials, force a new ECS deployment:"
        echo "  aws ecs update-service \\"
        echo "    --cluster ${PROJECT_NAME}-gateway \\"
        echo "    --service ${PROJECT_NAME}-gateway \\"
        echo "    --force-new-deployment --no-cli-pager"
    else
        warn "ECS Gateway not found. Deploy Phase 4 first:"
        warn "  ./scripts/deploy.sh phase4"
    fi
fi

# --------------------------------------------------------------------------
# Step 5: Allowlist
# --------------------------------------------------------------------------
header "============================================================"
header " Step 5: Add Users to Allowlist"
header "============================================================"
echo ""
echo "  Users must be added to the DynamoDB allowlist before they can chat."
echo "  You need each user's Feishu open_id (starts with 'ou_')."
echo ""
echo "  To find a user's open_id:"
echo "    - In Feishu Admin Console → Contacts → select user → copy open_id"
echo "    - Or check the Lambda/ECS logs after the user sends a message"
echo ""
echo "  To add a user:"
echo "  aws dynamodb put-item \\"
echo "    --table-name ${PROJECT_NAME}-identity \\"
echo "    --item '{\"PK\":{\"S\":\"ALLOW#feishu:ou_XXXXXX\"},\"SK\":{\"S\":\"ALLOW\"}}'"
echo ""

read -rp "Add a user now? Enter open_id (or press Enter to skip): " OPEN_ID
if [ -n "$OPEN_ID" ]; then
    info "Adding feishu:${OPEN_ID} to allowlist …"
    aws dynamodb put-item \
        --table-name "${PROJECT_NAME}-identity" \
        --item "{\"PK\":{\"S\":\"ALLOW#feishu:${OPEN_ID}\"},\"SK\":{\"S\":\"ALLOW\"}}" \
        --no-cli-pager
    info "User added."
fi

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
header "============================================================"
header " Feishu Setup Complete ($MODE mode)"
header "============================================================"
echo ""
if [ "$MODE" = "webhook" ]; then
    info "Webhook URL: ${WEBHOOK_URL}"
    info "Mode: Webhook → API Gateway → Lambda Router (Phase 3)"
else
    info "Mode: WebSocket → ECS Fargate Gateway (Phase 4)"
    info "No public URL required."
fi
echo ""
info "Secrets stored:"
info "  hermes/feishu-app-id"
info "  hermes/feishu-app-secret"
echo ""
