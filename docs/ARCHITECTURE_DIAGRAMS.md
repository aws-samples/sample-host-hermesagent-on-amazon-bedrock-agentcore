# Architecture Diagrams: Hermes vs OpenClaw on Bedrock AgentCore

Two Mermaid architecture diagrams comparing the Hermes-Agent and OpenClaw deployments on Amazon Bedrock AgentCore.

---

## 1. Hermes-Agent on Bedrock AgentCore

```mermaid
graph TB
    subgraph Users["External Channels"]
        TG["Telegram"]
        SL["Slack"]
        DC["Discord"]
    end

    subgraph AWS["AWS Cloud"]
        subgraph Phase1["Phase 1: Foundation Stacks (CDK)"]
            VPC["VPC<br/>2-AZ, NAT GW<br/>VPC Endpoints"]
            SEC["Security<br/>KMS + Secrets Manager<br/>(bot tokens, API keys)"]
            GR["Bedrock Guardrails<br/>Content Filter + PII Redaction"]
            OBS["Observability<br/>CloudWatch Dashboard<br/>SNS Alarms"]
            S3["S3 Bucket<br/>User Workspace<br/>(state.db, memories, skills)"]
            IAM["IAM Execution Role<br/>Bedrock invoke, S3 rw,<br/>Secrets read, KMS decrypt"]
        end

        subgraph Phase3["Phase 3: Dependent Stacks (CDK)"]
            APIGW["API Gateway v2<br/>(HTTP API)"]
            ROUTER["Router Lambda<br/>Python 3.13<br/>Signature verify<br/>Identity resolve<br/>AgentCore dispatch"]
            DDB["DynamoDB<br/>Identity Table<br/>channel:userId → hermes_user_id"]
            CRON["EventBridge Scheduler"]
            CRONLAM["Cron Lambda"]
            TOKMON["Token Monitoring<br/>Lambda"]
        end

        subgraph Phase2["Phase 2: AgentCore Runtime"]
            AC["Bedrock AgentCore<br/>Per-User Firecracker microVM"]

            subgraph Container["Agent Container (Docker)"]
                MAIN["main.py<br/>BedrockAgentCoreApp<br/>Monkey-patch:<br/>Anthropic → AnthropicBedrock"]
                CONTRACT["contract.py<br/>HTTP Contract Server<br/>GET /ping<br/>POST /invocations"]
                WARMUP["warmup_agent.py<br/>Lightweight Agent<br/>(0-2s ready)"]
                HERMES["hermes-agent<br/>AIAgent (full)<br/>40+ tools, memory, skills<br/>(10-30s ready)"]
                WSYNC["workspace_sync.py<br/>S3 ↔ /mnt/workspace<br/>Every 300s"]
                BPROV["bedrock_provider.py<br/>Model mapping + litellm"]
            end
        end

        BEDROCK["Amazon Bedrock<br/>Claude Sonnet/Opus<br/>SigV4 Auth (no API key)"]
    end

    TG -->|"POST /webhook/telegram"| APIGW
    SL -->|"POST /webhook/slack"| APIGW
    DC -->|"POST /webhook/discord"| APIGW
    APIGW --> ROUTER
    ROUTER -->|"read/write identity"| DDB
    ROUTER -->|"read bot tokens"| SEC
    ROUTER -->|"invoke_agent_runtime<br/>(ARN, sessionId, userId, payload)"| AC
    CRON -->|"schedule"| CRONLAM
    CRONLAM -->|"invoke (action: cron)"| AC

    AC -->|"spawn/reuse microVM"| Container
    CONTRACT --> WARMUP
    CONTRACT --> HERMES
    MAIN --> CONTRACT
    HERMES -->|"monkey-patched SDK"| BEDROCK
    WARMUP -->|"monkey-patched SDK"| BEDROCK
    BEDROCK -.->|"optional"| GR
    WSYNC -->|"sync state"| S3
    Container -.-> IAM
    Container -.-> VPC

    TOKMON -->|"metrics"| OBS

    classDef phase1 fill:#e8f5e9,stroke:#2e7d32
    classDef phase2 fill:#e3f2fd,stroke:#1565c0
    classDef phase3 fill:#fff3e0,stroke:#e65100
    classDef external fill:#fce4ec,stroke:#c62828
    classDef bedrock fill:#f3e5f5,stroke:#6a1b9a

    class VPC,SEC,GR,OBS,S3,IAM phase1
    class AC,MAIN,CONTRACT,WARMUP,HERMES,WSYNC,BPROV phase2
    class APIGW,ROUTER,DDB,CRON,CRONLAM,TOKMON phase3
    class TG,SL,DC external
    class BEDROCK bedrock
```

