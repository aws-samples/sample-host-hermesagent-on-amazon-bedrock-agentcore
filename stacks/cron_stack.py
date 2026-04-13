"""Cron stack — EventBridge Scheduler + Cron executor Lambda.

Replaces hermes-agent's in-process cron daemon with a serverless scheduler
that invokes the agent via AgentCore.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    Stack,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    CfnOutput,
)
from constructs import Construct


class HermesCronStack(Stack):
    """EventBridge Scheduler + Lambda executor for scheduled tasks."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        agentcore_runtime_arn: str = "",
        agentcore_qualifier: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        region = Stack.of(self).region
        account = Stack.of(self).account

        # ---- Cron executor Lambda ----------------------------------------

        self.cron_fn = lambda_.Function(
            self,
            "CronFn",
            function_name=f"{project}-cron",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/cron"),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "AGENTCORE_RUNTIME_ARN": agentcore_runtime_arn,
                "AGENTCORE_QUALIFIER": agentcore_qualifier,
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # Allow Lambda to invoke AgentCore.
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                resources=["*"],
            )
        )

        # Allow Lambda to read secrets (for delivery to channels).
        self.cron_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/*",
                ],
            )
        )

        # ---- EventBridge Scheduler role ----------------------------------
        # Schedules are created dynamically via the agent or console.
        # This role allows EventBridge to invoke the Lambda.

        self.scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{project}-scheduler-role",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.cron_fn.grant_invoke(self.scheduler_role)

        # ---- Outputs -----------------------------------------------------

        CfnOutput(self, "CronFunctionArn", value=self.cron_fn.function_arn)
        CfnOutput(self, "SchedulerRoleArn", value=self.scheduler_role.role_arn)
