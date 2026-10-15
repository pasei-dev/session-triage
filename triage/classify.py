"""The manual-stop discriminator.

Resume **only** an interruption the user did not cause on purpose. Every "no" here is conservative:
uncertain evidence means "do not touch", because a wrongly resumed session re-runs work the user
explicitly stopped.
"""

from __future__ import annotations

import os
import sqlite3

from . import db, logs, store
from .config import Config
from .model import Candidate, Verdict


def _steered_or_redirected(conn: sqlite3.Connection, session_id: str) -> bool:
    if not db.has_table(conn, "session_admissions"):
        return False
    try:
        row = conn.execute(
            "select 1 from session_admissions where target_session_id=?"
            " and intent in ('steer','redirect') limit 1",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def classify(
    conn: sqlite3.Connection,
    ledger: str | os.PathLike[str],
    candidate: Candidate,
    now: float,
    cfg: Config,
    events: list | None = None,
) -> Verdict:
    session_id = candidate.session_id
    sess = db.session_row(conn, session_id) or {}

    # 1. A deliberate stop seen live (/stop) is recorded to the ledger. Nothing overrides it.
    stop_ids = [session_id]
    if candidate.session_key:
        stop_ids.append(candidate.session_key)
    if store.stopped_any(ledger, stop_ids, candidate.signal_ts - cfg.resume_window_seconds):
        return Verdict(False, "manual stop recorded for this session")

    # 2. A planned gateway restart / shutdown killed the turn — infrastructure, NOT a user stop.
    #    This must be checked BEFORE the deliberate-close branch: a planned app restart can leave the
    #    surface's own close reason behind, which would otherwise read as a user decision.
    if events and logs.planned_restart(events, candidate.signal_ts - cfg.resume_window_seconds):
        return Verdict(True, "infrastructure restart killed the turn (planned gateway stop)")

    # 3. A surface that closed on purpose.
    end_reason = (sess.get("end_reason") or "").strip()
    if end_reason in db.DELIBERATE_END_REASONS:
        return Verdict(False, f"session closed deliberately ({end_reason})")

    # 3. The user steered or redirected the running turn: they were present and in control.
    if _steered_or_redirected(conn, session_id):
        return Verdict(False, "user steered or redirected the interrupted turn")

    # 4. A newer user message already landed: the user has moved on; a resume would talk over them.
    newest_user = db.max_user_ts(conn, session_id)
    if newest_user and newest_user > candidate.signal_ts + 1.0:
        return Verdict(False, "a newer user turn superseded the interruption")

    # 5. A completed assistant reply after the interruption means the turn already recovered.
    recovered_at = db.last_success_ts(conn, session_id)
    if recovered_at and recovered_at > candidate.signal_ts + 1.0:
        return Verdict(False, "a later assistant reply already completed the turn")

    return Verdict(True, f"involuntary interruption ({candidate.reason})")