### Hermes Request Flow

```mermaid
sequenceDiagram
    participant U as User (Telegram/Slack/Discord)
    participant GW as API Gateway v2
    participant R as Router Lambda
    participant DB as DynamoDB
    participant AC as AgentCore
    participant VM as Firecracker microVM
    participant C as contract.py
    participant H as hermes-agent
    participant B as Amazon Bedrock

    U->>GW: POST /webhook/{channel}
    GW->>R: Invoke Lambda
    R->>R: Verify signature
    R->>DB: Resolve identity (actor_id → user_id)
    R->>AC: invoke_agent_runtime(ARN, sessionId, userId, payload)
    AC->>VM: Route to microVM (create or reuse)
    VM->>C: POST /invocations {action: "chat", message: "..."}

    alt First request (cold start)
        C->>C: warmup_agent handles (~2s)
        C-->>H: Load full agent in background
    else Warm
        C->>H: run_conversation(message)
    end

    H->>B: Claude API (monkey-patched → AnthropicBedrock, SigV4)
    B-->>H: Streaming response
    H-->>C: SSE: data: "response text"
    C-->>VM: HTTP response
    VM-->>AC: Stream back
    AC-->>R: SSE response
    R->>R: Parse SSE, extract text
    R->>U: Send to channel (sendMessage / postMessage)
```

### Hermes Deployment Flow

```mermaid
graph LR
    subgraph P1["Phase 1: CDK Foundation"]
        P1A["cdk deploy"] --> VPC["vpc"]
        P1A --> SEC["security"]
        P1A --> GR["guardrails"]
        P1A --> AGC["agentcore"]
        P1A --> OBS["observability"]
    end

    subgraph P2["Phase 2: AgentCore Runtime"]
        P2A["rsync hermes-agent<br/>+ bridge/ into app/"] --> P2B["agentcore deploy<br/>(Docker build + push ECR)"]
        P2B --> P2C["agentcore status --json<br/>Extract ARN + qualifier"]
        P2C --> P2D["Update cdk.json<br/>with runtime IDs"]
    end

    subgraph P3["Phase 3: CDK Dependent"]
        P3A["cdk deploy"] --> RTR["router"]
        P3A --> CRN["cron"]
        P3A --> TKM["token-monitoring"]
    end

    P1 --> P2
    P2 --> P3
    P3 --> OUT["API Gateway URL<br/>+ Webhook Endpoints"]

    classDef p1 fill:#e8f5e9,stroke:#2e7d32
    classDef p2 fill:#e3f2fd,stroke:#1565c0
    classDef p3 fill:#fff3e0,stroke:#e65100

    class P1A,VPC,SEC,GR,AGC,OBS p1
    class P2A,P2B,P2C,P2D p2
    class P3A,RTR,CRN,TKM p3
```

---

## 2. OpenClaw on Bedrock AgentCore

