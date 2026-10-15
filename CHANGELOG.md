# Changelog

## [Unreleased]

## [0.1.1]

### Added

- Stall detection in two classes: **post-fence** (an admission fenced `unknown` with a queued
  successor — the visible stall) and **pre-fence** (a live `started` turn with no logged progress past
  `stall_seconds` — the invisible one, which the harness' turn watchdog does not cover).
- Log-based attribution (`triage/logs.py`): provider (`waiting for non-streaming API response`),
  tool (the checkpoint `git` wedge / a tool left open after an API call), infrastructure (a planned
  gateway stop / `SIGTERM`), or unknown.
- Post-fence recovery via the harness' own acknowledgement (`hermes sessions discard <id> --yes`),
  verified by reading the admission back to `terminal` and the successor to `started`. Pre-fence
  stalls are reported, never interrupted.
- A planned gateway restart is classified as **infrastructure, not a user stop**, so its turn stays
  resumable — checked before the deliberate-close branch.
- `stalls` and `recover <session_id>` tool / `/triage` actions.

## [0.1.0]

### Added

- Abandonment detection from `session_turn_leases` (expired lease, dead holder PID),
  `session_admissions` (unsettled claim) and reaped sessions (`startup_orphan_reap`,
  `ws_orphan_reap`), unioned and de-duplicated.
- The manual-stop discriminator: a recorded `/stop` (via the `agent_loop_stopped` hook), a deliberate
  close, a steered turn, or a newer user message all refuse a resume.
- Background-cause diagnosis (disk, credentials, workspace, context overload, gateway liveness) with
  blocking/non-blocking classification.
- Safe auto-heal for a full disk (regenerable caches only) and report-only handling of every other
  cause.
- Gated resume: per-abruption fence, per-session and global budget, optional pause file, and an
  in-process guard that never injects from a CLI REPL.
- The `session_triage` tool and `/triage` command.
