"""The value objects a triage pass moves between stages."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidate:
    """One session that looks abandoned, and the signal that flagged it."""

    session_id: str
    session_key: str | None
    source: str  # "lease" | "admission" | "reaped" | "hook"
    reason: str
    signal_ts: float
    holder_pid: int | None = None

    def key(self) -> str:
        return f"{self.session_id}::{self.source}::{self.reason}"


@dataclass
class Verdict:
    """Whether the interruption was involuntary (resumable) or a deliberate stop (never)."""

    involuntary: bool
    why: str


@dataclass
class Diagnosis:
    """The background causes behind an interruption, split by whether they block a resume."""

    causes: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Decision:
    """What the engine will do with a candidate, and why."""

    action: str  # "resume" | "defer" | "skip" | "report" | "error"
    reason: str
    resumed: bool = False