```mermaid
graph TB
    subgraph Users["External Channels"]
        TG["Telegram"]
        WA["WhatsApp"]
        DC["Discord"]
    end

    subgraph EC2["EC2 Gateway Instance"]
        OCGW["OpenClaw Gateway<br/>Node.js, port 18789<br/>IM Channel Handler"]
        H2P["Bedrock H2 Proxy<br/>Node.js, port 8091<br/>Intercepts Bedrock API<br/>Cold-start fast-path"]
        TR["Tenant Router<br/>Python, port 8090<br/>derive_tenant_id()<br/>AgentCore API dispatch"]
    end

    subgraph AWS["AWS Cloud"]
        subgraph Infra["Infrastructure"]
            VPC["VPC"]
            SSM["SSM Parameter Store<br/>Tenant permissions<br/>Position mapping<br/>User mapping"]
            S3["S3 Bucket<br/>Workspace persistence<br/>SOUL templates<br/>(global/position/personal)"]
            CW["CloudWatch<br/>Audit logs"]
        end

        AC["Bedrock AgentCore<br/>Per-Tenant Firecracker microVM"]

        subgraph Container["Agent Container (Docker)"]
            ENTRY["entrypoint.sh<br/>Write openclaw.json<br/>Start server.py<br/>Pull workspace from S3"]
            SRV["server.py<br/>HTTP Contract Server<br/>GET /ping<br/>POST /invocations"]
            WA2["workspace_assembler.py<br/>3-Layer SOUL merge:<br/>Global > Position > Personal"]
            PERM["permissions.py<br/>Plan A: Inject tool constraints<br/>Plan E: Audit responses"]
            OC["openclaw CLI<br/>Node.js subprocess<br/>subprocess.run()<br/>(zero-invasion)"]
        end

        BEDROCK["Amazon Bedrock<br/>ConverseStream API<br/>SigV4 Auth"]
    end

    TG -->|"webhook"| OCGW
    WA -->|"webhook"| OCGW
    DC -->|"webhook"| OCGW
    OCGW -->|"Bedrock API call<br/>(intercepted via<br/>AWS_ENDPOINT_URL)"| H2P
    H2P -->|"cold start: fast-path"| BEDROCK
    H2P -->|"extract channel+user_id"| TR
    TR -->|"derive tenant_id<br/>(≥33 chars)"| AC
    TR -->|"read permissions"| SSM

    AC -->|"spawn/reuse microVM"| Container
    ENTRY --> SRV
    SRV -->|"subprocess.run()<br/>openclaw agent --message"| OC
    ENTRY -->|"S3 sync workspace"| S3
    WA2 -->|"merge 3-layer SOUL"| S3
    PERM -->|"read permissions"| SSM
    PERM -->|"audit log"| CW
    OC -->|"Bedrock ConverseStream<br/>(real API, SigV4)"| BEDROCK

    classDef ec2 fill:#fff9c4,stroke:#f57f17
    classDef infra fill:#e8f5e9,stroke:#2e7d32
    classDef container fill:#e3f2fd,stroke:#1565c0
    classDef external fill:#fce4ec,stroke:#c62828
    classDef bedrock fill:#f3e5f5,stroke:#6a1b9a

    class OCGW,H2P,TR ec2
    class VPC,SSM,S3,CW infra
    class AC,ENTRY,SRV,WA2,PERM,OC container
    class TG,WA,DC external
    class BEDROCK bedrock
```

### OpenClaw Request Flow (6 Hops)

```mermaid
sequenceDiagram
    participant U as User (Telegram/WhatsApp/Discord)
    participant GW as OpenClaw Gateway<br/>(EC2, Node.js)
    participant H2 as H2 Proxy<br/>(EC2, port 8091)
    participant TR as Tenant Router<br/>(EC2, Python)
    participant AC as AgentCore
    participant VM as Firecracker microVM
    participant SRV as server.py
    participant OC as openclaw CLI<br/>(Node.js subprocess)
    participant B as Amazon Bedrock

    U->>GW: Webhook message
    GW->>H2: Bedrock API call (intercepted via AWS_ENDPOINT_URL)
    H2->>H2: Extract channel + user_id from request body

    alt Cold start (tenant not warm)
        H2->>B: Fast-path direct Bedrock call (~3s)
        B-->>H2: Quick response
        H2-->>GW: Return fast response
        H2->>TR: Async: pre-warm microVM
    else Warm tenant
        H2->>TR: Forward to tenant router
    end

    TR->>TR: derive_tenant_id(channel, user_id)
    TR->>AC: invoke_agent_runtime(ARN, sessionId=tenant_id)
    AC->>VM: Route to microVM

    Note over VM: entrypoint.sh:<br/>1. Write openclaw.json<br/>2. S3 sync workspace<br/>3. 3-layer SOUL merge<br/>4. Start server.py

    VM->>SRV: POST /invocations
    SRV->>OC: subprocess.run("openclaw agent --message ...")
    OC->>B: Bedrock ConverseStream (real API, SigV4)
    B-->>OC: Streaming response
    OC-->>SRV: JSON stdout
    SRV->>SRV: Plan E: audit response for forbidden tools
    SRV-->>VM: HTTP response
    VM-->>AC: Return
    AC-->>TR: Response
    TR-->>H2: Response
    H2-->>GW: Response
    GW-->>U: Channel message
```

