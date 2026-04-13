"""Token Metrics Lambda — Bedrock usage analytics.

Periodically invoked by EventBridge to aggregate Bedrock token usage from
CloudWatch Logs, publish custom metrics, and trigger alarms when budgets
are exceeded.

Environment variables:
    DAILY_TOKEN_BUDGET     — Maximum tokens per day (default: 1_000_000)
    DAILY_COST_BUDGET_USD  — Maximum cost per day in USD (default: 10)
    ALARM_SNS_TOPIC_ARN    — SNS topic for budget alarms
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

cloudwatch = boto3.client("cloudwatch")
sns = boto3.client("sns")

DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "1000000"))
DAILY_COST_BUDGET_USD = float(os.environ.get("DAILY_COST_BUDGET_USD", "10"))
ALARM_TOPIC = os.environ.get("ALARM_SNS_TOPIC_ARN", "")
NAMESPACE = "Hermes/AgentCore"

# Approximate pricing per 1K tokens (input/output blended).
MODEL_COST_PER_1K: dict[str, float] = {
    "claude-opus-4-6":   0.030,
    "claude-sonnet-4-6": 0.006,
    "claude-haiku-4-5":  0.001,
    "default":           0.006,
}


def handler(event: dict, context: Any) -> dict:
    """EventBridge scheduled handler — runs every 15 minutes."""
    logger.info("Token metrics collection started")
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # Read current accumulated metrics from CloudWatch.
    total_tokens = _get_metric_sum("TotalTokens", today)
    total_cost = _get_metric_sum("EstimatedCostUSD", today)

    logger.info(
        "Today's usage: tokens=%d, cost=$%.4f (budget: %d / $%.2f)",
        total_tokens, total_cost, DAILY_TOKEN_BUDGET, DAILY_COST_BUDGET_USD,
    )

    # Publish utilisation percentage.
    token_pct = (total_tokens / DAILY_TOKEN_BUDGET * 100) if DAILY_TOKEN_BUDGET else 0
    cost_pct = (total_cost / DAILY_COST_BUDGET_USD * 100) if DAILY_COST_BUDGET_USD else 0

    _put_metric("TokenBudgetUtilization", token_pct, "Percent")
    _put_metric("CostBudgetUtilization", cost_pct, "Percent")

    # Fire alarm if budget exceeded.
    alerts: list[str] = []
    if total_tokens > DAILY_TOKEN_BUDGET:
        alerts.append(
            f"Token budget exceeded: {total_tokens:,} / {DAILY_TOKEN_BUDGET:,}"
        )
    if total_cost > DAILY_COST_BUDGET_USD:
        alerts.append(
            f"Cost budget exceeded: ${total_cost:.4f} / ${DAILY_COST_BUDGET_USD:.2f}"
        )

    if alerts and ALARM_TOPIC:
        _send_alarm(today, alerts)

    return {
        "date": today,
        "totalTokens": int(total_tokens),
        "estimatedCostUSD": round(total_cost, 4),
        "tokenBudgetPct": round(token_pct, 1),
        "costBudgetPct": round(cost_pct, 1),
        "alerts": alerts,
    }


# --------------------------------------------------------------------------
# Token reporting (called by contract server via CloudWatch PutMetricData)
# --------------------------------------------------------------------------

def report_usage(
    input_tokens: int,
    output_tokens: int,
    model: str = "default",
    user_id: str = "",
) -> None:
    """Publish token usage metrics to CloudWatch.

    This is meant to be called from the contract server (not this Lambda),
    but is placed here as a shared utility.
    """
    total = input_tokens + output_tokens
    cost_rate = MODEL_COST_PER_1K.get(model, MODEL_COST_PER_1K["default"])
    cost = total / 1000 * cost_rate

    dimensions = [{"Name": "Environment", "Value": "production"}]
    if user_id:
        dimensions.append({"Name": "UserId", "Value": user_id})

    cloudwatch.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[
            {
                "MetricName": "TotalTokens",
                "Value": total,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "InputTokens",
                "Value": input_tokens,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "OutputTokens",
                "Value": output_tokens,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "EstimatedCostUSD",
                "Value": cost,
                "Unit": "None",
                "Dimensions": dimensions,
            },
        ],
    )


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _get_metric_sum(metric_name: str, date_str: str) -> float:
    """Sum a metric for the given UTC day."""
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)

    try:
        resp = cloudwatch.get_metric_statistics(
            Namespace=NAMESPACE,
            MetricName=metric_name,
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
            Dimensions=[{"Name": "Environment", "Value": "production"}],
        )
        datapoints = resp.get("Datapoints", [])
        return sum(dp.get("Sum", 0) for dp in datapoints)
    except Exception as exc:
        logger.warning("Failed to get metric %s: %s", metric_name, exc)
        return 0


def _put_metric(name: str, value: float, unit: str) -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[{
                "MetricName": name,
                "Value": value,
                "Unit": unit,
                "Dimensions": [{"Name": "Environment", "Value": "production"}],
            }],
        )
    except Exception as exc:
        logger.warning("Failed to put metric %s: %s", name, exc)


def _send_alarm(date_str: str, alerts: list[str]) -> None:
    message = f"Hermes AgentCore Budget Alert ({date_str})\n\n" + "\n".join(f"- {a}" for a in alerts)
    try:
        sns.publish(
            TopicArn=ALARM_TOPIC,
            Subject=f"Hermes Budget Alert: {date_str}",
            Message=message,
        )
        logger.info("Budget alarm sent to %s", ALARM_TOPIC)
    except Exception as exc:
        logger.error("Failed to send alarm: %s", exc)
