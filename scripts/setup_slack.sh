#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Print Slack app setup instructions and register the webhook URL.
#
# Prerequisites:
#   1. Slack bot token stored in Secrets Manager: hermes/slack-bot-token
#   2. Signing secret stored: hermes/slack-signing-secret
#   3. Phase 3 deployed (API Gateway URL available)
#
# Usage:
#   ./scripts/setup_slack.sh
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

WEBHOOK_URL="${API_URL}webhook/slack"

echo ""
echo "============================================================"
echo " Slack App Setup Instructions"
echo "============================================================"
echo ""
echo "1. Go to https://api.slack.com/apps"
echo "2. Create a new app (or select existing)."
echo ""
echo "3. Under 'Event Subscriptions':"
echo "   - Enable events"
echo "   - Request URL: ${WEBHOOK_URL}"
echo "   - Subscribe to bot events: message.im, message.channels"
echo ""
echo "4. Under 'OAuth & Permissions':"
echo "   - Bot Token Scopes: chat:write, channels:history, im:history"
echo "   - Install to workspace"
echo "   - Copy the Bot User OAuth Token"
echo ""
echo "5. Store credentials in Secrets Manager:"
echo "   aws secretsmanager put-secret-value \\"
echo "     --secret-id hermes/slack-bot-token \\"
echo "     --secret-string 'xoxb-YOUR-TOKEN'"
echo ""
echo "   aws secretsmanager put-secret-value \\"
echo "     --secret-id hermes/slack-signing-secret \\"
echo "     --secret-string 'YOUR-SIGNING-SECRET'"
echo ""
echo "6. Add Slack users to the allowlist:"
echo "   aws dynamodb put-item --table-name ${PROJECT_NAME}-identity \\"
echo "     --item '{\"PK\":{\"S\":\"ALLOW#slack:U12345\"},\"SK\":{\"S\":\"ALLOW\"}}'"
echo ""
echo "============================================================"