---

## 3. Side-by-Side Comparison

```mermaid
graph LR
    subgraph Hermes["Hermes-Agent Architecture"]
        direction TB
        H_CH["Channels:<br/>Telegram, Slack, Discord"]
        H_GW["API Gateway v2 (Serverless)"]
        H_RT["Router Lambda (Python)"]
        H_ID["DynamoDB Identity Table"]
        H_AC["AgentCore microVM"]
        H_BR["Bridge Layer:<br/>contract.py + monkey-patch"]
        H_AG["hermes-agent (Python)<br/>In-process SDK call"]
        H_LLM["Bedrock via AnthropicBedrock<br/>(monkey-patched)"]

        H_CH --> H_GW --> H_RT
        H_RT --> H_ID
        H_RT --> H_AC --> H_BR --> H_AG --> H_LLM
    end

    subgraph OpenClaw["OpenClaw Architecture"]
        direction TB
        O_CH["Channels:<br/>Telegram, WhatsApp, Discord"]
        O_GW["EC2 Gateway (Node.js)"]
        O_H2["H2 Proxy (intercept Bedrock)"]
        O_TR["Tenant Router (Python)"]
        O_AC["AgentCore microVM"]
        O_SRV["server.py (HTTP wrapper)"]
        O_AG["openclaw CLI (Node.js)<br/>subprocess.run()"]
        O_LLM["Bedrock ConverseStream<br/>(native SDK)"]

        O_CH --> O_GW --> O_H2
        O_H2 --> O_TR --> O_AC --> O_SRV --> O_AG --> O_LLM
    end

    classDef hermes fill:#e3f2fd,stroke:#1565c0
    classDef openclaw fill:#fff3e0,stroke:#e65100

    class H_CH,H_GW,H_RT,H_ID,H_AC,H_BR,H_AG,H_LLM hermes
    class O_CH,O_GW,O_H2,O_TR,O_AC,O_SRV,O_AG,O_LLM openclaw
```

### Feature Comparison Table

| Dimension | Hermes-Agent | OpenClaw |
|-----------|-------------|----------|
| **Agent Language** | Python | Node.js |
| **Gateway** | Serverless (API GW + Lambda) | EC2 (Node.js + H2 Proxy) |
| **Agent Integration** | In-process SDK call (monkey-patch) | CLI subprocess (`openclaw agent --message`) |
| **Bedrock Routing** | Monkey-patch `Anthropic` → `AnthropicBedrock` | Environment variable hijack (`AWS_ENDPOINT_URL`) |
| **Tenant Isolation** | session_id per user | tenant_id derived from channel+user (≥33 chars) |
| **Identity Store** | DynamoDB | SSM Parameter Store |
| **State Persistence** | S3 workspace sync (every 300s) | S3 workspace sync (every 60s) |
| **Cold Start** | Dual-agent: warmup (2s) + full (10-30s) | H2 Proxy fast-path direct Bedrock (~3s) |
| **Permission Model** | Bedrock Guardrails (content filter + PII) | Plan A (SOUL injection) + Plan E (audit regex) |
| **SOUL/Persona** | Single system prompt | 3-layer merge (global > position > personal) |
| **Deployment** | 3-phase: CDK → AgentCore CLI → CDK | EC2 + AgentCore CLI |
| **CDK Stacks** | 8 stacks (vpc, security, guardrails, agentcore, observability, router, cron, token-monitoring) | CloudFormation (VPC, EC2, IAM) |
| **Channels** | Telegram, Slack, Discord (+ ECS gateway planned for WeChat, Feishu) | Telegram, WhatsApp, Discord (via OpenClaw native) |
| **Scheduling** | EventBridge Scheduler → Lambda → AgentCore | Not documented |
| **Monitoring** | CloudWatch dashboard + SNS alarms + token budget | CloudWatch audit logs |
| **Container Base** | Python 3.11-slim (~65MB) | Python 3.12-slim + Node.js (~800MB+) |
| **Hops (user→LLM)** | 4 (APIGW → Lambda → AgentCore → Bedrock) | 6 (GW → H2 → Router → AgentCore → CLI → Bedrock) |
