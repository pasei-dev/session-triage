"""Read the agent log and attribute *why* a turn stopped making progress.

Grounded in the real line shape::

    2026-10-15 20:54:12,874 INFO [<session>] agent.tool_executor: tool write_file completed (1.77s, 279 chars)

Rotated files (``agent.log.1..N``, ``errors.log*``) carry the older lines, so a scan reads the tail of
each. A tool call logs only its **completion** — there is no start line — so a stall is inferred from
the last event plus the markers the provider-wait and checkpoint paths emit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: Files scanned, in the profile's ``logs/`` directory.
_LOG_GLOBS = ("agent.log", "agent.log.[0-9]*", "errors.log", "errors.log.[0-9]*",
              "gateway-restart.log", "tui_gateway_crash.log")

#: Bytes read from the tail of each file — a stall is recent, so the tail is the whole story.
_TAIL_BYTES = 2_000_000

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\s+(?P<level>\w+)\s+"
    r"(?:\[(?P<sid>[^\]]+)\]\s+)?(?P<logger>[^:]+):\s(?P<msg>.*)$"
)

#: A provider that is not answering (the harness' own wait marker, and the retry line).
PROVIDER_MARKERS = ("waiting for non-streaming API response", "Retrying API call")
#: Infrastructure took the process down — a planned restart or an app/OS kill, never a user stop.
INFRA_MARKERS = ("planned gateway stop", "SIGTERM", "draining in-flight runs",
                 "Shutdown notification", "Stopping gateway")
#: The workspace checkpoint git store wedged — the file-write path's own signature.
TOOL_MARKERS = ("Git command failed", "Unable to create")


@dataclass
class Event:
    ts: float
    level: str
    session_id: str | None
    logger: str
    msg: str


@dataclass
class Attribution:
    kind: str  # "provider" | "tool" | "infrastructure" | "unknown"
    evidence: list[str] = field(default_factory=list)


def log_paths(hermes_home: str | Path) -> list[Path]:
    logs = Path(hermes_home) / "logs"
    if not logs.is_dir():
        return []
    found: list[Path] = []
    for pattern in _LOG_GLOBS:
        found.extend(sorted(logs.glob(pattern)))
    # De-dupe, newest first (agent.log before agent.log.1), so a tail read sees fresh lines first.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved not in seen and path.is_file():
            seen.add(resolved)
            unique.append(path)
    return unique


def _parse_ts(text: str) -> float:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def iter_events(paths: list[Path], max_bytes: int = _TAIL_BYTES) -> list[Event]:
    """Every parseable log line from the tail of each file, oldest first."""
    events: list[Event] = []
    for path in paths:
        try:
            size = path.stat().st_size
            with open(path, "rb") as handle:
                if size > max_bytes:
                    handle.seek(size - max_bytes)
                    handle.readline()  # drop the partial first line
                data = handle.read()
        except OSError:
            continue
        for raw in data.decode("utf-8", "replace").splitlines():
            match = _LINE_RE.match(raw)
            if not match:
                continue
            events.append(Event(
                ts=_parse_ts(match.group("ts")),
                level=match.group("level"),
                session_id=(match.group("sid") or None),
                logger=match.group("logger").strip(),
                msg=match.group("msg"),
            ))
    events.sort(key=lambda event: event.ts)
    return events


def session_events(events: list[Event], session_id: str) -> list[Event]:
    return [event for event in events if event.session_id == session_id]


def last_event_ts(events: list[Event], session_id: str) -> float:
    latest = 0.0
    for event in events:
        if event.session_id == session_id and event.ts > latest:
            latest = event.ts
    return latest


def attribute(events: list[Event], session_id: str, since: float) -> Attribution:
    """Why the session stopped progressing, from the log tail.

    Precedence: an infrastructure kill explains the whole turn, so it wins; then the explicit
    provider-wait marker; then the checkpoint/tool wedge or a tool left open after an API call.
    """
    infra = [event for event in events
             if event.ts >= since and any(marker in event.msg for marker in INFRA_MARKERS)]
    if infra:
        return Attribution("infrastructure", [f"{infra[-1].logger}: {infra[-1].msg[:120]}"])

    tail = [event for event in events if event.session_id == session_id and event.ts >= since]
    if not tail:
        return Attribution("unknown", ["no log lines for the session in the window"])

    provider = [event for event in tail if any(m in event.msg for m in PROVIDER_MARKERS)]
    if provider:
        return Attribution("provider", [f"{provider[-1].logger}: {provider[-1].msg[:120]}"])

    tool = [event for event in tail if any(m in event.msg for m in TOOL_MARKERS)]
    if tool:
        return Attribution("tool", [f"{tool[-1].logger}: {tool[-1].msg[:120]}"])

    last = tail[-1]
    if "API call #" in last.msg and "Turn ended" not in last.msg:
        return Attribution("tool", [f"last event is '{last.msg[:100]}' with no tool completion after it"])
    return Attribution("unknown", [f"last event: {last.logger}: {last.msg[:100]}"])


def planned_restart(events: list[Event], since: float) -> bool:
    """True when a planned gateway restart / shutdown is evidenced after ``since``."""
    return any(event.ts >= since and any(marker in event.msg for marker in INFRA_MARKERS)
               for event in events)
