"""Resolved settings for a triage pass.

Values come from the plugin's own ``plugins.entries.session-triage.settings`` block (via
``ctx.get_config``). ``SESSION_TRIAGE_*`` environment variables override them, which is how the
tests and a one-off headless run pin behaviour without touching the profile config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping

_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "n"}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str) -> str:
    return default if value is None else str(value)


@dataclass(frozen=True)
class Config:
    # Master switch and the two autonomous actions.
    enabled: bool = True
    auto_resume: bool = True
    auto_heal: bool = True
    dry_run: bool = False

    # Cadence and candidate windows.
    interval_seconds: int = 60
    startup_grace_seconds: int = 20
    min_age_seconds: int = 45
    max_age_seconds: int = 6 * 3600
    lease_grace_seconds: int = 15

    # Resume budget and de-duplication.
    max_resumes_per_session: int = 2
    max_resumes_total: int = 20
    resume_window_seconds: int = 3600
    fence_ttl_seconds: int = 900

    # Diagnosis thresholds.
    disk_free_floor_mb: int = 1024
    gateway_stale_seconds: int = 180

    # Stall detection: a live turn with no logged progress for this long is a stall.
    stall_seconds: int = 300
    auto_recover_fences: bool = True

    # Optional operator kill-switch: a file path whose existence pauses all resumes.
    pause_file: str = ""

    @classmethod
    def resolve(
        cls,
        get: Callable[[str, Any], Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "Config":
        """Build from ``get(key, default)`` (ctx.get_config) with env overrides on top."""
        env = env if env is not None else os.environ
        get = get if get is not None else (lambda _key, default: default)

        def raw(key: str, default: Any) -> Any:
            override = env.get("SESSION_TRIAGE_" + key.upper())
            return get(key, default) if override is None else override

        return cls(
            enabled=_as_bool(raw("enabled", True), True),
            auto_resume=_as_bool(raw("auto_resume", True), True),
            auto_heal=_as_bool(raw("auto_heal", True), True),
            dry_run=_as_bool(raw("dry_run", False), False),
            interval_seconds=_as_int(raw("interval_seconds", 60), 60),
            startup_grace_seconds=_as_int(raw("startup_grace_seconds", 20), 20),
            min_age_seconds=_as_int(raw("min_age_seconds", 45), 45),
            max_age_seconds=_as_int(raw("max_age_seconds", 6 * 3600), 6 * 3600),
            lease_grace_seconds=_as_int(raw("lease_grace_seconds", 15), 15),
            max_resumes_per_session=_as_int(raw("max_resumes_per_session", 2), 2),
            max_resumes_total=_as_int(raw("max_resumes_total", 20), 20),
            resume_window_seconds=_as_int(raw("resume_window_seconds", 3600), 3600),
            fence_ttl_seconds=_as_int(raw("fence_ttl_seconds", 900), 900),
            disk_free_floor_mb=_as_int(raw("disk_free_floor_mb", 1024), 1024),
            gateway_stale_seconds=_as_int(raw("gateway_stale_seconds", 180), 180),
            stall_seconds=_as_int(raw("stall_seconds", 300), 300),
            auto_recover_fences=_as_bool(raw("auto_recover_fences", True), True),
            pause_file=_as_str(raw("pause_file", ""), ""),
        )
