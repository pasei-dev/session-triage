"""One triage pass: detect, classify, diagnose, heal, gate, resume — and record every decision.

The pass is idempotent by construction: the ledger fences a resume per abruption, the resume budget
bounds repetition, and a failure to inject is a defer rather than an error. It never raises; a stage
that fails is logged and recorded, and the sweep moves to the next candidate.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from . import classify, detect, diagnose, heal, resume, store
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
    actions: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _record(ledger: str, result: PassResult, entry: dict[str, Any]) -> None:
    try:
        store.append(ledger, entry)
    except Exception:
        pass


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

    candidates = detect.find_candidates(conn, now, cfg, ledger)
    result.considered = len(candidates)

    for candidate in candidates:
        try:
            verdict = classify.classify(conn, ledger, candidate, now, cfg)
        except Exception as exc:  # never let one candidate end the sweep
            log(f"session-triage: classify failed for {candidate.session_id}: {exc}")
            result.actions.append({"session_id": candidate.session_id, "action": "error",
                                   "why": f"classify: {exc}"})
            continue

        if not verdict.involuntary:
            result.skipped += 1
            _record(ledger, result, {"kind": "skip", "session_id": candidate.session_id,
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
            _record(ledger, result, {"kind": "heal", "session_id": candidate.session_id,
                                     "freed_bytes": healing.freed_bytes, "fixed": healing.fixed})

        allowed, why = resume.gate(conn, ledger, candidate, diagnosis, now=now, cfg=cfg)
        if not allowed:
            result.deferred += 1
            _record(ledger, result, {"kind": "defer", "session_id": candidate.session_id,
                                     "source": candidate.source, "reason": candidate.reason,
                                     "why": why})
            result.actions.append({"session_id": candidate.session_id, "action": "defer", "why": why})
            continue

        if cfg.dry_run:
            _record(ledger, result, {"kind": "report", "session_id": candidate.session_id,
                                     "source": candidate.source, "reason": candidate.reason,
                                     "why": "dry_run"})
            result.actions.append({"session_id": candidate.session_id, "action": "report",
                                   "why": "dry_run (no injection)"})
            continue

        prompt = resume.build_prompt(candidate, diagnosis)
        injected = resume.do_resume(inject, prompt, candidate.session_key or "")
        if injected:
            result.resumed += 1
            _record(ledger, result, {"kind": "resume", "session_id": candidate.session_id,
                                     "session_key": candidate.session_key,
                                     "source": candidate.source, "reason": candidate.reason,
                                     "ok": True})
            result.actions.append({"session_id": candidate.session_id, "action": "resume",
                                   "why": why})
            log(f"session-triage: resumed {candidate.session_id} ({candidate.reason})")
        else:
            result.deferred += 1
            _record(ledger, result, {"kind": "defer", "session_id": candidate.session_id,
                                     "source": candidate.source, "reason": candidate.reason,
                                     "why": "injector refused or unavailable"})
            result.actions.append({"session_id": candidate.session_id, "action": "defer",
                                   "why": "injector refused or unavailable"})

    return result
