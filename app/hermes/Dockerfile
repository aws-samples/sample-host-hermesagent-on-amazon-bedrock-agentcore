# --------------------------------------------------------------------------
# Hermes-Agent on Amazon Bedrock AgentCore — Container image
# --------------------------------------------------------------------------
# Multi-stage build:
#   Stage 1 (builder)  — install all Python dependencies
#   Stage 2 (runtime)  — minimal image with only what's needed at runtime
# --------------------------------------------------------------------------

# ---- Stage 1: builder ----------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.11-slim AS builder

WORKDIR /build

# System build deps (needed for C extensions like SQLite, crypto, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make libffi-dev libsqlite3-dev git curl \
    && rm -rf /var/lib/apt/lists/*

# Copy hermes-agent source (mounted or copied at build time)
COPY hermes-agent/ /build/hermes-agent/

# Install hermes-agent + bridge deps into a prefix we can copy later.
RUN pip install --no-cache-dir --prefix=/install \
        -e "/build/hermes-agent[cron,mcp]" \
        boto3 botocore litellm bedrock-agentcore

# Pre-compile .pyc files for faster cold start.
RUN python -m compileall -q /install/lib 2>/dev/null || true


# ---- Stage 2: runtime ----------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Runtime system deps only.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsqlite3-0 curl ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-installed Python packages from builder.
COPY --from=builder /install /usr/local

# Copy hermes-agent source (needed at runtime — it's an editable install).
COPY --from=builder /build/hermes-agent /app/hermes-agent

# Copy bridge components (workspace_sync, etc.).
COPY bridge/ /app/bridge/

# Copy main entrypoint.
COPY main.py /app/main.py

# Make everything importable.
ENV PYTHONPATH="/app:/app/hermes-agent"

# Create non-root user.
RUN useradd -m -u 1000 bedrock_agentcore \
    && mkdir -p /mnt/workspace/.hermes \
    && chown -R bedrock_agentcore:bedrock_agentcore /mnt/workspace

# Persistent volume (AgentCore session storage).
VOLUME ["/mnt/workspace"]

# AgentCore contract ports.
EXPOSE 8080 8000 9000

# Environment defaults.
ENV HERMES_HEADLESS=1 \
    AGENTCORE_MODE=1 \
    HERMES_HOME=/mnt/workspace/.hermes \
    PORT=8080 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1

# Entrypoint handles workspace init + starts the contract server.
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER bedrock_agentcore

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "main"]
