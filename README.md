# session-triage

A Hermes plugin that watches for **abandoned sessions** — a turn that died with its process, a
provider that cut a response, a session the host reaped while it was still open — and closes the loop:
detect it, work out *why*, repair the background cause, and resume the turn **once**. It never resumes
a session you stopped or closed on purpose.

## What it does

```
detect ──▶ classify ──▶ diagnose ──▶ heal ──▶ gate ──▶ resume ──▶ verify
   │          │            │           │        │         │
 leases    manual?     disk/cred/    reclaim   budget    inject a
 admis-    /stop?      workspace/    caches     fence     user-role
 sions     closed?     compression  (disk    per-sess.   turn via
 reaped    steered?                  only)    + pause     ctx.inject_message
```

- **Detection** reads the durable signals a crash leaves: an expired `session_turn_leases` row whose
  holder PID is gone, a `session_admissions` target whose newest claim never settled, and a session
  reaped mid-life. The live `on_session_end` hook is the fast signal; the sweep is the backstop.
- **Classification** is the manual-stop discriminator. It refuses to touch a session with a recorded
  `/stop`, a deliberate close (`agent_close` / `cli_close` / `tui_close` / `tui_shutdown`), a steered
  turn, or a newer user message.
- **Diagnosis** probes disk, credentials, workspace, context overload and gateway liveness. A cause is
  *blocking* when resuming under it would reproduce the same interruption.
- **Heal** auto-repairs exactly one class: a full disk, by deleting regenerable caches only. Everything
  else is reported and left as a blocker.
- **Resume** is a plain user-role turn injected into the session (the same mechanism `/heartbeat`
  uses), so message alternation and prompt caching hold. It is fenced per abruption and budgeted per
  session and globally.

## Install

```bash
hermes plugins install <owner>/session-triage --ref <40-char sha>
hermes plugins enable session-triage
```

Injection into a gateway/desktop session needs the host's consent gate:

```bash
hermes config set plugins.entries.session-triage.allow_gateway_injection true
```

## Settings

All under `plugins.entries.session-triage.settings`, overridable at runtime with `SESSION_TRIAGE_*`
environment variables.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch for the background sweep |
| `auto_resume` | `true` | Whether the sweep may inject a resume turn |
| `auto_heal` | `true` | Whether it may reclaim disk |
| `dry_run` | `false` | Detect and report, never inject |
| `interval_seconds` | `60` | Sweep cadence |
| `min_age_seconds` | `45` | Ignore interruptions younger than this (a live turn may still be settling) |
| `max_age_seconds` | `21600` | Ignore interruptions older than this |
| `max_resumes_per_session` | `2` | Resume budget per session inside the window |
| `max_resumes_total` | `20` | Global resume budget inside the window |
| `resume_window_seconds` | `3600` | Budget window |
| `fence_ttl_seconds` | `900` | Do not re-resume the same abruption inside this window |
| `disk_free_floor_mb` | `1024` | Below this, the disk is a blocking cause |
| `stall_seconds` | `300` | A live turn with no logged progress for this long is a stall |
| `auto_recover_fences` | `true` | Acknowledge a fenced admission so its queued successor runs |
| `pause_file` | `""` | If set and the file exists, all resumes pause |

## Stalls

Two classes, both read from `state.db` + the log tail:

- **post-fence** — an admission is fenced `unknown` (a process died mid-turn) and a *queued successor*
  waits behind it. This is the visible stall: nothing runs until the fence is acknowledged. The
  plugin recovers it with the harness' own `hermes sessions discard <id> --yes`, then reads the store
  back to prove `unknown` → terminal and the successor `started`.
- **pre-fence** — a live `started` turn with no logged progress past `stall_seconds`. The harness'
  turn watchdog covers a provider wait but not a silent *tool* call, so the bound is the plugin's.
  This is reported with its attribution, never interrupted.

Attribution comes from the log tail: **provider** (`waiting for non-streaming API response`),
**tool** (the workspace checkpoint's `git` wedge, or a tool left open after an API call),
**infrastructure** (a planned gateway stop / `SIGTERM`). A planned restart is infrastructure, **not** a
user stop — the turn stays resumable.

## The tool and the command

`session_triage` (and `/triage`) expose the engine for a manual pass:

- `status` — config and whether a live injector is present.
- `scan` — a dry-run pass: what would be resumed, and why.
- `ledger` — the recent decision log.
- `diagnose <session_id>` — the background causes for one session.
- `resume <session_id>` — force a gated resume of one session.
- `stalls` — every stalled session, its class and attribution.
- `recover <session_id>` — acknowledge a fenced admission so its successor can run.

## Where it can resume, and why

`ctx.inject_message` reaches a live CLI queue, a TUI/desktop injector, or the messaging-gateway
injector. Only the last two are published by a long-lived host, and in a CLI process the same call
would push the resume text onto *your own REPL* — so the worker refuses to run there and only sweeps
where a gateway or TUI host is present.
