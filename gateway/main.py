#!/usr/bin/env python3
"""ECS Gateway entry point — Phase 4.

Patches AIAgent with AgentCoreProxyAgent, then starts the hermes-agent
gateway.  All AI inference runs in AgentCore microVMs; this container
handles only platform protocol adapters (WeChat long-poll, Feishu
WebSocket, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# Ensure hermes-agent modules are importable.
HERMES_AGENT_DIR = os.environ.get("HERMES_AGENT_DIR", "/app/hermes-agent")
if HERMES_AGENT_DIR not in sys.path:
    sys.path.insert(0, HERMES_AGENT_DIR)

# Ensure this directory is also on the path (for agentcore_proxy import).
GATEWAY_DIR = os.path.dirname(os.path.abspath(__file__))
if GATEWAY_DIR not in sys.path:
    sys.path.insert(0, GATEWAY_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ecs-gateway")


def main():
    logger.info("ECS Gateway starting (Phase 4)")

    # Step 0: Start health-check HTTP server for ECS.
    from healthcheck import start_health_server
    start_health_server(port=int(os.environ.get("HEALTH_PORT", "8080")))

    # Step 1: Monkey-patch AIAgent before any gateway import.
    from agentcore_proxy import patch_aiagent
    patch_aiagent()

    # Step 2: Patch WeixinAdapter.send() for auto-file delivery.
    from weixin_file_patch import patch_weixin_send
    patch_weixin_send()

    # Step 3: Import and run the hermes-agent gateway.
    from gateway.run import start_gateway
    logger.info("Starting hermes-agent gateway (protocol adapters only)")
    success = asyncio.run(start_gateway(replace=True, verbosity=1))
    if not success:
        logger.error("Gateway failed to start")
        sys.exit(1)


if __name__ == "__main__":
    main()
