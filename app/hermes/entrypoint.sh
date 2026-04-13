#!/usr/bin/env bash
# Workspace init + hermes-agent contract server start.
set -e

WORKSPACE="${HERMES_HOME:-/mnt/workspace/.hermes}"

# Create workspace directories if they don't exist.
for dir in memories skills sessions logs cache cron; do
    mkdir -p "$WORKSPACE/$dir" 2>/dev/null || true
done

# Copy bundled SOUL.md if user doesn't have one yet.
if [ ! -f "$WORKSPACE/SOUL.md" ] && [ -f /app/hermes-agent/SOUL.md ]; then
    cp /app/hermes-agent/SOUL.md "$WORKSPACE/SOUL.md"
fi

exec "$@"
