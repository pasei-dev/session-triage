"""Find sessions that look abandoned, from the durable signals a crash leaves behind.

Two independent sources, unioned and de-duplicated:

* **turn leases** — ``session_turn_leases`` holds a row per in-flight turn stamped with the holder
  PID. A lease past its expiry whose PID is gone is a turn that died with its process, which no
  in-memory marker survives.
* **admissions** — ``session_admissions`` is the durable inbound claim. A target whose *newest*
  admission is still ``started``/``unknown`` has a turn that was accepted and never settled.
* **reaped sessions** — a session whose ``end_reason`` says the host reaped it while open and whose
  last activity is recent.

The live ``on_session_end`` hook adds a fourth source at ``register()`` time; it seeds the ledger and
this sweep re-reads the durable state behind it.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Optional

from . import db, store
from .config import Config
from .model import Candidate

_PID_RE = re.compile(r"pid=(\d+)")


def pid_alive(pid: Optional[int]) -> bool:
    """True when ``pid`` names a live process (or unknown, which is treated as live = do not touch)."""
    if pid is None or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _holder_pid(holder: str) -> Optional[int]:
    match = _PID_RE.search(holder or "")
    return int(match.group(1)) if match else None


def _signal_ts(row: Optional[dict]) -> float:
    if not row:
        return 0.0
    for key in ("last_activity_at", "started_at"):
        value = row.get(key)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _fresh_enough(ts: float, now: float, cfg: Config) -> bool:
    if ts <= 0:
        return False
    age = now - ts
    return cfg.min_age_seconds <= age <= cfg.max_age_seconds


def from_leases(conn: sqlite3.Connection, now: float, cfg: Config) -> list[Candidate]:
    if not db.has_table(conn, "session_turn_leases"):
        return []
    out: list[Candidate] = []
    try:
        rows = conn.execute(
            "select conversation_id, holder, expires_at from session_turn_leases where expires_at < ?",
            (now - cfg.lease_grace_seconds,),
        ).fetchall()
    except sqlite3.Error:
        return []
    for row in rows:
        pid = _holder_pid(row["holder"])
        if pid_alive(pid):
            continue  # the turn is genuinely live; leave it alone
        sess = db.session_row(conn, row["conversation_id"])
        ts = _signal_ts(sess) or float(row["expires_at"] or 0.0)
        if not _fresh_enough(ts, now, cfg):
            continue
        out.append(
            Candidate(
                session_id=row["conversation_id"],
                session_key=(sess or {}).get("session_key"),
                source="lease",
                reason="turn_lease_expired",
                signal_ts=ts,
                holder_pid=pid,
            )
        )
    return out


def from_admissions(conn: sqlite3.Connection, now: float, cfg: Config) -> list[Candidate]:
    if not db.has_table(conn, "session_admissions"):
        return []
    out: list[Candidate] = []
    try:
        targets = conn.execute(
            "select distinct target_session_id from session_admissions"
        ).fetchall()
    except sqlite3.Error:
        return []
    for target in targets:
        sid = target["target_session_id"]
        try:
            latest = conn.execute(
                "select status from session_admissions where target_session_id=? order by seq desc limit 1",
                (sid,),
            ).fetchone()
        except sqlite3.Error:
            continue
        if latest is None or latest["status"] not in ("started", "unknown"):
            continue
        sess = db.session_row(conn, sid)
        ts = _signal_ts(sess)
        if not _fresh_enough(ts, now, cfg):
            continue
        out.append(
            Candidate(
                session_id=sid,
                session_key=(sess or {}).get("session_key"),
                source="admission",
                reason="admission_unsettled",
                signal_ts=ts,
            )
        )
    return out


def from_reaped(conn: sqlite3.Connection, now: float, cfg: Config) -> list[Candidate]:
    if not db.has_table(conn, "sessions"):
        return []
    out: list[Candidate] = []
    placeholders = ",".join("?" for _ in db.REAPED_END_REASONS)
    try:
        rows = conn.execute(
            f"select id, session_key, end_reason, last_activity_at from sessions"
            f" where end_reason in ({placeholders})",
            tuple(db.REAPED_END_REASONS),
        ).fetchall()
    except sqlite3.Error:
        return []
    for row in rows:
        ts = _signal_ts(dict(row))
        if not _fresh_enough(ts, now, cfg):
            continue
        # A reaped session whose last message is a completed assistant reply needs nothing.
        last = db.last_message(conn, row["id"])
        if last and last.get("role") == "assistant" and last.get("finish_reason") == "stop" \
                and last.get("content"):
            continue
        out.append(
            Candidate(
                session_id=row["id"],
                session_key=row["session_key"],
                source="reaped",
                reason=row["end_reason"],
                signal_ts=ts,
            )
        )
    return out


def from_ledger(conn: sqlite3.Connection, ledger, now: float, cfg: Config) -> list[Candidate]:
    """Interruptions witnessed live by the ``on_session_end`` hook and recorded to the ledger.

    The hook catches a cut turn that left no durable trace in ``state.db`` (a provider cut a clean
    turn); the DB sources catch a crash that fired no hook. Neither alone is complete.
    """
    if not ledger:
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    try:
        records = store.read_all(ledger)
    except Exception:
        return []
    for record in records:
        if record.get("kind") != "interrupted":
            continue
        session_id = record.get("session_id")
        if not session_id or session_id in seen:
            continue
        if not _fresh_enough(float(record.get("ts") or 0.0), now, cfg):
            continue
        sess = db.session_row(conn, session_id)
        if sess is None:
            continue
        seen.add(session_id)
        out.append(
            Candidate(
                session_id=session_id,
                session_key=sess.get("session_key"),
                source="hook",
                reason=str(record.get("reason") or "interrupted_turn"),
                signal_ts=float(record.get("ts") or 0.0),
            )
        )
    return out


def find_candidates(conn: sqlite3.Connection, now: float, cfg: Config,
                    ledger=None) -> list[Candidate]:
    """All abandonment candidates, de-duplicated by session (lease wins over the rest)."""
    seen: dict[str, Candidate] = {}
    order = {"lease": 0, "admission": 1, "reaped": 2, "hook": 3}
    sources = (from_leases(conn, now, cfg) + from_admissions(conn, now, cfg)
               + from_reaped(conn, now, cfg) + from_ledger(conn, ledger, now, cfg))
    for candidate in sources:
        prior = seen.get(candidate.session_id)
        if prior is None or order.get(candidate.source, 9) < order.get(prior.source, 9):
            seen[candidate.session_id] = candidate
    return sorted(seen.values(), key=lambda c: c.signal_ts, reverse=True)
