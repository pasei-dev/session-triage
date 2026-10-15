"""Read-only access to a profile's ``state.db`` and the small queries the stages share.

Every reader opens the database ``mode=ro``: a triage pass never writes session state, only the
ledger. A missing table or column (an older Hermes, a fresh profile) is treated as "no signal"
rather than an error, so the sweep degrades to the live-hook signals instead of crashing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

#: end_reason values that mean a surface closed the session on purpose — never resumable.
DELIBERATE_END_REASONS = frozenset({"agent_close", "cli_close", "tui_close", "tui_shutdown"})

#: end_reason values that mean the host reaped the session while it was still open.
REAPED_END_REASONS = frozenset({"startup_orphan_reap", "ws_orphan_reap"})

#: A turn cut by the provider, or a model that stopped on reasoning alone.
INCOMPLETE_FINISH_REASONS = frozenset({"incomplete", "length"})


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open ``state.db`` read-only. Raises if the file does not exist."""
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "select 1 from sqlite_master where type='table' and name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return _table_exists(conn, name)


def session_row(conn: sqlite3.Connection, session_id: str) -> Optional[dict[str, Any]]:
    if not _table_exists(conn, "sessions"):
        return None
    try:
        row = conn.execute(
            "select id, session_key, end_reason, origin_json, last_activity_at, started_at,"
            " model, compression_failure_streak, compression_recovery_deadline"
            " from sessions where id=?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def sessions_by_id(conn: sqlite3.Connection, session_id: str) -> Optional[dict[str, Any]]:
    return session_row(conn, session_id)


def last_message(conn: sqlite3.Connection, session_id: str) -> Optional[dict[str, Any]]:
    if not _table_exists(conn, "messages"):
        return None
    try:
        row = conn.execute(
            "select role, finish_reason, content, reasoning_content, timestamp"
            " from messages where session_id=? order by id desc limit 1",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def max_user_ts(conn: sqlite3.Connection, session_id: str) -> float:
    """Timestamp of the newest user-role message, or 0.0 when there is none."""
    if not _table_exists(conn, "messages"):
        return 0.0
    try:
        row = conn.execute(
            "select max(timestamp) from messages where session_id=? and role='user'",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    if row is None or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return 0.0


def last_success_ts(conn: sqlite3.Connection, session_id: str) -> float:
    """Timestamp of the newest completed assistant reply (a real one, not a silence marker)."""
    if not _table_exists(conn, "messages"):
        return 0.0
    try:
        row = conn.execute(
            "select max(timestamp) from messages where session_id=? and role='assistant'"
            " and finish_reason='stop' and content is not null and trim(content) != ''",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    if row is None or row[0] is None:
        return 0.0
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return 0.0


def max_heartbeat(conn: sqlite3.Connection) -> Optional[float]:
    if not _table_exists(conn, "gateway_heartbeats"):
        return None
    try:
        row = conn.execute("select max(last_heartbeat) from gateway_heartbeats").fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None
