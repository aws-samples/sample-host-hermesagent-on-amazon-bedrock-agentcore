"""VPC stack — networking foundation for AgentCore.

Creates a 2-AZ VPC with public/private subnets, NAT Gateway, and VPC
endpoints for AWS services used by the AgentCore containers.
"""

from __future__ import annotations

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    CfnOutput,
)
from constructs import Construct


class HermesVpcStack(Stack):
    """Networking: VPC, subnets, NAT, VPC endpoints."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        az_count = int(self.node.try_get_context("az_count") or 2)
        cidr = self.node.try_get_context("vpc_cidr") or "10.0.0.0/16"

        # ---- VPC ----------------------------------------------------------

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{project}-vpc",
            ip_addresses=ec2.IpAddresses.cidr(cidr),
            max_azs=az_count,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=22,
                ),
            ],
        )

        # ---- VPC Endpoints -----------------------------------------------
        # Minimise NAT traffic and latency for AWS services.

        gateway_endpoints = {
            "S3": ec2.GatewayVpcEndpointAwsService.S3,
            "DynamoDB": ec2.GatewayVpcEndpointAwsService.DYNAMODB,
        }
        for name, service in gateway_endpoints.items():
            self.vpc.add_gateway_endpoint(f"{name}Endpoint", service=service)

        interface_services = [
            ("Bedrock", ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME),
            ("SecretsManager", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER),
            ("STS", ec2.InterfaceVpcEndpointAwsService.STS),
            ("ECR", ec2.InterfaceVpcEndpointAwsService.ECR),
            ("ECRDocker", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER),
            ("CloudWatchLogs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS),
        ]
        for name, service in interface_services:
            self.vpc.add_interface_endpoint(
                f"{name}Endpoint",
                service=service,
                private_dns_enabled=True,
            )

        # ---- Outputs -----------------------------------------------------

        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
