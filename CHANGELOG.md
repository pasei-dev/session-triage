# Changelog

## [Unreleased]

## [0.1.2]

### Fixed

- **`session_row` blanked every row when the schema differed.** It selected `compression_failure_streak`,
  a column that does not exist (the real one is `compression_fallback_streak`), so the query raised and
  returned `None` for *every* session — silently disabling the lease, admission and ledger (hook)
  detection sources, and making the resume gate refuse every candidate for want of a `session_key`. It now
  reads only the columns the live schema actually has (`pragma table_info` intersection), so a renamed or
  absent column degrades to a missing field instead of a dead sweep.
- The test fixture created the same nonexistent column, so the suite passed against a schema that never
  existed. The fixture now mirrors the real column names, and `tests/test_db_schema.py` asserts the fixture
  carries every column `db.SESSION_COLUMNS` reads and that the reader survives a schema that lacks one.

### Added

- The worker heartbeat (`worker.json`, read by the `status` action) now carries the last pass's counts
  (`considered`, `resumed`, `deferred`, `stalls`, …), a `passes` tally and `last_error`, so an inert sweep
  is visible instead of looking identical to a healthy one.
- Diagnosis now reports two local stall conditions: a bloated `state.db` (`state_db_warn_mb`) and a stale
  workspace checkpoint lock (`checkpoint_lock_stale_seconds`) — the wedged `git add -A` that stretches a
  file write from seconds to minutes. Both are reported, never repaired.
- Stall detection also reports a session the host paused (`session <id> paused: <reason>`, e.g.
  `admission_conflict` / `runtime_draining`), which never reaches an admission row.
- The ledger is bounded: it rotates to `ledger.jsonl.1` past `SESSION_TRIAGE_LEDGER_MAX_BYTES`
  (default 1 MB), keeping the previous rotation so a recent stop and a resume fence survive it.

### Changed

- The resume turn is a clearer in-session note: `[session-triage] Automated note: this session was
  auto-resumed by the session-triage plugin …`, so the transcript shows plainly that a resume happened.
  The `[session-triage]` sentinel also makes `max_user_ts` ignore the plugin's own note, so it is never
  mistaken for a newer user turn.

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
