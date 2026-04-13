"""Tests for workspace sync module."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge.workspace_sync import WorkspaceSync, SKIP_PATTERNS


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace directory."""
    ws = tmp_path / ".hermes"
    ws.mkdir()
    (ws / "memories").mkdir()
    (ws / "skills").mkdir()
    return ws


@pytest.fixture
def sync(tmp_workspace):
    """Create a WorkspaceSync with mocked S3 client."""
    with patch.dict(os.environ, {
        "S3_BUCKET": "test-bucket",
        "WORKSPACE_PATH": str(tmp_workspace),
    }):
        s = WorkspaceSync()
        s._s3 = MagicMock()
        return s


# --------------------------------------------------------------------------
# Tests — skip patterns
# --------------------------------------------------------------------------

def test_skip_pycache():
    s = WorkspaceSync.__new__(WorkspaceSync)
    assert s._should_skip("__pycache__/module.pyc") is True


def test_skip_log_files():
    s = WorkspaceSync.__new__(WorkspaceSync)
    assert s._should_skip("agent.log") is True


def test_skip_db_journal():
    s = WorkspaceSync.__new__(WorkspaceSync)
    assert s._should_skip("state.db-journal") is True


def test_allow_normal_files():
    s = WorkspaceSync.__new__(WorkspaceSync)
    assert s._should_skip("MEMORY.md") is False
    assert s._should_skip("skills/my_skill/SKILL.md") is False
    assert s._should_skip("config.yaml") is False


# --------------------------------------------------------------------------
# Tests — SQLite verification
# --------------------------------------------------------------------------

def test_verify_sqlite_good(tmp_workspace):
    """Good SQLite file should be kept."""
    db_path = tmp_workspace / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.close()

    WorkspaceSync._verify_sqlite(db_path)
    assert db_path.exists()


def test_verify_sqlite_corrupt(tmp_workspace):
    """Corrupt file should be removed."""
    db_path = tmp_workspace / "corrupt.db"
    db_path.write_bytes(b"not a sqlite database at all")

    WorkspaceSync._verify_sqlite(db_path)
    assert not db_path.exists()


# --------------------------------------------------------------------------
# Tests — SQLite backup
# --------------------------------------------------------------------------

def test_sqlite_backup(tmp_workspace):
    """Hot backup should produce a valid copy."""
    src = tmp_workspace / "source.db"
    dst = tmp_workspace / "backup.db"

    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'hello')")
    conn.commit()
    conn.close()

    WorkspaceSync._sqlite_backup(src, dst)

    conn = sqlite3.connect(str(dst))
    row = conn.execute("SELECT val FROM t WHERE id = 1").fetchone()
    conn.close()

    assert row[0] == "hello"


# --------------------------------------------------------------------------
# Tests — save
# --------------------------------------------------------------------------

def test_save_uploads_files(sync, tmp_workspace):
    """Save should upload all non-skipped files to S3."""
    # Create some test files.
    (tmp_workspace / "MEMORY.md").write_text("memory content")
    (tmp_workspace / "skills" / "test.md").write_text("skill")
    (tmp_workspace / "agent.log").write_text("log")  # Should be skipped.

    sync.save("user123")

    # Check that S3 upload was called for MEMORY.md and skills/test.md.
    uploaded_keys = [
        call.args[2] for call in sync._s3.upload_file.call_args_list
    ]
    assert any("MEMORY.md" in k for k in uploaded_keys)
    assert any("test.md" in k for k in uploaded_keys)
    # Log file should NOT be uploaded.
    assert not any("agent.log" in k for k in uploaded_keys)


# --------------------------------------------------------------------------
# Tests — restore
# --------------------------------------------------------------------------

def test_restore_skips_when_no_bucket():
    """Restore should be a no-op when S3_BUCKET is empty."""
    with patch.dict(os.environ, {"S3_BUCKET": "", "WORKSPACE_PATH": "/tmp"}):
        s = WorkspaceSync()
        s._s3 = MagicMock()
        s.restore("user123")
        s._s3.get_paginator.assert_not_called()
