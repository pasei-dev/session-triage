"""Probe the background causes an interruption is usually standing on.

Each probe is cheap, read-only and independent: a missing tool, an unreadable log or an absent table
yields *no signal* rather than an error, so a diagnosis never blocks the sweep. A cause is **blocking**
when resuming under it would only reproduce the same interruption.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from . import db
from .config import Config
from .model import Diagnosis

#: Markers in ``logs/errors.log`` that name a specific, non-transient cause.
_CREDENTIAL_MARKERS = ("launch_credentials_unavailable", "recover_config_secrets",
                       "session_policy_credentials")
_WORKSPACE_MARKERS = ("workspace_unavailable",)

#: The workspace checkpoint store's per-index lock files — a stale one wedges the file-write path.
_CHECKPOINT_LOCK_GLOBS = ("checkpoints/store/indexes/*.lock",)


def _free_mb(path: str | os.PathLike[str]) -> float | None:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / (1024 * 1024)
    except OSError:
        return None


def _db_size_mb(home: Path) -> float | None:
    try:
        return (home / "state.db").stat().st_size / (1024 * 1024)
    except OSError:
        return None


def _stale_checkpoint_lock(home: Path, now: float, cfg: Config) -> str | None:
    """The name of a checkpoint index lock older than the bound, or ``None``.

    A wedged ``git add -A`` (``index.lock: File exists``) is what stretches a file-write call from
    seconds to tens of minutes. The lock is reported, never deleted — the plugin does not touch
    another process's lock.
    """
    for pattern in _CHECKPOINT_LOCK_GLOBS:
        for lock in sorted(home.glob(pattern)):
            try:
                age = now - lock.stat().st_mtime
            except OSError:
                continue
            if age > cfg.checkpoint_lock_stale_seconds:
                return f"{lock.name} stale for {age:.0f}s"
    return None


def _log_hits(errors_log: Path, markers: tuple[str, ...], now: float, cfg: Config) -> bool:
    """True when a marker appears in the tail of a *recently written* errors log."""
    try:
        stat = errors_log.stat()
    except OSError:
        return False
    if (now - stat.st_mtime) > cfg.max_age_seconds:
        return False  # a stale log is not evidence about this interruption
    try:
        with open(errors_log, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return False
    return any(marker in tail for marker in markers)


def diagnose(
    conn: sqlite3.Connection,
    hermes_home: str | os.PathLike[str],
    session_id: str,
    now: float,
    cfg: Config,
) -> Diagnosis:
    home = Path(hermes_home)
    result = Diagnosis()

    free_mb = _free_mb(home)
    if free_mb is not None and free_mb < cfg.disk_free_floor_mb:
        result.causes.append("disk_full")
        result.blocking.append("disk_full")
        result.notes.append(f"{free_mb:.0f} MB free (< {cfg.disk_free_floor_mb} MB)")

    errors_log = home / "logs" / "errors.log"
    if _log_hits(errors_log, _CREDENTIAL_MARKERS, now, cfg):
        result.causes.append("credentials_unavailable")
        result.blocking.append("credentials_unavailable")
    if _log_hits(errors_log, _WORKSPACE_MARKERS, now, cfg):
        result.causes.append("workspace_unavailable")
        result.blocking.append("workspace_unavailable")

    sess = db.session_row(conn, session_id) or {}
    streak = sess.get("compression_fallback_streak") or 0
    deadline = sess.get("compression_recovery_deadline") or 0
    if streak or (deadline and float(deadline) > now):
        result.causes.append("context_overload")
        result.blocking.append("context_overload")

    heartbeat = db.max_heartbeat(conn)
    if heartbeat is not None and (now - heartbeat) > cfg.gateway_stale_seconds:
        result.causes.append("gateway_stale")
        result.notes.append(f"gateway heartbeat is {now - heartbeat:.0f}s old")

    # A bloated store and a wedged checkpoint lock are the two local conditions behind a slow or
    # stalled turn. Both are reported, never repaired: neither makes resuming impossible, so neither
    # blocks — but the resumed turn is told about them.
    size_mb = _db_size_mb(home)
    if size_mb is not None and size_mb > cfg.state_db_warn_mb:
        result.causes.append("database_bloat")
        result.notes.append(
            f"state.db is {size_mb:.0f} MB (> {cfg.state_db_warn_mb} MB); reads and checkpoints are slow"
        )
    lock = _stale_checkpoint_lock(home, now, cfg)
    if lock:
        result.causes.append("checkpoint_lock")
        result.notes.append(f"stale workspace checkpoint lock: {lock}")

    if not result.causes:
        result.notes.append("no background cause found")
    return result
