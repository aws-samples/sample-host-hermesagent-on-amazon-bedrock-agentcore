"""Gateway stack — ECS Fargate service for WeChat + Feishu (Phase 4).

Runs hermes-agent's native gateway on ECS Fargate.  Only platform protocol
adapters (WeChat long-poll, Feishu WebSocket) execute here; all AI inference
runs in AgentCore microVMs via ``invoke_agent_runtime()``.

Dependencies:
    - Phase 1: VPC (private subnets + NAT), Security (Secrets Manager)
    - Phase 2: AgentCore Runtime ARN
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
    CfnOutput,
)
from constructs import Construct


class HermesGatewayStack(Stack):
    """ECS Fargate service running hermes-agent gateway (protocol only)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        agentcore_runtime_arn: str = "",
        agentcore_qualifier: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        project = self.node.try_get_context("project_name") or "hermes-agentcore"
        region = Stack.of(self).region
        account = Stack.of(self).account

        # ---- ECR Repository --------------------------------------------------

        # ECR repo is created by deploy.sh before CDK runs (image must exist
        # before the ECS Service starts).  Import the existing repo by name.
        self.ecr_repo = ecr.Repository.from_repository_name(
            self, "GatewayRepo", f"{project}-gateway",
        )

        # ---- CloudWatch Log Group -------------------------------------------

        self.log_group = logs.LogGroup(
            self,
            "GatewayLogs",
            log_group_name=f"/ecs/{project}-gateway",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- ECS Cluster ----------------------------------------------------

        self.cluster = ecs.Cluster(
            self,
            "GatewayCluster",
            cluster_name=f"{project}-gateway",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # ---- IAM Task Role (least-privilege) --------------------------------

        self.task_role = iam.Role(
            self,
            "TaskRole",
            role_name=f"{project}-gateway-task-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        # AgentCore invocation — scoped to specific Runtime ARN.
        if agentcore_runtime_arn:
            self.task_role.add_to_policy(
                iam.PolicyStatement(
                    sid="AgentCoreInvoke",
                    actions=[
                        "bedrock-agentcore:InvokeAgentRuntime",
                        "bedrock-agentcore:InvokeAgentRuntimeForUser",
                    ],
                    resources=[
                        agentcore_runtime_arn,
                        f"{agentcore_runtime_arn}/*",
                    ],
                )
            )

        # Secrets Manager — read WeChat and Feishu credentials.
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsRead",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/weixin/*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:hermes/feishu/*",
                ],
            )
        )

        # CloudWatch logs.
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[self.log_group.log_group_arn + ":*"],
            )
        )

        # ---- IAM Execution Role (for pulling images) -----------------------

        execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"{project}-gateway-exec-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )

        # Allow pulling from the gateway ECR repo.
        self.ecr_repo.grant_pull(execution_role)

        # Note: secrets are read at runtime by the Task Role, not injected
        # via the ECS task definition, so the Execution Role does not need
        # secretsmanager:GetSecretValue.

        # ---- Security Group -------------------------------------------------

        self.sg = ec2.SecurityGroup(
            self,
            "GatewaySG",
            vpc=vpc,
            description="ECS Gateway - outbound only (WeChat, Feishu, AgentCore)",
            allow_all_outbound=True,
        )
        # No inbound rules — WeChat long-poll and Feishu WebSocket are
        # outbound-initiated connections.

        # ---- Task Definition ------------------------------------------------

        task_def = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            family=f"{project}-gateway",
            cpu=512,
            memory_limit_mib=1024,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            task_role=self.task_role,
            execution_role=execution_role,
        )

        # Container environment variables.
        environment = {
            "AGENTCORE_RUNTIME_ARN": agentcore_runtime_arn,
            "AGENTCORE_QUALIFIER": agentcore_qualifier,
            "AWS_REGION": region,
            "HERMES_HOME": "/opt/data",
            "HERMES_HEADLESS": "1",
            "HERMES_QUIET": "1",
            "HEALTH_PORT": "8080",
        }

        # Secrets (WEIXIN_TOKEN, FEISHU_APP_ID, FEISHU_APP_SECRET) are NOT
        # injected via ECS task definition — they may not exist yet.
        # Instead, the gateway reads them at runtime via environment variables
        # or Secrets Manager SDK calls.  The Task Role already has
        # secretsmanager:GetSecretValue permission for hermes/weixin/* and
        # hermes/feishu/* paths.  Operators set credentials via:
        #   aws secretsmanager create-secret --name hermes/weixin/token ...
        # and pass them as environment variables when updating the service,
        # or configure them in the hermes-agent config.yaml.

        container = task_def.add_container(
            "gateway",
            container_name="hermes-gateway",
            image=ecs.ContainerImage.from_ecr_repository(self.ecr_repo, tag="latest"),
            environment=environment,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="gateway",
                log_group=self.log_group,
            ),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(120),
            ),
        )

        container.add_port_mappings(
            ecs.PortMapping(container_port=8080, protocol=ecs.Protocol.TCP),
        )

        # ---- Fargate Service ------------------------------------------------

        self.service = ecs.FargateService(
            self,
            "GatewayService",
            service_name=f"{project}-gateway",
            cluster=self.cluster,
            task_definition=task_def,
            desired_count=1,
            security_groups=[self.sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
            assign_public_ip=False,
            enable_execute_command=True,
            min_healthy_percent=0,
            max_healthy_percent=200,
        )

        # ---- Outputs --------------------------------------------------------

        CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        CfnOutput(self, "ServiceName", value=self.service.service_name)
        CfnOutput(self, "EcrRepoUri", value=self.ecr_repo.repository_uri)
        CfnOutput(self, "LogGroupName", value=self.log_group.log_group_name)
        CfnOutput(self, "TaskRoleArn", value=self.task_role.role_arn)
