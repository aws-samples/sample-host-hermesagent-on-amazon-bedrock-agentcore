#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Set up Telegram bot webhook pointing to the API Gateway endpoint.
#
# Prerequisites:
#   1. Bot token stored in Secrets Manager: hermes/telegram-bot-token
#   2. Phase 3 deployed (API Gateway URL available)
#
# Usage:
#   ./scripts/setup_telegram.sh
# --------------------------------------------------------------------------
set -euo pipefail

PROJECT_NAME="hermes-agentcore"

echo "[INFO] Retrieving API Gateway URL …"
API_URL=$(aws cloudformation describe-stacks \
    --stack-name "${PROJECT_NAME}-router" \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
    --output text)

if [ -z "$API_URL" ]; then
    echo "[ERROR] Could not find API URL. Is the router stack deployed?"
    exit 1
fi

WEBHOOK_URL="${API_URL}webhook/telegram"
echo "[INFO] Webhook URL: $WEBHOOK_URL"

echo "[INFO] Retrieving Telegram bot token from Secrets Manager …"
TOKEN=$(aws secretsmanager get-secret-value \
    --secret-id "hermes/telegram-bot-token" \
    --query "SecretString" --output text)

if [ -z "$TOKEN" ]; then
    echo "[ERROR] No token found in hermes/telegram-bot-token."
    exit 1
fi

echo "[INFO] Setting Telegram webhook …"
RESULT=$(curl -s "https://api.telegram.org/bot${TOKEN}/setWebhook" \
    -d "url=${WEBHOOK_URL}" \
    -d "allowed_updates=[\"message\",\"edited_message\"]" \
    -d "max_connections=40")

echo "[INFO] Telegram API response: $RESULT"

# Verify.
echo ""
echo "[INFO] Verifying webhook info …"
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | python3 -m json.tool

echo ""
echo "[INFO] Telegram webhook setup complete."
echo "[INFO] Users must be added to the allowlist in DynamoDB before they can chat."
echo "[INFO] To add a user:"
echo "  aws dynamodb put-item --table-name ${PROJECT_NAME}-identity --item '{\"PK\":{\"S\":\"ALLOW#telegram:USER_ID\"},\"SK\":{\"S\":\"ALLOW\"}}'"
