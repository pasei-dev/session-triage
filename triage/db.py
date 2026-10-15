"""Read-only access to a profile's ``state.db`` and the small queries the stages share.

Every reader opens the database ``mode=ro``: a triage pass never writes session state, only the
ledger. A missing table or column (an older Hermes, a fresh profile) is treated as "no signal"
rather than an error, so the sweep degrades to the live-hook signals instead of crashing.

``session_row`` reads only the columns the live schema actually has, so one column this build does
not know (or a Hermes that renamed one) leaves that field absent rather than failing the whole
query — the failure mode that once blanked *every* row and silently disabled detection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from .model import RESUME_SENTINEL

#: end_reason values that mean a surface closed the session on purpose — never resumable.
DELIBERATE_END_REASONS = frozenset({"agent_close", "cli_close", "tui_close", "tui_shutdown"})

#: end_reason values that mean the host reaped the session while it was still open.
REAPED_END_REASONS = frozenset({"startup_orphan_reap", "ws_orphan_reap"})

#: A turn cut by the provider, or a model that stopped on reasoning alone.
INCOMPLETE_FINISH_REASONS = frozenset({"incomplete", "length"})

#: The columns the stages want from ``sessions``. ``session_row`` reads the intersection of this
#: list with the live schema, so a renamed or absent column degrades to a missing field, never a
#: query error that returns ``None`` for every session.
SESSION_COLUMNS = (
    "id", "session_key", "end_reason", "origin_json", "last_activity_at", "started_at",
    "model", "compression_fallback_streak", "compression_recovery_deadline",
)


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


def table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    """The column names a table actually has, or an empty set when it is absent or unreadable."""
    try:
        rows = conn.execute(f"pragma table_info({name})").fetchall()
    except sqlite3.Error:
        return set()
    return {row["name"] for row in rows}


def session_row(conn: sqlite3.Connection, session_id: str) -> Optional[dict[str, Any]]:
    """The ``sessions`` row for ``session_id``, restricted to the columns this schema has.

    Reads ``SESSION_COLUMNS`` intersected with ``pragma table_info``: a schema that lacks a column
    still yields the row rather than failing the whole query. Returns ``None`` only when the table
    is absent or the session genuinely does not exist.
    """
    if not _table_exists(conn, "sessions"):
        return None
    present = [name for name in SESSION_COLUMNS if name in table_columns(conn, "sessions")]
    if "id" not in present:
        return None
    try:
        row = conn.execute(
            f"select {', '.join(present)} from sessions where id=?", (session_id,)
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
    """Timestamp of the newest user-role message, or 0.0 when there is none.

    A resume turn this plugin injected (a ``RESUME_SENTINEL`` prefix) is excluded: the engine must
    never read its own note as a newer user turn that supersedes the interruption.
    """
    if not _table_exists(conn, "messages"):
        return 0.0
    try:
        row = conn.execute(
            "select max(timestamp) from messages where session_id=? and role='user'"
            " and (content is null or content not like ?)",
            (session_id, RESUME_SENTINEL + "%"),
        ).fetchone()
    except sqlite3.Error:
        return 0.0
    return _as_float(row[0] if row is not None else None)


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
    return _as_float(row[0] if row is not None else None)


def max_heartbeat(conn: sqlite3.Connection) -> Optional[float]:
    if not _table_exists(conn, "gateway_heartbeats"):
        return None
    try:
        row = conn.execute("select max(last_heartbeat) from gateway_heartbeats").fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return _as_float(row[0])


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
