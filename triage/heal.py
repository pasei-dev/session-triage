"""Repair the background causes that are safe to repair without asking.

Only two classes are auto-repaired, and both are bounded and reversible in effect: a **full disk**,
reclaimed by deleting regenerable caches only, and nothing else. Every other cause is reported and
left as a blocker so the engine defers instead of resuming into the same failure — a wrong automatic
repair is worse than a deferred resume, and the operator can act on the report.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .diagnose import _free_mb
from .model import Diagnosis

#: Regenerable directories a full-disk reclaim may delete, most expendable first. Relative to the
#: Hermes home. Nothing here is a recovery source: ``state.db``, ``sessions/`` and ``plugin-data/``
#: are deliberately absent.
_RECLAIM_TARGETS = ("cache/spillover", "cache/scratch", "cache/web", "cache/image_cache")


@dataclass
class HealResult:
    fixed: list[str]
    blocking: list[str]
    freed_bytes: int = 0


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _reclaim(home: Path, *, enabled: bool) -> int:
    freed = 0
    for rel in _RECLAIM_TARGETS:
        target = home / rel
        if not target.exists():
            continue
        size = _dir_size(target)
        if enabled:
            shutil.rmtree(target, ignore_errors=True)
            freed += size
        else:
            freed += 0
    return freed


def heal(
    diagnosis: Diagnosis,
    hermes_home: str | os.PathLike[str],
    cfg: Config,
) -> HealResult:
    home = Path(hermes_home)
    blocking = list(diagnosis.blocking)
    fixed = list(diagnosis.fixed)
    freed = 0

    if "disk_full" in blocking:
        if not cfg.auto_heal:
            diagnosis.notes.append("disk_full: auto_heal is off; not reclaimed")
        else:
            freed = _reclaim(home, enabled=not cfg.dry_run)
            after = _free_mb(home)
            if after is not None and after >= cfg.disk_free_floor_mb:
                blocking.remove("disk_full")
                fixed.append("disk_full")
                diagnosis.notes.append(f"reclaimed {freed // (1024 * 1024)} MB; disk now {after:.0f} MB free")
            else:
                diagnosis.notes.append(
                    f"reclaimed {freed // (1024 * 1024)} MB; disk still below floor"
                )

    return HealResult(fixed=fixed, blocking=blocking, freed_bytes=freed)
