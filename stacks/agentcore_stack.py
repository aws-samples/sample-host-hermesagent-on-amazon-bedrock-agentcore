"""AgentCore stack — IAM execution role, S3 bucket, security group.

Defines the IAM role that AgentCore containers assume, the S3 bucket for
per-user workspace persistence, and the security group for VPC networking.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3 as s3,
    CfnOutput,
)
from constructs import Construct


class HermesAgentCoreStack(Stack):
    """IAM role, S3 user-files bucket, security group for AgentCore."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        kms_key_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        region = Stack.of(self).region
        account = Stack.of(self).account

        # ---- S3 bucket for user files ------------------------------------

        self.bucket = s3.Bucket(
            self,
            "UserFilesBucket",
            bucket_name=f"{project}-user-files-{account}-{region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=None,  # Uses the default S3 key; override with kms_key_arn if desired.
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="CleanupOldVersions",
                    noncurrent_version_expiration=Duration.days(90),
                ),
            ],
        )

        # ---- Security group ----------------------------------------------

        self.sg = ec2.SecurityGroup(
            self,
            "AgentCoreSG",
            vpc=vpc,
            description="AgentCore container security group",
            allow_all_outbound=True,
        )

        # ---- IAM execution role ------------------------------------------
        # This role is assumed by the AgentCore runtime containers.

        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"{project}-execution-role",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.AccountPrincipal(account),
            ),
        )

        # Bedrock model invocation.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvoke",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/*",
                    f"arn:aws:bedrock:*:{account}:inference-profile/*",
                ],
            )
        )

        # Bedrock Guardrails.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockGuardrails",
                actions=["bedrock:ApplyGuardrail"],
                resources=[f"arn:aws:bedrock:{region}:{account}:guardrail/*"],
            )
        )

        # S3 — user files bucket.
        self.bucket.grant_read_write(self.execution_role)

        # Secrets Manager — read bot tokens and API keys.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsRead",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/*",
                ],
            )
        )

        # STS — self-assume for scoped credentials.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="SelfAssume",
                actions=["sts:AssumeRole"],
                resources=[self.execution_role.role_arn],
            )
        )

        # KMS — decrypt secrets and S3 objects.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="KmsDecrypt",
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[kms_key_arn],
            )
        )

        # CloudWatch — logging and metrics.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatch",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )

        # ECR — pull container image.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                sid="ECRPull",
                actions=[
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=["*"],
            )
        )

        # ---- Outputs -----------------------------------------------------

        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        CfnOutput(self, "BucketName", value=self.bucket.bucket_name)
        CfnOutput(self, "SecurityGroupId", value=self.sg.security_group_id)
