"""Guardrails stack — Bedrock Guardrails for content safety.

Configures content filtering and PII redaction applied to model responses
before they are delivered to users.
"""

from __future__ import annotations

from aws_cdk import (
    Stack,
    aws_bedrock as bedrock,
    CfnOutput,
)
from constructs import Construct


class HermesGuardrailsStack(Stack):
    """Bedrock Guardrails: content filter + PII anonymisation."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        enable = self.node.try_get_context("enable_guardrails")
        if enable is False:
            return

        # ---- Content filter categories -----------------------------------

        filters = [
            bedrock.CfnGuardrail.ContentFilterConfigProperty(
                type=cat,
                input_strength="MEDIUM",
                output_strength="MEDIUM",
            )
            for cat in [
                "SEXUAL",
                "VIOLENCE",
                "HATE",
                "INSULTS",
                "MISCONDUCT",
                "PROMPT_ATTACK",
            ]
        ]

        # ---- PII entities to anonymise -----------------------------------

        pii_entities = [
            bedrock.CfnGuardrail.PiiEntityConfigProperty(type=t, action="ANONYMIZE")
            for t in [
                "EMAIL",
                "PHONE",
                "NAME",
                "US_SOCIAL_SECURITY_NUMBER",
                "CREDIT_DEBIT_CARD_NUMBER",
            ]
        ]

        # ---- Guardrail resource ------------------------------------------

        self.guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{project}-guardrail",
            description="Content safety guardrail for Hermes AgentCore",
            blocked_input_messaging="I can't help with that request.",
            blocked_outputs_messaging="I can't provide that response.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=filters,
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=pii_entities,
            ),
        )

        self.guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "GuardrailVersion",
            guardrail_identifier=self.guardrail.attr_guardrail_id,
            description="Initial version",
        )

        # ---- Outputs -----------------------------------------------------

        CfnOutput(
            self,
            "GuardrailId",
            value=self.guardrail.attr_guardrail_id,
        )
        CfnOutput(
            self,
            "GuardrailVersionOutput",
            value=self.guardrail_version.attr_version,
            export_name=f"{project}-guardrail-version",
        )
