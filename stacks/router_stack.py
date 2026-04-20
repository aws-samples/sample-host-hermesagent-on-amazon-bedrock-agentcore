"""Router stack — Lambda + API Gateway + DynamoDB.

Provides the external-facing HTTP API that receives channel webhooks
(Telegram, Slack, Discord) and routes them to AgentCore.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    CfnOutput,
)
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct


class HermesRouterStack(Stack):
    """Router Lambda, HTTP API Gateway, DynamoDB identity table."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        execution_role_arn: str,
        bucket_name: str,
        agentcore_runtime_arn: str = "",
        agentcore_qualifier: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        region = Stack.of(self).region
        account = Stack.of(self).account

        # ---- DynamoDB identity table -------------------------------------

        self.identity_table = dynamodb.Table(
            self,
            "IdentityTable",
            table_name=f"{project}-identity",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
            time_to_live_attribute="ttl",
        )

        # GSI for looking up users by userId.
        self.identity_table.add_global_secondary_index(
            index_name="UserIdIndex",
            partition_key=dynamodb.Attribute(
                name="userId", type=dynamodb.AttributeType.STRING,
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ---- Lambda function ---------------------------------------------

        self.router_fn = lambda_.Function(
            self,
            "RouterFn",
            function_name=f"{project}-router",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda/router"),
            timeout=Duration.seconds(120),
            memory_size=256,
            environment={
                "AGENTCORE_RUNTIME_ARN": agentcore_runtime_arn,
                "AGENTCORE_QUALIFIER": agentcore_qualifier,
                "IDENTITY_TABLE": self.identity_table.table_name,
                "S3_BUCKET": bucket_name,
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # Permissions.
        self.identity_table.grant_read_write_data(self.router_fn)

        # Allow Lambda to invoke AgentCore runtime.
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=["*"],  # Scoped at the API call level via ARN param.
            )
        )

        # Allow Lambda to invoke itself asynchronously (Discord deferred response).
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{region}:{account}:function:{project}-router",
                ],
            )
        )

        # Allow Lambda to read secrets.
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/*",
                ],
            )
        )

        # Allow Lambda to write to S3 (photo uploads).
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"arn:aws:s3:::{bucket_name}/*"],
            )
        )

        # ---- HTTP API Gateway --------------------------------------------

        integration = HttpLambdaIntegration("RouterIntegration", self.router_fn)

        self.api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"{project}-api",
            description=f"Hermes AgentCore webhook API ({project})",
        )

        routes = [
            ("/webhook/telegram", [apigwv2.HttpMethod.POST]),
            ("/webhook/slack", [apigwv2.HttpMethod.POST]),
            ("/webhook/discord", [apigwv2.HttpMethod.POST]),
            ("/webhook/feishu", [apigwv2.HttpMethod.POST]),
            ("/health", [apigwv2.HttpMethod.GET]),
        ]
        for path, methods in routes:
            self.api.add_routes(
                path=path,
                methods=methods,
                integration=integration,
            )

        # ---- Outputs -----------------------------------------------------

        CfnOutput(self, "ApiUrl", value=self.api.url or "")
        CfnOutput(
            self, "IdentityTableName", value=self.identity_table.table_name,
        )
