# --------------------------------------------------------------------------
# Hermes-Agent on Amazon Bedrock AgentCore — ARM64 container image
# --------------------------------------------------------------------------
# Multi-stage build:
#   Stage 1 (builder)  — install all Python dependencies
#   Stage 2 (runtime)  — minimal image with only what's needed at runtime
# --------------------------------------------------------------------------

# ---- Stage 1: builder ----------------------------------------------------
FROM --platform=linux/arm64 python:3.11-slim AS builder

WORKDIR /build

# System build deps (needed for C extensions like SQLite, crypto, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make libffi-dev libsqlite3-dev git curl \
    && rm -rf /var/lib/apt/lists/*

# Copy hermes-agent source (mounted or copied at build time)
COPY hermes-agent/ /build/hermes-agent/

# Install hermes-agent + bridge deps into a prefix we can copy later.
RUN pip install --no-cache-dir --prefix=/install \
        -e "/build/hermes-agent[bedrock,cron,mcp]" \
        boto3 botocore litellm \
    && pip install --no-cache-dir --prefix=/install \
        boto3 botocore litellm

# Pre-compile .pyc files for faster cold start.
RUN python -m compileall -q /install/lib 2>/dev/null || true


# ---- Stage 2: runtime ----------------------------------------------------
FROM --platform=linux/arm64 python:3.11-slim

WORKDIR /app

# Runtime system deps only.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsqlite3-0 curl ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-installed Python packages from builder.
COPY --from=builder /install /usr/local

# Copy hermes-agent source (needed at runtime — it's an editable install).
COPY --from=builder /build/hermes-agent /app/hermes-agent

# Copy bridge components.
COPY bridge/ /app/bridge/

# Make bridge a proper package on sys.path.
ENV PYTHONPATH="/app:/app/hermes-agent"

# Create non-root user.
RUN useradd -u 10000 -m -d /opt/data -s /bin/bash hermes \
    && mkdir -p /opt/data/.hermes /mnt/workspace/.hermes \
    && chown -R hermes:hermes /opt/data /mnt/workspace

# Persistent volume (AgentCore session storage).
VOLUME ["/mnt/workspace"]

# AgentCore health-check port.
EXPOSE 8080

# Environment defaults.
ENV HERMES_HEADLESS=1 \
    AGENTCORE_MODE=1 \
    HERMES_HOME=/mnt/workspace/.hermes \
    PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=0

# Entrypoint handles privilege dropping + workspace init.
COPY bridge/entrypoint.sh /app/bridge/entrypoint.sh
RUN chmod +x /app/bridge/entrypoint.sh

ENTRYPOINT ["/app/bridge/entrypoint.sh"]
