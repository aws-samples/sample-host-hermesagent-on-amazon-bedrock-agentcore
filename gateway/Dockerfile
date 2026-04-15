FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git libsqlite3-0 ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install hermes-agent with messaging + feishu extras.
# The full agent is needed because the gateway module has import
# dependencies on agent internals.  No LLM calls are made — all AI
# inference runs inside AgentCore microVMs.
COPY hermes-agent/ /app/hermes-agent/
RUN pip install --no-cache-dir \
    "/app/hermes-agent[messaging,feishu,cron,mcp]" \
    boto3

# Copy the AgentCore proxy bridge.
COPY agentcore_proxy.py /app/agentcore_proxy.py
COPY weixin_file_patch.py /app/weixin_file_patch.py
COPY main.py /app/main.py

# Simple health-check endpoint — curl localhost:8080/health
# The gateway itself does not serve HTTP, so we use a tiny sidecar.
COPY healthcheck.py /app/healthcheck.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app:/app/hermes-agent
ENV HERMES_HOME=/opt/data
ENV HERMES_HEADLESS=1
ENV HERMES_QUIET=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:8080/health || exit 1

CMD ["python", "/app/main.py"]
