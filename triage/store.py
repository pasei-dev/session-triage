"""The append-only triage ledger: the durable record of stops, interruptions, decisions and resumes.

It carries three pieces of state the engine needs and neither ``state.db`` nor an in-memory flag can
give across processes and restarts:

* **stop markers** — a ``/stop`` seen live, so the sweep can tell a deliberate stop from a crash even
  after the process that saw it is gone;
* **the resume budget** — how many resumes a session has had inside the window, counted across every
  process that shares the profile;
* **the resume fence** — a resume already issued for one abruption, so a second sweep never repeats it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

try:  # POSIX cross-process lock; a Windows host degrades to the in-process lock only.
    import fcntl
except ImportError:  # pragma: no cover - windows
    fcntl = None  # type: ignore[assignment]

_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()

LEDGER_NAME = "ledger.jsonl"

#: The ledger is bounded: once it grows past this many bytes it rotates to ``<name>.1`` and a fresh
#: file starts. The previous rotation is kept (a recent stop and a resume fence survive it) and any
#: older one is dropped. ``SESSION_TRIAGE_LEDGER_MAX_BYTES=0`` disables rotation.
_MAX_BYTES_DEFAULT = 1_000_000
_ROTATED_SUFFIX = ".1"


def ledger_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / LEDGER_NAME


def _max_bytes() -> int:
    raw = os.environ.get("SESSION_TRIAGE_LEDGER_MAX_BYTES")
    try:
        value = int(raw) if raw is not None else _MAX_BYTES_DEFAULT
    except (TypeError, ValueError):
        value = _MAX_BYTES_DEFAULT
    return max(0, value)


def _rotated_path(path: Path) -> Path:
    return path.with_name(path.name + _ROTATED_SUFFIX)


def _rotate_if_needed(path: Path) -> None:
    """Rotate the ledger to ``.1`` once it is over the bound. ``os.replace`` is atomic."""
    limit = _max_bytes()
    if limit <= 0:
        return
    try:
        if path.exists() and path.stat().st_size > limit:
            os.replace(path, _rotated_path(path))
    except OSError:
        pass


def append(path: str | os.PathLike[str], record: dict[str, Any]) -> None:
    """Append one JSON record. A single ``write`` of one short line is atomic on POSIX."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    record = dict(record)
    record.setdefault("ts", time.time())
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


def read_all(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every record, oldest first: the rotated file (``.1``) then the current ledger."""
    path = Path(path)
    records: list[dict[str, Any]] = []
    for source in (_rotated_path(path), path):
        if not source.exists():
            continue
        try:
            with open(source, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        records.append(item)
        except OSError:
            continue
    return records


def _recent(records: Iterable[dict[str, Any]], kinds: set[str], session_id: str | None,
            since: float) -> list[dict[str, Any]]:
    out = []
    for rec in records:
        if rec.get("kind") not in kinds:
            continue
        if rec.get("ts", 0) < since:
            continue
        if session_id is not None and rec.get("session_id") != session_id:
            continue
        out.append(rec)
    return out


def stopped_any(path, ids: Iterable[str], since: float) -> bool:
    """True when a deliberate stop/close was recorded for any of ``ids`` at or after ``since``.

    ``agent_loop_stopped`` carries the durable ``session_key`` while a candidate is named by
    ``session_id``; either may be what the stop was recorded under, so both are matched.
    """
    wanted = {i for i in ids if i}
    if not wanted:
        return False
    for rec in read_all(path):
        if rec.get("kind") != "stop" or rec.get("ts", 0) < since:
            continue
        if rec.get("session_id") in wanted or rec.get("session_key") in wanted:
            return True
    return False


def stopped_since(path, session_id: str, since: float) -> bool:
    """True when a deliberate stop/close was recorded for the session at or after ``since``."""
    return stopped_any(path, (session_id,), since)


def resume_count(path, *, since: float, session_id: str | None = None) -> int:
    return len(_recent(read_all(path), {"resume"}, session_id, since))


def recently_recorded(path, kind: str, session_id: str, ttl: float, now: float) -> bool:
    """True when a record of ``kind`` for the session is newer than ``ttl`` — a report de-duplicator."""
    return bool(_recent(read_all(path), {kind}, session_id, now - ttl))


def fenced(path, session_id: str, source: str, reason: str, *, ttl: float, now: float) -> bool:
    """True when this exact abruption was already resumed inside the fence TTL."""
    since = now - ttl
    for rec in _recent(read_all(path), {"resume"}, session_id, since):
        if rec.get("source") == source and rec.get("reason") == reason:
            return True
    return False


@contextmanager
def pass_lock(data_dir: str | os.PathLike[str]):
    """Serialize a triage pass across threads and processes sharing the profile."""
    lock_path = Path(data_dir) / "triage.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path.resolve())
    with _PROCESS_LOCKS_GUARD:
        thread_lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        handle = open(lock_path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
