"""One triage pass: detect, classify, diagnose, heal, gate, resume — and detect and recover stalls.

The pass is idempotent by construction: the ledger fences a resume per abruption, the resume budget
bounds repetition, and a failure to inject is a defer rather than an error. It never raises; a stage
that fails is logged and recorded, and the sweep moves to the next item.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from . import classify, db, detect, diagnose, heal, logs, resume, stall, store
from .config import Config

Inject = Callable[..., bool]
Log = Callable[[str], None]


@dataclass
class PassResult:
    considered: int = 0
    resumed: int = 0
    skipped: int = 0
    deferred: int = 0
    healed: int = 0
    stalls: int = 0
    recovered: int = 0
    resume_verified: int = 0
    resume_failed: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _record(ledger: str, entry: dict[str, Any]) -> None:
    try:
        store.append(ledger, entry)
    except Exception:
        pass


def _verify_resumes(conn, ledger, now, cfg, result, log) -> None:
    """Ask whether the resumes already issued actually produced a reply.

    An injection the host accepted is not a recovery — the session can die again the moment it
    wakes. Each resume is settled here exactly once: **verified** when a completed assistant reply
    exists after it, **failed** when the grace has passed with none. That is what stops the
    ``resumed`` count from being read as ``fixed``.
    """
    try:
        pending = store.unverified_resumes(ledger, since=now - cfg.resume_window_seconds)
    except Exception:
        return
    for record in pending:
        session_id = record.get("session_id")
        ts = float(record.get("ts") or 0.0)
        if not session_id:
            continue
        try:
            replied = db.last_success_ts(conn, session_id) > ts
        except Exception:
            continue
        try:
            if replied:
                result.resume_verified += 1
                _record(ledger, {"kind": "resume_verified", "session_id": session_id,
                                 "resume_id": record["resume_id"], "took_s": round(now - ts, 1)})
            elif (now - ts) > cfg.resume_verify_seconds:
                result.resume_failed += 1
                _record(ledger, {"kind": "resume_failed", "session_id": session_id,
                                 "resume_id": record["resume_id"], "age_s": round(now - ts, 1)})
                log(f"session-triage: resume on {session_id} produced no reply in "
                    f"{cfg.resume_verify_seconds}s")
        except Exception:
            continue


def _resume_candidates(conn, ledger, now, cfg, events, inject, result, log, hermes_home) -> None:
    candidates = detect.find_candidates(conn, now, cfg, ledger)
    result.considered = len(candidates)

    for candidate in candidates:
        try:
            verdict = classify.classify(conn, ledger, candidate, now, cfg, events)
        except Exception as exc:  # never let one candidate end the sweep
            log(f"session-triage: classify failed for {candidate.session_id}: {exc}")
            result.actions.append({"session_id": candidate.session_id, "action": "error",
                                   "why": f"classify: {exc}"})
            continue

        if not verdict.involuntary:
            result.skipped += 1
            _record(ledger, {"kind": "skip", "session_id": candidate.session_id,
                             "source": candidate.source, "reason": candidate.reason,
                             "why": verdict.why})
            result.actions.append({"session_id": candidate.session_id, "action": "skip",
                                   "why": verdict.why})
            continue

        try:
            diagnosis = diagnose.diagnose(conn, hermes_home, candidate.session_id, now, cfg)
            healing = heal.heal(diagnosis, hermes_home, cfg)
        except Exception as exc:
            log(f"session-triage: diagnose failed for {candidate.session_id}: {exc}")
            result.actions.append({"session_id": candidate.session_id, "action": "error",
                                   "why": f"diagnose: {exc}"})
            continue

        diagnosis.fixed = healing.fixed
        diagnosis.blocking = healing.blocking
        if healing.freed_bytes:
            result.healed += 1
            _record(ledger, {"kind": "heal", "session_id": candidate.session_id,
                             "freed_bytes": healing.freed_bytes, "fixed": healing.fixed})

        allowed, why = resume.gate(conn, ledger, candidate, diagnosis, now=now, cfg=cfg)
        if not allowed:
            result.deferred += 1
            _record(ledger, {"kind": "defer", "session_id": candidate.session_id,
                             "source": candidate.source, "reason": candidate.reason, "why": why})
            result.actions.append({"session_id": candidate.session_id, "action": "defer", "why": why})
            continue

        if cfg.dry_run:
            _record(ledger, {"kind": "report", "session_id": candidate.session_id,
                             "source": candidate.source, "reason": candidate.reason,
                             "why": "dry_run"})
            result.actions.append({"session_id": candidate.session_id, "action": "report",
                                   "why": "dry_run (no injection)"})
            continue

        prompt = resume.build_prompt(candidate, diagnosis)
        injected = resume.do_resume(inject, prompt, candidate.session_key or "")
        if injected:
            result.resumed += 1
            _record(ledger, {"kind": "resume", "session_id": candidate.session_id,
                             "session_key": candidate.session_key, "source": candidate.source,
                             "reason": candidate.reason, "ok": True})
            result.actions.append({"session_id": candidate.session_id, "action": "resume", "why": why})
            log(f"session-triage: resumed {candidate.session_id} ({candidate.reason})")
        else:
            result.deferred += 1
            _record(ledger, {"kind": "defer", "session_id": candidate.session_id,
                             "source": candidate.source, "reason": candidate.reason,
                             "why": "injector refused or unavailable"})
            result.actions.append({"session_id": candidate.session_id, "action": "defer",
                                   "why": "injector refused or unavailable"})


def _stalls(conn, ledger, now, cfg, events, result, log) -> None:
    found = stall.find_stalls(conn, events, now, cfg)
    result.stalls = len(found)
    for item in found:
        entry: dict[str, Any] = {
            "session_id": item.session_id, "kind": item.kind, "attribution": item.attribution,
            "detail": item.detail, "evidence": item.evidence, "successor": item.has_queued_successor,
        }
        if item.kind == "post_fence" and item.has_queued_successor \
                and cfg.auto_recover_fences and not cfg.dry_run:
            ok, output = stall.recover_fence(item.session_id)
            resolved = ok and stall.fence_resolved(conn, item.session_id)
            entry.update({"action": "recover" if resolved else "recover_failed",
                          "recovered": resolved, "output": output})
            if resolved:
                result.recovered += 1
                _record(ledger, {"kind": "recover_fence", **entry})
                log(f"session-triage: recovered fence on {item.session_id}")
            else:
                _record(ledger, {"kind": "recover_failed", **entry})
        else:
            # A pre-fence stall is a LIVE turn: attribute and report, never interrupt.
            entry["action"] = "report"
            if item.attribution == "tool":
                # The write path shares one file-ops environment and checkpoint store per profile,
                # so a wedged write is not one session's problem.
                entry["blast_radius"] = (
                    "profile-scoped: one file-ops environment and one checkpoint store are shared "
                    "by every session in the profile, so a wedged write blocks them all"
                )
            if not store.recently_recorded(ledger, "stall", item.session_id,
                                           cfg.fence_ttl_seconds, now):
                _record(ledger, {"kind": "stall", **entry})
        result.actions.append(entry)


def run_pass(
    conn,
    *,
    hermes_home: str,
    ledger: str,
    inject: Inject,
    cfg: Config,
    now: Optional[float] = None,
    log: Optional[Log] = None,
) -> PassResult:
    now = time.time() if now is None else float(now)
    log = log or (lambda _msg: None)
    result = PassResult()

    try:
        events = logs.iter_events(logs.log_paths(hermes_home))
    except Exception:
        events = []

    _verify_resumes(conn, ledger, now, cfg, result, log)
    _resume_candidates(conn, ledger, now, cfg, events, inject, result, log, hermes_home)
    _stalls(conn, ledger, now, cfg, events, result, log)
    return result
