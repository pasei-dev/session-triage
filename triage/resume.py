"""Decide whether a candidate may be resumed, and if so, resume it once.

The resume is a plain **user-role** turn injected into the session (the same mechanism the session
heartbeat uses), so message alternation and prompt caching hold. It is deliberately dumb about the
conversation and precise about the gates: every reason to hold back is checked before the inject.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from . import detect, store
from .config import Config
from .model import Candidate, Diagnosis, RESUME_SENTINEL

Inject = Callable[..., bool]


def lease_live(conn: sqlite3.Connection, session_id: str, now: float) -> bool:
    """True when the session still holds a lease whose holder is a live process."""
    if not _has(conn, "session_turn_leases"):
        return False
    try:
        rows = conn.execute(
            "select holder, expires_at from session_turn_leases where conversation_id=?",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        pid = detect._holder_pid(row["holder"])
        if detect.pid_alive(pid) and float(row["expires_at"] or 0) > now:
            return True
    return False


def _has(conn: sqlite3.Connection, table: str) -> bool:
    from . import db

    return db.has_table(conn, table)


def build_prompt(candidate: Candidate, diagnosis: Diagnosis) -> str:
    """The resume turn.

    A plain user-role message (so message alternation and prompt caching hold), prefixed with
    ``RESUME_SENTINEL`` so it reads as a plugin note in the transcript and is never mistaken for a
    real user message by ``db.max_user_ts``.
    """
    causes = ", ".join(diagnosis.causes) or "an interruption"
    fixed = ", ".join(diagnosis.fixed)
    tail = (
        f" The background cause ({fixed}) was repaired before this message."
        if fixed
        else ""
    )
    return (
        f"{RESUME_SENTINEL} Automated note: this session was auto-resumed by the session-triage "
        f"plugin after an interruption. Your previous turn was interrupted "
        f"({candidate.reason}; diagnosed cause: {causes}).{tail} "
        "Continue from where you left off. Do not repeat side effects that already ran — verify "
        "them first (read the file, re-run the check, inspect the git state) and only redo what is "
        "genuinely missing. If nothing remains, reply with a one-line confirmation that the work is "
        "complete."
    )


def gate(
    conn: sqlite3.Connection,
    ledger: str | os.PathLike[str],
    candidate: Candidate,
    diagnosis: Diagnosis,
    *,
    now: float,
    cfg: Config,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)``. ``reason`` is a human sentence either way."""
    if not cfg.auto_resume:
        return False, "auto_resume is off"
    if cfg.pause_file and Path(cfg.pause_file).exists():
        return False, f"paused by {cfg.pause_file}"
    if not candidate.session_key:
        return False, "no durable session_key to target"
    if diagnosis.blocking:
        return False, "blocking cause unrepaired: " + ", ".join(diagnosis.blocking)
    if lease_live(conn, candidate.session_id, now):
        return False, "a live lease still holds the session"

    since = now - cfg.resume_window_seconds
    per_session = store.resume_count(ledger, since=since, session_id=candidate.session_id)
    if per_session >= cfg.max_resumes_per_session:
        return False, f"resume budget spent for this session ({per_session})"
    total = store.resume_count(ledger, since=since)
    if total >= cfg.max_resumes_total:
        return False, f"global resume budget spent ({total})"
    if store.fenced(ledger, candidate.session_id, candidate.source, candidate.reason,
                    ttl=cfg.fence_ttl_seconds, now=now):
        return False, "already resumed for this abruption (fenced)"
    return True, "resumable"


def do_resume(inject: Inject, prompt: str, session_key: str) -> bool:
    """Inject the resume turn. Any failure is a defer, never a raised exception into the sweep."""
    try:
        return bool(inject(prompt, session_key=session_key))
    except Exception:
        return False
