"""Security stack — KMS, Secrets Manager, Cognito.

Provides the encryption key, secret storage, and (optional) user-pool
authentication for the Hermes AgentCore deployment.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_cognito as cognito,
    aws_kms as kms,
    aws_secretsmanager as sm,
    CfnOutput,
)
from constructs import Construct


class HermesSecurityStack(Stack):
    """KMS CMK, Secrets Manager secrets, Cognito user pool."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"

        # ---- KMS ----------------------------------------------------------

        self.kms_key = kms.Key(
            self,
            "Key",
            alias=f"alias/{project}",
            description=f"Encryption key for {project}",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---- Secrets Manager (placeholders — real values set by operator) -

        secret_names = [
            "telegram-bot-token",
            "slack-bot-token",
            "slack-signing-secret",
            "discord-bot-token",
            "discord-public-key",
            "feishu-app-id",
            "feishu-app-secret",
            "feishu-encrypt-key",
            "openai-api-key",
            "openrouter-api-key",
        ]
        self.secrets: dict[str, sm.Secret] = {}
        for name in secret_names:
            self.secrets[name] = sm.Secret(
                self,
                name.replace("-", "_").title().replace("_", ""),
                secret_name=f"hermes/{name}",
                description=f"Hermes AgentCore — {name}",
                encryption_key=self.kms_key,
            )

        # ---- Cognito (optional — for web UI auth) ------------------------

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{project}-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=False,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_srp=True),
            id_token_validity=Duration.hours(8),
        )

        # ---- Outputs -----------------------------------------------------

        CfnOutput(self, "KmsKeyArn", value=self.kms_key.key_arn)
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(
            self,
            "UserPoolClientId",
            value=self.user_pool_client.user_pool_client_id,
        )
