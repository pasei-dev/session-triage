"""A throwaway ``state.db`` shaped like the real one, small enough to assert against.

Only the tables the triage stages read are created, with the columns those stages use. The plugin
package is put on ``sys.path`` so the tests import the same code the loader runs.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, session_key TEXT, end_reason TEXT, origin_json TEXT,
    last_activity_at REAL, started_at REAL, model TEXT,
    compression_fallback_streak INTEGER DEFAULT 0, compression_recovery_deadline REAL
);
CREATE TABLE session_turn_leases (
    conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
    acquired_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE session_admissions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, admission_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL, principal_id TEXT NOT NULL, target_session_id TEXT NOT NULL,
    lineage_json TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
    intent TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT,
    owner_epoch INTEGER, generation INTEGER
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
    finish_reason TEXT, reasoning_content TEXT, timestamp REAL
);
CREATE TABLE gateway_heartbeats (
    backend_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL
);
"""


def make_db(path, *, sessions=(), leases=(), admissions=(), messages=(), heartbeats=()) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for row in sessions:
        conn.execute(
            "insert into sessions(id, session_key, end_reason, origin_json, last_activity_at,"
            " started_at, model, compression_fallback_streak, compression_recovery_deadline)"
            " values(?,?,?,?,?,?,?,?,?)",
            (row["id"], row.get("session_key"), row.get("end_reason"), row.get("origin_json"),
             row.get("last_activity_at"), row.get("started_at"), row.get("model"),
             row.get("compression_fallback_streak", 0), row.get("compression_recovery_deadline")),
        )
    for row in leases:
        conn.execute(
            "insert into session_turn_leases(conversation_id, holder, acquired_at, expires_at)"
            " values(?,?,?,?)",
            (row["conversation_id"], row["holder"], row.get("acquired_at", 0.0), row["expires_at"]),
        )
    for row in admissions:
        conn.execute(
            "insert into session_admissions(admission_id, request_id, principal_id,"
            " target_session_id, lineage_json, payload_json, payload_digest, intent, status,"
            " owner_epoch, generation) values(?,?,?,?,?,?,?,?,?,?,?)",
            (row["admission_id"], row.get("request_id", "req"), row.get("principal_id", "prin"),
             row["target_session_id"], "{}", "{}", "digest", row.get("intent", "queue"),
             row["status"], row.get("owner_epoch", 1), row.get("generation", 1)),
        )
    for row in messages:
        conn.execute(
            "insert into messages(session_id, role, content, finish_reason, reasoning_content,"
            " timestamp) values(?,?,?,?,?,?)",
            (row["session_id"], row["role"], row.get("content"), row.get("finish_reason"),
             row.get("reasoning_content"), row.get("timestamp")),
        )
    for row in heartbeats:
        conn.execute(
            "insert into gateway_heartbeats(backend_id, pid, started_at, last_heartbeat)"
            " values(?,?,?,?)",
            (row["backend_id"], row.get("pid", 1), row.get("started_at", 0.0), row["last_heartbeat"]),
        )
    conn.commit()
    conn.close()
    return path


#: A pid the OS will never hand out, so ``os.kill`` reports the holder as gone.
DEAD_PID = 999999
