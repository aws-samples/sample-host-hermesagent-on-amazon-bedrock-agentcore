"""Observability stack — CloudWatch dashboards, alarms, SNS.

Provides a single-pane dashboard for monitoring the Hermes AgentCore
deployment and alerts for critical thresholds.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    CfnOutput,
)
from constructs import Construct


class HermesObservabilityStack(Stack):
    """CloudWatch dashboard, alarms, and SNS topic."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alarm_email: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        namespace = "Hermes/AgentCore"

        # ---- SNS topic for alarms ----------------------------------------

        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=f"{project}-alarms",
            display_name=f"Hermes AgentCore Alarms",
        )

        if alarm_email:
            self.alarm_topic.add_subscription(
                subs.EmailSubscription(alarm_email),
            )

        # ---- Metrics (custom namespace) ----------------------------------

        total_tokens = cw.Metric(
            namespace=namespace,
            metric_name="TotalTokens",
            statistic="Sum",
            period=Duration.minutes(5),
            dimensions_map={"Environment": "production"},
        )

        estimated_cost = cw.Metric(
            namespace=namespace,
            metric_name="EstimatedCostUSD",
            statistic="Sum",
            period=Duration.minutes(5),
            dimensions_map={"Environment": "production"},
        )

        token_budget_pct = cw.Metric(
            namespace=namespace,
            metric_name="TokenBudgetUtilization",
            statistic="Maximum",
            period=Duration.minutes(15),
            dimensions_map={"Environment": "production"},
        )

        # Lambda metrics.
        router_errors = cw.Metric(
            namespace="AWS/Lambda",
            metric_name="Errors",
            statistic="Sum",
            period=Duration.minutes(5),
            dimensions_map={"FunctionName": f"{project}-router"},
        )

        router_duration = cw.Metric(
            namespace="AWS/Lambda",
            metric_name="Duration",
            statistic="p99",
            period=Duration.minutes(5),
            dimensions_map={"FunctionName": f"{project}-router"},
        )

        # ---- Alarms ------------------------------------------------------

        token_alarm = cw.Alarm(
            self,
            "TokenBudgetAlarm",
            alarm_name=f"{project}-token-budget-exceeded",
            metric=token_budget_pct,
            threshold=100,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="Daily token budget exceeded",
        )
        token_alarm.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        error_alarm = cw.Alarm(
            self,
            "RouterErrorAlarm",
            alarm_name=f"{project}-router-errors",
            metric=router_errors,
            threshold=5,
            evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="Router Lambda error rate is elevated",
        )
        error_alarm.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        latency_alarm = cw.Alarm(
            self,
            "RouterLatencyAlarm",
            alarm_name=f"{project}-router-latency",
            metric=router_duration,
            threshold=30000,  # 30 s
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="Router Lambda P99 latency exceeds 30 s",
        )
        latency_alarm.add_alarm_action(cw_actions.SnsAction(self.alarm_topic))

        # ---- Dashboard ---------------------------------------------------

        self.dashboard = cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"{project}-dashboard",
            widgets=[
                # Row 1: Token usage.
                [
                    cw.GraphWidget(
                        title="Token Usage (5 min)",
                        left=[total_tokens],
                        width=12,
                    ),
                    cw.GraphWidget(
                        title="Estimated Cost USD (5 min)",
                        left=[estimated_cost],
                        width=12,
                    ),
                ],
                # Row 2: Budget utilisation.
                [
                    cw.GaugeWidget(
                        title="Token Budget Utilization %",
                        metrics=[token_budget_pct],
                        left_y_axis=cw.YAxisProps(min=0, max=150),
                        width=12,
                    ),
                    cw.SingleValueWidget(
                        title="Router Errors (5 min)",
                        metrics=[router_errors],
                        width=6,
                    ),
                    cw.SingleValueWidget(
                        title="Router P99 Latency (ms)",
                        metrics=[router_duration],
                        width=6,
                    ),
                ],
            ],
        )

        # ---- Outputs -----------------------------------------------------

        CfnOutput(self, "AlarmTopicArn", value=self.alarm_topic.topic_arn)
        CfnOutput(
            self,
            "DashboardUrl",
            value=f"https://console.aws.amazon.com/cloudwatch/home#dashboards:name={project}-dashboard",
        )
