# Changelog

## [Unreleased]

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
