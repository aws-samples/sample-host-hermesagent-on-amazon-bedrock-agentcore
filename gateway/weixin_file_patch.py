"""Monkey-patch WeixinAdapter.send() to auto-convert long text to .md file.

When the response text exceeds a configurable threshold (default 2000 chars),
the patch writes the markdown to a temporary .md file and sends it as a
document via the existing ``send_document()`` / ``_send_file()`` CDN upload
pipeline.  This way the WeChat user receives a nicely formatted file instead
of raw markdown text split across multiple messages.

Usage:
    from weixin_file_patch import patch_weixin_send
    patch_weixin_send()

Must be called AFTER the gateway is imported (WeixinAdapter must exist).
"""

from __future__ import annotations

import logging
import os
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("weixin_file_patch")

# Threshold in characters — responses longer than this are sent as .md files.
FILE_THRESHOLD = int(os.environ.get("WEIXIN_FILE_THRESHOLD", "2000"))


def patch_weixin_send():
    """Wrap WeixinAdapter.send() with auto-file-conversion logic."""

    from gateway.platforms.weixin import WeixinAdapter

    _original_send = WeixinAdapter.send

    async def _patched_send(self, chat_id, content, reply_to=None, metadata=None):
        # Only convert if content exceeds threshold
        if content and len(content) > FILE_THRESHOLD:
            logger.info(
                "[weixin_file_patch] Response length %d > %d, sending as .md file",
                len(content), FILE_THRESHOLD,
            )
            try:
                result = await _send_as_md_file(self, chat_id, content)
                if result.success:
                    return result
                # Fall through to original send if file delivery failed.
                logger.warning(
                    "[weixin_file_patch] File send failed (%s), falling back to text",
                    result.error,
                )
            except Exception as exc:
                logger.warning(
                    "[weixin_file_patch] File send error: %s, falling back to text",
                    exc,
                )

        # Default: send as text (original behavior)
        return await _original_send(self, chat_id, content, reply_to=reply_to, metadata=metadata)

    WeixinAdapter.send = _patched_send
    logger.info(
        "Patched WeixinAdapter.send() — responses > %d chars sent as .md file",
        FILE_THRESHOLD,
    )


async def _send_as_md_file(adapter, chat_id: str, content: str):
    """Write content to a temp .md file and send via send_document()."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"reply_{timestamp}.md"

    # Write to temp directory
    tmp_dir = Path(tempfile.gettempdir()) / "hermes_weixin_files"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    file_path = tmp_dir / filename
    file_path.write_text(content, encoding="utf-8")

    try:
        # Send a brief summary as text first, then the file.
        summary = _make_summary(content)
        result = await adapter.send_document(
            chat_id=chat_id,
            file_path=str(file_path),
            caption=summary,
        )
        return result
    finally:
        # Clean up temp file (best-effort)
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass


def _make_summary(content: str, max_len: int = 100) -> str:
    """Extract a short summary from the first non-empty line of content."""
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            if len(stripped) > max_len:
                return stripped[:max_len] + "..."
            return stripped
    return ""
