#!/usr/bin/env bash
# --------------------------------------------------------------------------
# Container entrypoint for hermes-agent on AgentCore.
#
# 1. Sets up the workspace directory structure.
# 2. Optionally drops privileges to the hermes user.
# 3. Launches the AgentCore contract server.
# --------------------------------------------------------------------------
set -euo pipefail

# Force IPv4 DNS resolution — best practice inside AgentCore VPC.
export NODE_OPTIONS="${NODE_OPTIONS:---dns-result-order=ipv4first}"

# ---- Workspace setup -----------------------------------------------------

HERMES_HOME="${HERMES_HOME:-/mnt/workspace/.hermes}"
export HERMES_HOME

mkdir -p "${HERMES_HOME}"
mkdir -p "${HERMES_HOME}/memories"
mkdir -p "${HERMES_HOME}/skills"
mkdir -p "${HERMES_HOME}/sessions"
mkdir -p "${HERMES_HOME}/logs"
mkdir -p "${HERMES_HOME}/cache"
mkdir -p "${HERMES_HOME}/cron"

# If bundled SOUL.md exists and workspace doesn't have one yet, copy it.
if [ -f /app/hermes-agent/docker/SOUL.md ] && [ ! -f "${HERMES_HOME}/SOUL.md" ]; then
    cp /app/hermes-agent/docker/SOUL.md "${HERMES_HOME}/SOUL.md"
fi

# ---- Privilege drop (optional) -------------------------------------------

TARGET_UID="${HERMES_UID:-10000}"
CURRENT_UID=$(id -u)

if [ "$CURRENT_UID" = "0" ] && [ "$TARGET_UID" != "0" ]; then
    # Fix ownership so the non-root user can write.
    chown -R "${TARGET_UID}:${TARGET_UID}" "${HERMES_HOME}" 2>/dev/null || true
    chown -R "${TARGET_UID}:${TARGET_UID}" /mnt/workspace 2>/dev/null || true

    echo "[entrypoint] Dropping privileges to UID ${TARGET_UID}"
    exec gosu "${TARGET_UID}" python -m bridge.contract "$@"
fi

# Already running as non-root or root was requested.
exec python -m bridge.contract "$@"
