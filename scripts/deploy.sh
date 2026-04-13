#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Three-phase deploy script for Hermes-Agent on Amazon Bedrock AgentCore.
#
# Usage:
#   ./scripts/deploy.sh           # Run all phases
#   ./scripts/deploy.sh phase1    # CDK foundation stacks only
#   ./scripts/deploy.sh phase2    # AgentCore Toolkit (build + deploy runtime)
#   ./scripts/deploy.sh phase3    # CDK dependent stacks only
#   ./scripts/deploy.sh cdk-only  # Phase 1 + Phase 3 (skip runtime build)
# --------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PHASE="${1:-all}"
PROJECT_NAME="hermes-agentcore"
RUNTIME_NAME="hermes_agent"

# Activate virtual environment if present.
if [ -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.venv/bin/activate"
fi

# Use local npx cdk if global cdk is not available.
if command -v cdk &>/dev/null; then
    CDK="cdk"
else
    CDK="npx cdk"
fi

# Colours.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# --------------------------------------------------------------------------
# Phase 1: CDK foundation stacks
# --------------------------------------------------------------------------
phase1() {
    info "=== Phase 1: CDK Foundation Stacks ==="

    # Ensure CDK is bootstrapped.
    if ! aws cloudformation describe-stacks --stack-name CDKToolkit &>/dev/null; then
        info "Bootstrapping CDK …"
        $CDK bootstrap
    fi

    $CDK deploy \
        "${PROJECT_NAME}-vpc" \
        "${PROJECT_NAME}-security" \
        "${PROJECT_NAME}-guardrails" \
        "${PROJECT_NAME}-agentcore" \
        "${PROJECT_NAME}-observability" \
        --require-approval never

    info "Phase 1 complete."
}

# --------------------------------------------------------------------------
# Phase 2: AgentCore Starter Toolkit
# --------------------------------------------------------------------------
phase2() {
    info "=== Phase 2: AgentCore Runtime (build + deploy) ==="

    # Check toolkit is installed.
    if ! command -v agentcore &>/dev/null; then
        info "Installing @aws/agentcore CLI …"
        npm install -g @aws/agentcore
    fi

    # Copy hermes-agent source into the app/hermes/ Docker build context.
    if [ ! -d "$PROJECT_DIR/app/hermes/hermes-agent" ]; then
        if [ -d "$HOME/hermes-agent" ]; then
            info "Copying hermes-agent source into app/hermes/ for Docker build …"
            rsync -a --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
                "$HOME/hermes-agent/" "$PROJECT_DIR/app/hermes/hermes-agent/"
        else
            error "hermes-agent source not found. Place it at ~/hermes-agent"
            exit 1
        fi
    fi

    # Copy bridge/ into app/hermes/ so Dockerfile can access it.
    info "Syncing bridge/ into app/hermes/bridge/ …"
    rsync -a --delete --exclude='__pycache__' --exclude='Dockerfile' \
        "$PROJECT_DIR/bridge/" "$PROJECT_DIR/app/hermes/bridge/"

    # Build and deploy via agentcore CLI.
    info "Deploying to AgentCore …"
    agentcore deploy --yes --verbose

    # Extract runtime IDs and write back to cdk.json.
    info "Extracting runtime IDs …"
    STATUS_JSON=$(agentcore status --json 2>/dev/null || echo "{}")
    RUNTIME_ARN=$(echo "$STATUS_JSON" | jq -r '
        .runtimes[0].agentRuntimeArn //
        .runtimes[0].runtimeArn //
        .agentRuntimeArn //
        .runtimeArn //
        empty' 2>/dev/null || echo "")
    QUALIFIER=$(echo "$STATUS_JSON" | jq -r '
        .runtimes[0].agentRuntimeId //
        .runtimes[0].qualifier //
        .qualifier //
        .endpointId //
        empty' 2>/dev/null || echo "")

    if [ -n "$RUNTIME_ARN" ]; then
        info "Runtime ARN:  $RUNTIME_ARN"
        info "Qualifier:    $QUALIFIER"

        # Update cdk.json with runtime IDs.
        TMP=$(mktemp)
        jq ".context.agentcore_runtime_arn = \"$RUNTIME_ARN\" | \
            .context.agentcore_qualifier = \"$QUALIFIER\"" \
            cdk.json > "$TMP" && mv "$TMP" cdk.json

        info "cdk.json updated with runtime IDs."
    else
        warn "Could not extract runtime IDs automatically."
        warn "Run 'agentcore status --json' and set agentcore_runtime_arn / agentcore_qualifier in cdk.json manually."
    fi

    info "Phase 2 complete."
}

# --------------------------------------------------------------------------
# Phase 3: CDK dependent stacks
# --------------------------------------------------------------------------
phase3() {
    info "=== Phase 3: CDK Dependent Stacks ==="

    # Verify runtime IDs are set.
    RUNTIME_ARN=$(jq -r '.context.agentcore_runtime_arn // empty' cdk.json)
    if [ -z "$RUNTIME_ARN" ]; then
        warn "agentcore_runtime_arn not set in cdk.json — Lambda will not be able to invoke AgentCore."
        warn "Run Phase 2 first, or set the values manually."
    fi

    $CDK deploy \
        "${PROJECT_NAME}-router" \
        "${PROJECT_NAME}-cron" \
        "${PROJECT_NAME}-token-monitoring" \
        --require-approval never

    # Print API URL.
    API_URL=$(aws cloudformation describe-stacks \
        --stack-name "${PROJECT_NAME}-router" \
        --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
        --output text 2>/dev/null || echo "")

    if [ -n "$API_URL" ]; then
        info "API Gateway URL: $API_URL"
        info "Webhook endpoints:"
        info "  Telegram: ${API_URL}webhook/telegram"
        info "  Slack:    ${API_URL}webhook/slack"
        info "  Discord:  ${API_URL}webhook/discord"
    fi

    info "Phase 3 complete."
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
case "$PHASE" in
    all)
        phase1
        phase2
        phase3
        ;;
    phase1)
        phase1
        ;;
    phase2)
        phase2
        ;;
    phase3)
        phase3
        ;;
    cdk-only)
        phase1
        phase3
        ;;
    *)
        error "Usage: $0 [all|phase1|phase2|phase3|cdk-only]"
        exit 1
        ;;
esac

info "=== Deploy complete ==="
