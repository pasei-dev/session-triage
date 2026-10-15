"""Detect a stalled session in two classes, and recover the one that is safe to recover.

* **post-fence stall** — the visible one. An admission is fenced ``unknown`` (a process that died
  mid-turn) and a *queued successor* waits behind it. Nothing runs until the fence is acknowledged.
* **pre-fence stall** — the invisible one. A live admission is ``started`` but the session has logged
  no progress for longer than the bound while a tool or API call is open. The harness' turn watchdog
  does not cover a silent tool call, so the bound has to be ours.

Never auto-kills: only a fenced admission is recovered (the turn is already lost), and the recovery is
the harness' own acknowledgement. A pre-fence stall is attributed and reported, never interrupted.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field

from . import db, logs
from .config import Config


@dataclass
class Stall:
    session_id: str
    kind: str  # "post_fence" | "pre_fence" | "paused"
    detail: str
    attribution: str  # "provider" | "tool" | "infrastructure" | "unknown"
    evidence: list[str] = field(default_factory=list)
    last_progress_ts: float | None = None
    stalled_for: float | None = None
    admission_status: str | None = None
    has_queued_successor: bool = False


def _admissions(conn: sqlite3.Connection) -> list[dict]:
    if not db.has_table(conn, "session_admissions"):
        return []
    try:
        rows = conn.execute(
            "select seq, target_session_id, status, intent from session_admissions order by seq"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def find_stalls(conn: sqlite3.Connection, events: list[logs.Event], now: float, cfg: Config,
                min_age_seconds: int | None = None) -> list[Stall]:
    """Post-fence first (a lost turn is already lost), then pre-fence live stalls."""
    rows = _admissions(conn)
    by_session: dict[str, list[dict]] = {}
    for row in rows:  # ordered by seq
        by_session.setdefault(row["target_session_id"], []).append(row)

    stalls: list[Stall] = []
    for session_id, session_rows in by_session.items():
        # A fenced admission is not necessarily the newest row — a queued successor sits after it.
        unknowns = [row for row in session_rows if row["status"] == "unknown"]
        if unknowns:
            fenced = unknowns[-1]
            successor = any(
                row["seq"] > fenced["seq"] and row["status"] in ("queued", "started")
                for row in session_rows
            )
            attribution = logs.attribute(events, session_id, 0.0)
            stalls.append(Stall(
                session_id=session_id, kind="post_fence", admission_status="unknown",
                detail="admission fenced unknown; a successor is waiting" if successor
                       else "admission fenced unknown",
                attribution=attribution.kind, evidence=attribution.evidence,
                has_queued_successor=successor,
            ))
            continue

        if session_rows[-1]["status"] != "started":
            continue
        last = logs.last_event_ts(events, session_id)
        if last <= 0:
            continue
        stalled_for = now - last
        floor = cfg.stall_seconds if min_age_seconds is None else min_age_seconds
        if stalled_for < floor:
            continue
        attribution = logs.attribute(events, session_id, last - 1.0)
        stalls.append(Stall(
            session_id=session_id, kind="pre_fence", admission_status="started",
            detail=f"live turn with no progress for {stalled_for:.0f}s",
            attribution=attribution.kind, evidence=attribution.evidence,
            last_progress_ts=last, stalled_for=stalled_for,
        ))

    _add_paused(events, now, cfg, stalls)
    return stalls


def _add_paused(events: list[logs.Event], now: float, cfg: Config, stalls: list[Stall]) -> None:
    """Report a session the host paused (``... paused: admission_conflict`` / ``runtime_draining``).

    These lines never reach an admission row and carry no ``[sid]`` bracket, so the store lookups
    above miss them. Report only — a paused session is never interrupted.
    """
    seen = {item.session_id for item in stalls}
    for session_id, reason, ts in logs.paused_sessions(events, since=now - cfg.max_age_seconds):
        if session_id in seen:
            continue
        seen.add(session_id)
        stalls.append(Stall(
            session_id=session_id, kind="paused", admission_status=None,
            detail=f"session paused: {reason}",
            attribution="infrastructure" if "drain" in reason else "unknown",
            evidence=[f"{reason} at {ts:.0f}"],
        ))


def hermes_bin() -> str | None:
    import os

    override = os.environ.get("SESSION_TRIAGE_HERMES_BIN")
    if override:
        return override
    return shutil.which("hermes")


def recover_fence(session_id: str, binary: str | None = None, timeout: float = 60.0) -> tuple[bool, str]:
    """Acknowledge a fenced admission so the queued successor can run.

    This is the harness' own recovery (``prompt.resolve_unknown``), never a hand-edit of the store.
    """
    binary = binary or hermes_bin()
    if not binary:
        return False, "no `hermes` executable on PATH (set SESSION_TRIAGE_HERMES_BIN)"
    try:
        proc = subprocess.run(
            [binary, "sessions", "discard", session_id, "--yes"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, output[:400]


def fence_resolved(conn: sqlite3.Connection, session_id: str) -> bool:
    """True when the session no longer has any ``unknown`` admission (proof the recovery landed)."""
    for row in _admissions(conn):
        if row["target_session_id"] == session_id and row["status"] == "unknown":
            return False
    return True
