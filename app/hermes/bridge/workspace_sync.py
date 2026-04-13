"""S3-backed workspace persistence for AgentCore.

Mirrors the local ``/mnt/workspace/.hermes`` directory to an S3 prefix so that
user state (SQLite, memories, skills) survives across container image updates.

Pattern ported from the reference project's ``workspace-sync.js``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("agentcore.workspace_sync")

SKIP_PATTERNS: list[str] = [
    "__pycache__/*",
    "*.pyc",
    "*.pyo",
    "*.log",
    "*.tmp",
    "*.bak",
    "*.sock",
    "node_modules/*",
    ".git/*",
    "*.db-journal",
    "*.db-wal",
    "*.db-shm",
]


class WorkspaceSync:
    """Bi-directional sync between a local directory and an S3 prefix."""

    def __init__(self) -> None:
        self.bucket = os.environ.get("S3_BUCKET", "")
        self.workspace = Path(
            os.environ.get("WORKSPACE_PATH", "/mnt/workspace/.hermes"),
        )
        self.sync_interval = int(
            os.environ.get("WORKSPACE_SYNC_INTERVAL", "300"),
        )
        self._s3: Any = boto3.client("s3")
        self._stop = threading.Event()
        self._save_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def restore(self, namespace: str) -> None:
        """Download the workspace from S3 on container init."""
        if not self.bucket:
            logger.info("No S3_BUCKET — workspace restore skipped")
            return

        prefix = f"{namespace}/.hermes/"
        logger.info("Restoring workspace from s3://%s/%s …", self.bucket, prefix)
        count = 0

        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key: str = obj["Key"]
                    relative = key[len(prefix):]
                    if not relative or self._should_skip(relative):
                        continue
                    local_path = self.workspace / relative
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    self._s3.download_file(self.bucket, key, str(local_path))
                    count += 1
        except ClientError as exc:
            logger.warning("S3 restore error: %s", exc)
            return

        # Integrity-check restored SQLite databases.
        for db_file in self.workspace.glob("*.db"):
            self._verify_sqlite(db_file)

        logger.info("Workspace restore complete (%d files)", count)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, namespace: str) -> None:
        """Upload the workspace to S3."""
        if not self.bucket:
            return

        with self._save_lock:
            prefix = f"{namespace}/.hermes/"
            count = 0

            # Hot-copy any SQLite databases first.
            for db_file in self.workspace.glob("*.db"):
                bak = db_file.with_suffix(".db.s3bak")
                try:
                    self._sqlite_backup(db_file, bak)
                    self._s3.upload_file(str(bak), self.bucket, f"{prefix}{db_file.name}")
                    bak.unlink(missing_ok=True)
                    count += 1
                except Exception as exc:
                    logger.error("SQLite backup upload failed (%s): %s", db_file.name, exc)

            # Upload remaining files.
            for path in self.workspace.rglob("*"):
                if path.is_dir():
                    continue
                relative = str(path.relative_to(self.workspace))
                if self._should_skip(relative):
                    continue
                # Skip original DB files — already backed up above.
                if path.suffix == ".db":
                    continue
                try:
                    self._s3.upload_file(str(path), self.bucket, f"{prefix}{relative}")
                    count += 1
                except Exception as exc:
                    logger.error("Upload failed (%s): %s", relative, exc)

            logger.info("Workspace save complete (%d files → s3://%s/%s)", count, self.bucket, prefix)

    def save_immediate(self, namespace: str) -> None:
        """Non-blocking save — fire a background thread."""
        threading.Thread(target=self.save, args=(namespace,), daemon=True).start()

    # ------------------------------------------------------------------
    # Periodic sync
    # ------------------------------------------------------------------

    def start_periodic_save(self, namespace: str) -> None:
        """Start a background thread that saves to S3 every *sync_interval* seconds."""

        def _loop() -> None:
            while not self._stop.is_set():
                self._stop.wait(self.sync_interval)
                if self._stop.is_set():
                    break
                try:
                    self.save(namespace)
                except Exception as exc:
                    logger.error("Periodic save failed: %s", exc)

        t = threading.Thread(target=_loop, daemon=True, name="workspace-sync")
        t.start()
        logger.info(
            "Periodic workspace sync started (interval=%ds)", self.sync_interval,
        )

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _should_skip(relative: str) -> bool:
        for pattern in SKIP_PATTERNS:
            if fnmatch(relative, pattern) or fnmatch(relative, f"*/{pattern}"):
                return True
        return False

    @staticmethod
    def _sqlite_backup(src: Path, dst: Path) -> None:
        """Use the SQLite Online Backup API for a safe hot copy."""
        src_conn = sqlite3.connect(str(src))
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

    @staticmethod
    def _verify_sqlite(db_path: Path) -> None:
        """Run a quick integrity check — remove the file if it's corrupt."""
        try:
            conn = sqlite3.connect(str(db_path))
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            if result and result[0] != "ok":
                logger.warning("SQLite integrity check failed for %s — removing", db_path)
                db_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Cannot open %s — removing corrupt database", db_path)
            db_path.unlink(missing_ok=True)
