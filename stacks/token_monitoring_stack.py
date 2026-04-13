"""Token monitoring stack — scheduled Lambda for usage analytics.

Runs every 15 minutes to aggregate Bedrock token usage and publish
budget-utilisation metrics and alarms.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    CfnOutput,
)
from constructs import Construct


class HermesTokenMonitoringStack(Stack):
    """Token metrics Lambda + EventBridge schedule."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alarm_topic_arn: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        daily_token_budget = str(
            self.node.try_get_context("daily_token_budget") or "1000000",
        )
        daily_cost_budget = str(
            self.node.try_get_context("daily_cost_budget_usd") or "10",
        )

        # ---- Lambda function ---------------------------------------------

        self.metrics_fn = lambda_.Function(
            self,
            "TokenMetricsFn",
            function_name=f"{project}-token-metrics",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/token_metrics"),
            timeout=Duration.seconds(60),
            memory_size=128,
            environment={
                "DAILY_TOKEN_BUDGET": daily_token_budget,
                "DAILY_COST_BUDGET_USD": daily_cost_budget,
                "ALARM_SNS_TOPIC_ARN": alarm_topic_arn,
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # CloudWatch read + write.
        self.metrics_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )

        # SNS publish for alarms.
        if alarm_topic_arn:
            self.metrics_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["sns:Publish"],
                    resources=[alarm_topic_arn],
                )
            )

        # ---- EventBridge rule: every 15 minutes --------------------------

        events.Rule(
            self,
            "ScheduleRule",
            rule_name=f"{project}-token-metrics-schedule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            targets=[targets.LambdaFunction(self.metrics_fn)],
        )

        # ---- Outputs -----------------------------------------------------

        CfnOutput(
            self, "TokenMetricsFnArn", value=self.metrics_fn.function_arn,
        )
