"""Router Lambda — channel webhooks → AgentCore invocation.

Handles incoming messages from Telegram, Slack, and Discord, resolves user
identity via DynamoDB, and dispatches to the AgentCore runtime.

Environment variables (set by CDK):
    AGENTCORE_RUNTIME_ARN  — AgentCore runtime ARN
    AGENTCORE_QUALIFIER    — Runtime qualifier / endpoint
    IDENTITY_TABLE         — DynamoDB table name
    S3_BUCKET              — User files bucket (for image uploads)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---- AWS clients (reused across invocations) -----------------------------

dynamodb = boto3.resource("dynamodb")
identity_table = dynamodb.Table(os.environ.get("IDENTITY_TABLE", "hermes-identity"))
s3 = boto3.client("s3")

RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")

# Lazy-init the agentcore client (might not be available in test).
_agentcore_client: Any = None


def _agentcore() -> Any:
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client("bedrock-agentcore")
    return _agentcore_client


# --------------------------------------------------------------------------
# Handler entry point
# --------------------------------------------------------------------------

def handler(event: dict, context: Any) -> dict:
    """API Gateway HTTP API v2 handler."""
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    logger.info("Incoming request: %s %s", method, path)

    try:
        # Discord async followup (invoked by ourselves).
        if event.get("_discord_followup"):
            _discord_followup(event["_discord_followup"])
            return _ok({"status": "ok"})

        if path.startswith("/webhook/telegram"):
            return _handle_telegram(event)
        elif path.startswith("/webhook/slack"):
            return _handle_slack(event)
        elif path.startswith("/webhook/discord"):
            return _handle_discord(event)
        elif path.startswith("/webhook/feishu"):
            return _handle_feishu(event)
        elif path == "/health":
            return _ok({"status": "healthy", "timestamp": int(time.time())})
        else:
            return _ok({"error": "Not found"}, status=404)
    except Exception as exc:
        logger.exception("Unhandled error")
        return _ok({"error": str(exc)}, status=500)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def _handle_telegram(event: dict) -> dict:
    body = _parse_body(event)

    # Telegram sends different update types.
    message = body.get("message") or body.get("edited_message")
    if not message:
        return _ok({"status": "ignored"})

    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))
    user_id = str(message.get("from", {}).get("id", ""))
    username = message.get("from", {}).get("username", "")
    actor_id = f"telegram:{user_id}"

    if not text.strip():
        return _ok({"status": "empty"})

    # Check allowlist.
    if not _is_allowed(actor_id):
        logger.info("Blocked message from %s (not in allowlist)", actor_id)
        return _ok({"status": "blocked"})

    # Resolve hermes user.
    hermes_user_id = _resolve_user(actor_id, username=username)
    session_id = _build_session_id(hermes_user_id, "telegram")

    # Handle images (photo attachments).
    images = _download_telegram_photos(message)

    # Invoke AgentCore.
    payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "telegram",
        "chatId": chat_id,
        "message": text,
        "images": images,
    }

    agent_response = _invoke_agentcore(session_id, actor_id, payload)

    # Send response back to Telegram.
    _send_telegram_message(chat_id, agent_response)

    return _ok({"status": "ok"})


def _download_telegram_photos(message: dict) -> list[dict]:
    """Download photos from Telegram message and upload to S3."""
    photos = message.get("photo", [])
    if not photos:
        return []

    # Telegram sends multiple sizes — take the largest.
    photo = photos[-1]
    file_id = photo.get("file_id", "")
    if not file_id:
        return []

    try:
        token = _get_secret("telegram-bot-token")
        # Get file path from Telegram.
        url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
        resp = json.loads(urllib.request.urlopen(url, timeout=10).read())
        file_path = resp.get("result", {}).get("file_path", "")
        if not file_path:
            return []

        # Download file.
        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        file_data = urllib.request.urlopen(download_url, timeout=30).read()

        # Upload to S3.
        ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "jpg"
        s3_key = f"uploads/{int(time.time())}_{file_id}.{ext}"
        s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=file_data)

        content_type = f"image/{ext}" if ext in ("jpg", "jpeg", "png", "gif", "webp") else "application/octet-stream"
        return [{"s3Key": s3_key, "contentType": content_type}]
    except Exception as exc:
        logger.warning("Failed to download Telegram photo: %s", exc)
        return []


def _send_telegram_message(chat_id: str, text: str) -> None:
    """Send a message via the Telegram Bot API."""
    if not text:
        return
    token = _get_secret("telegram-bot-token")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram has a 4096-character limit — split if needed.
    chunks = _split_message(text, max_len=4096)
    for chunk in chunks:
        data = json.dumps({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as exc:
            logger.error("Telegram sendMessage failed: %s", exc)
            # Retry without Markdown parse_mode (in case of formatting errors).
            data = json.dumps({"chat_id": chat_id, "text": chunk}).encode()
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=15)
            except Exception:
                logger.error("Telegram sendMessage retry also failed")


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------

def _handle_slack(event: dict) -> dict:
    body = _parse_body(event)

    # Slack URL verification challenge.
    if body.get("type") == "url_verification":
        return _ok({"challenge": body.get("challenge", "")})

    # Verify Slack request signature.
    signing_secret = _get_secret("slack-signing-secret")
    if not _verify_slack_signature(event, signing_secret):
        return _ok({"error": "Invalid signature"}, status=401)

    # Parse Slack event.
    slack_event = body.get("event", {})
    if slack_event.get("type") != "message" or slack_event.get("subtype"):
        return _ok({"status": "ignored"})

    text = slack_event.get("text", "")
    channel_id = slack_event.get("channel", "")
    user_id = slack_event.get("user", "")
    actor_id = f"slack:{user_id}"

    if not text.strip() or not _is_allowed(actor_id):
        return _ok({"status": "blocked"})

    hermes_user_id = _resolve_user(actor_id)
    session_id = _build_session_id(hermes_user_id, "slack")

    payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "slack",
        "chatId": channel_id,
        "message": text,
    }

    agent_response = _invoke_agentcore(session_id, actor_id, payload)

    # Send response back to Slack.
    _send_slack_message(channel_id, agent_response, slack_event.get("ts"))

    return _ok({"status": "ok"})


def _verify_slack_signature(event: dict, signing_secret: str) -> bool:
    """Verify Slack request signing (v0)."""
    headers = event.get("headers", {})
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    body = event.get("body", "")

    if not timestamp or not signature:
        return False

    # Reject requests older than 5 minutes.
    if abs(time.time() - int(timestamp)) > 300:
        return False

    basestring = f"v0:{timestamp}:{body}"
    computed = "v0=" + hmac.new(
        signing_secret.encode(), basestring.encode(), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def _send_slack_message(channel: str, text: str, thread_ts: str | None = None) -> None:
    """Post a message to Slack via chat.postMessage."""
    if not text:
        return
    token = _get_secret("slack-bot-token")
    url = "https://slack.com/api/chat.postMessage"
    payload: dict[str, Any] = {
        "channel": channel,
        "text": text,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        logger.error("Slack chat.postMessage failed: %s", exc)


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def _verify_discord_signature(event: dict, public_key_hex: str) -> bool:
    """Verify Discord Ed25519 request signature."""
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError

    headers = event.get("headers", {})
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")
    raw_body = event.get("body", "")

    # API Gateway HTTP API v2 may base64-encode the body.
    if event.get("isBase64Encoded") and raw_body:
        import base64
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    if not signature or not timestamp:
        logger.warning("Discord verify: missing signature or timestamp")
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        verify_key.verify(f"{timestamp}{raw_body}".encode(), bytes.fromhex(signature))
        return True
    except BadSignatureError:
        logger.warning("Discord verify: bad signature")
        return False
    except Exception as exc:
        logger.warning("Discord verify: unexpected error: %s", exc)
        return False


def _handle_discord(event: dict) -> dict:
    # Verify Ed25519 signature (required by Discord).
    public_key = _get_secret("discord-public-key")
    if not _verify_discord_signature(event, public_key):
        logger.warning("Discord signature verification failed")
        return _ok({"error": "Invalid request signature"}, status=401)

    body = _parse_body(event)

    # Discord interaction verification (ping).
    if body.get("type") == 1:
        return _ok({"type": 1})

    # Only handle message-type interactions.
    if body.get("type") not in (2, 4):  # APPLICATION_COMMAND or AUTO_COMPLETE
        return _ok({"status": "ignored"})

    # For now, handle messages from the data payload.
    data = body.get("data", {})
    options = data.get("options", [])
    text = ""
    for opt in options:
        if opt.get("name") == "message":
            text = opt.get("value", "")
            break

    if not text:
        text = data.get("content", body.get("content", ""))

    user = body.get("member", {}).get("user", body.get("user", {}))
    user_id = user.get("id", "")
    channel_id = body.get("channel_id", "")
    actor_id = f"discord:{user_id}"

    if not text.strip() or not _is_allowed(actor_id):
        return _ok({"type": 4, "data": {"content": "Access denied."}})

    # Get interaction token for deferred followup.
    interaction_token = body.get("token", "")
    app_id = body.get("application_id", "")

    # Async-invoke ourselves to process in the background.
    followup_payload = {
        "_discord_followup": {
            "app_id": app_id,
            "interaction_token": interaction_token,
            "actor_id": actor_id,
            "channel_id": channel_id,
            "text": text,
        }
    }
    lambda_client = boto3.client("lambda")
    lambda_client.invoke(
        FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
        InvocationType="Event",  # Async
        Payload=json.dumps(followup_payload).encode(),
    )

    # Return deferred response immediately (shows "thinking..." in Discord).
    return _ok({"type": 5})


def _discord_followup(ctx: dict) -> None:
    """Process Discord interaction asynchronously and edit the deferred response."""
    app_id = ctx["app_id"]
    token = ctx["interaction_token"]
    actor_id = ctx["actor_id"]
    channel_id = ctx["channel_id"]
    text = ctx["text"]

    hermes_user_id = _resolve_user(actor_id)
    session_id = _build_session_id(hermes_user_id, "discord")

    payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "discord",
        "chatId": channel_id,
        "message": text,
    }

    logger.info("Discord followup: app_id=%s, actor=%s, text=%s", app_id, actor_id, text[:50])

    agent_response = _invoke_agentcore(session_id, actor_id, payload)
    logger.info("Discord followup: agent response length=%d", len(agent_response))

    # Edit the original deferred response via Discord webhook.
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
    content = agent_response[:2000] if agent_response.strip() else "No response from agent."
    data = json.dumps({"content": content}).encode()
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "HermesAgent/1.0 (https://github.com/hermes-agent)",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        logger.info("Discord followup: edit success, status=%d", resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("Discord followup edit failed: %s %s — %s", exc.code, exc.reason, body)
    except Exception as exc:
        logger.error("Discord followup edit failed: %s", exc)


# --------------------------------------------------------------------------
# Feishu (Lark)
# --------------------------------------------------------------------------

def _handle_feishu(event: dict) -> dict:
    body = _parse_body(event)
    logger.info("Feishu body: %s", json.dumps(body, ensure_ascii=False)[:2000])

    # Feishu URL verification challenge.
    if body.get("type") == "url_verification":
        return _ok({"challenge": body.get("challenge", "")})

    # Parse event (Feishu 2.0 event format).
    header = body.get("header", {})
    event_type = header.get("event_type", "")
    feishu_event = body.get("event", {})

    # Only handle im.message.receive_v1 events.
    if event_type != "im.message.receive_v1":
        return _ok({"status": "ignored"})

    sender = feishu_event.get("sender", {}).get("sender_id", {})
    user_id = sender.get("open_id", "")
    message = feishu_event.get("message", {})
    chat_id = message.get("chat_id", "")
    msg_type = message.get("message_type", "")

    # Only handle text messages for now.
    if msg_type != "text":
        return _ok({"status": "ignored"})

    try:
        content = json.loads(message.get("content", "{}"))
        text = content.get("text", "")
    except (json.JSONDecodeError, ValueError):
        text = ""

    actor_id = f"feishu:{user_id}"

    if not text.strip() or not _is_allowed(actor_id):
        return _ok({"status": "blocked"})

    hermes_user_id = _resolve_user(actor_id)
    session_id = _build_session_id(hermes_user_id, "feishu")

    payload = {
        "action": "chat",
        "userId": hermes_user_id,
        "actorId": actor_id,
        "channel": "feishu",
        "chatId": chat_id,
        "message": text,
    }

    agent_response = _invoke_agentcore(session_id, actor_id, payload)

    # Reply via Feishu API.
    _send_feishu_message(chat_id, message.get("message_id", ""), agent_response)

    return _ok({"status": "ok"})


def _send_feishu_message(chat_id: str, message_id: str, text: str) -> None:
    """Reply to a Feishu message."""
    if not text:
        return

    token = _get_feishu_tenant_token()
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
    data = json.dumps({
        "content": json.dumps({"text": text}),
        "msg_type": "text",
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        logger.error("Feishu reply failed: %s", exc)


def _get_feishu_tenant_token() -> str:
    """Get Feishu tenant_access_token using app credentials."""
    # Check cache first (token valid for ~2 hours, we cache in Lambda container).
    cached = _secrets_cache.get("_feishu_tenant_token")
    cached_at = _secrets_cache.get("_feishu_tenant_token_at", 0)
    if cached and (time.time() - cached_at) < 6000:  # refresh every ~100 min
        return cached

    app_id = _get_secret("feishu-app-id")
    app_secret = _get_secret("feishu-app-secret")

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
    })
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    token = resp.get("tenant_access_token", "")

    _secrets_cache["_feishu_tenant_token"] = token
    _secrets_cache["_feishu_tenant_token_at"] = time.time()
    return token


# --------------------------------------------------------------------------
# AgentCore invocation
# --------------------------------------------------------------------------

def _invoke_agentcore(session_id: str, actor_id: str, payload: dict) -> str:
    """Call InvokeAgentRuntime and return the text response."""
    try:
        response = _agentcore().invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=session_id,
            runtimeUserId=actor_id,
            payload=json.dumps(payload).encode("utf-8"),
        )

        # AgentCore returns the response in the "response" key (may be StreamingBody).
        result = response.get("response", "")
        if hasattr(result, "read"):
            result = result.read()
        if isinstance(result, bytes):
            result = result.decode("utf-8")

        # Parse SSE format: strip "data: " prefix and JSON-decode the string.
        result = result.strip()
        if result.startswith("data: "):
            result = result[6:]  # Strip "data: " prefix
        # May be a JSON-encoded string (with escaped \n, \", etc.)
        if result.startswith('"') and result.endswith('"'):
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, ValueError):
                pass

        logger.info("AgentCore response length=%d, status=%s",
                     len(result), response.get("statusCode", ""))
        return result
    except Exception as exc:
        logger.exception("AgentCore invocation failed")
        return f"Sorry, I couldn't process your message right now. ({exc})"


# --------------------------------------------------------------------------
# Identity management (DynamoDB)
# --------------------------------------------------------------------------

def _resolve_user(actor_id: str, username: str = "") -> str:
    """Look up or create a user in the DynamoDB identity table."""
    try:
        resp = identity_table.get_item(
            Key={"PK": f"CHANNEL#{actor_id}", "SK": "PROFILE"},
        )
        if "Item" in resp:
            return resp["Item"]["userId"]
    except ClientError:
        pass

    # New user — create entries.
    user_id = f"user_{hashlib.sha256(actor_id.encode()).hexdigest()[:16]}"
    now = int(time.time())

    try:
        identity_table.put_item(Item={
            "PK": f"CHANNEL#{actor_id}",
            "SK": "PROFILE",
            "userId": user_id,
            "username": username,
            "createdAt": now,
        })
        identity_table.put_item(Item={
            "PK": f"USER#{user_id}",
            "SK": f"CHANNEL#{actor_id}",
            "actorId": actor_id,
            "createdAt": now,
        })
    except ClientError as exc:
        logger.error("Failed to create identity: %s", exc)

    return user_id


def _is_allowed(actor_id: str) -> bool:
    """Check whether *actor_id* is on the allowlist."""
    # If IDENTITY_TABLE is not set, allow all (dev mode).
    if not os.environ.get("IDENTITY_TABLE"):
        return True
    try:
        resp = identity_table.get_item(
            Key={"PK": f"ALLOW#{actor_id}", "SK": "ALLOW"},
        )
        return "Item" in resp
    except ClientError:
        return False


def _build_session_id(user_id: str, channel: str) -> str:
    """Build an AgentCore session ID (must be >= 33 characters)."""
    base = f"{user_id}:{channel}"
    # Pad to ensure >= 33 characters.
    if len(base) < 33:
        base = base + ":" + "0" * (33 - len(base) - 1)
    return base


# --------------------------------------------------------------------------
# Secrets Manager (with in-memory cache)
# --------------------------------------------------------------------------

_secrets_cache: dict[str, str] = {}


def _get_secret(name: str) -> str:
    """Retrieve a secret from AWS Secrets Manager (cached per Lambda container)."""
    if name in _secrets_cache:
        return _secrets_cache[name]

    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=f"hermes/{name}")
    value = resp["SecretString"]
    _secrets_cache[name] = value
    return value


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _parse_body(event: dict) -> dict:
    body = event.get("body", "{}")
    if isinstance(body, str):
        # API Gateway may base64-encode the body.
        if event.get("isBase64Encoded"):
            import base64
            body = base64.b64decode(body).decode()
        return json.loads(body) if body else {}
    return body


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a long message into chunks that fit within *max_len*."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split on newline.
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _ok(body: dict, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
