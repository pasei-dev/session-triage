"""session-triage — the Hermes plugin surface.

Registers the two lifecycle hooks that witness an interruption (``on_session_end``) and a deliberate
stop (``agent_loop_stopped``), a ``session_triage`` tool and a ``/triage`` command for manual passes,
and a background worker that runs the sweep **inside the gateway process** — the only place
``ctx.inject_message`` can reach a live session. Nothing here raises into session start: every hook
and the worker are wrapped, and a failure degrades to a logged no-op.

Why the gateway process: ``inject_message`` routes to a live CLI queue, a TUI/desktop injector, or the
messaging-gateway injector, and only the last two are published by a long-lived host. In a CLI process
the same call would push the resume text onto *the operator's own REPL*, so the worker refuses to run
there (see ``_can_inject``).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .triage import classify, db, detect, diagnose, engine, heal, logs, resume, stall, store
from .triage.config import Config

logger = logging.getLogger("plugin.session-triage")

TOOL = "session_triage"
TOOLSET = "session-triage"

_TOOL_SCHEMA = {
    "name": TOOL,
    "description": (
        "Inspect and drive abandoned-session recovery. `status` reports config and whether a live "
        "injector is present; `scan` runs a dry-run pass (what would be resumed, and why); `ledger` "
        "returns recent decisions; `diagnose <session_id>` probes the background causes; "
        "resume <session_id>` forces a gated resume; `stalls` lists stalled sessions with their "
        "attribution; `recover <session_id>` acknowledges a fenced admission so its queued successor "
        "can run. A session the user stopped or closed deliberately is never resumed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "scan", "ledger", "diagnose", "resume", "stalls", "recover"],
                "description": "The operation to perform.",
            },
            "session_id": {
                "type": "string",
                "description": "Target session id for `diagnose` and `resume`.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum ledger entries to return (default 40).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


# --------------------------------------------------------------------------- helpers

def _hermes_home(ctx) -> str:
    override = os.environ.get("SESSION_TRIAGE_HERMES_HOME")
    if override:
        return override
    manager = getattr(ctx, "_manager", None)
    home = getattr(manager, "home_path", None)
    if home:
        return str(home)
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _can_inject(ctx) -> bool:
    """Whether a live host can accept a named-session injection from this process.

    Deliberately reads the manager's injector slots (the only place that knows) and refuses in a CLI
    process, where ``inject_message`` would enqueue onto the operator's own REPL rather than the
    session named by ``session_key``.
    """
    manager = getattr(ctx, "_manager", None)
    if manager is None:
        return False
    if getattr(manager, "_cli_ref", None) is not None:
        return False
    return bool(
        getattr(manager, "has_gateway_message_injector", False)
        or getattr(manager, "has_tui_message_injector", False)
    )


def _injector(ctx):
    def inject(content: str, *, session_key: str | None = None) -> bool:
        try:
            return bool(ctx.inject_message(content, session_key=session_key))
        except Exception:
            logger.debug("session-triage: inject_message raised", exc_info=True)
            return False

    return inject


def _state_db(home: str) -> Path:
    return Path(os.environ.get("SESSION_TRIAGE_STATE_DB") or (Path(home) / "state.db"))


def _resolve_data_dir(ctx) -> Path:
    """Where the ledger lives. Never lets a data-dir failure stop the plugin from registering.

    ``ctx.state.data_dir`` is the profile-scoped plugin-data directory; the fallbacks exist so the
    capability probe (which runs ``register()`` against a bare context) and a stripped environment
    still wire the hooks and the tool.
    """
    candidates = []
    try:
        candidates.append(Path(ctx.state.data_dir))
    except Exception:
        pass
    home = _hermes_home(ctx)
    candidates.append(Path(home) / "plugin-data" / "session-triage")
    candidates.append(Path(tempfile.gettempdir()) / "session-triage")
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            continue
    return Path(tempfile.gettempdir())


# --------------------------------------------------------------------------- hooks

def _on_session_end(ledger: str, kwargs: dict) -> None:
    try:
        interrupted = bool(kwargs.get("interrupted") or kwargs.get("failed"))
        reason = kwargs.get("turn_exit_reason")
        if interrupted or (reason and "interrupt" in str(reason).lower()):
            store.append(ledger, {
                "kind": "interrupted",
                "session_id": kwargs.get("session_id"),
                "reason": reason,
                "platform": kwargs.get("platform"),
            })
    except Exception:
        logger.debug("session-triage: on_session_end failed", exc_info=True)


def _on_loop_stopped(ledger: str, kwargs: dict) -> None:
    try:
        store.append(ledger, {
            "kind": "stop",
            "session_key": kwargs.get("session_key"),
            "reason": kwargs.get("reason") or kwargs.get("invalidation_reason"),
            "platform": kwargs.get("platform"),
        })
    except Exception:
        logger.debug("session-triage: agent_loop_stopped failed", exc_info=True)


# --------------------------------------------------------------------------- passes

def _run_guarded(ctx, config: Config, ledger: str, state_db: Path, home: str, state_dir: Path,
                 *, force_dry_run: bool = False) -> dict:
    if not state_db.exists():
        return {"ok": False, "error": f"state.db not found at {state_db}"}
    with store.pass_lock(state_dir):
        conn = db.connect(state_db)
        try:
            effective = config
            if force_dry_run and not config.dry_run:
                from dataclasses import replace

                effective = replace(config, dry_run=True)
            result = engine.run_pass(
                conn,
                hermes_home=home,
                ledger=ledger,
                inject=_injector(ctx),
                cfg=effective,
                log=logger.info,
            )
            return result.as_dict()
        finally:
            conn.close()


def _targeted_resume(ctx, config: Config, ledger: str, state_db: Path, home: str,
                     state_dir: Path, session_id: str) -> dict:
    if not state_db.exists():
        return {"ok": False, "error": f"state.db not found at {state_db}"}
    with store.pass_lock(state_dir):
        conn = db.connect(state_db)
        try:
            now = time.time()
            candidates = detect.find_candidates(conn, now, config, ledger)
            target = next((c for c in candidates if c.session_id == session_id), None)
            if target is None:
                return {"ok": False, "error": "no abruption candidate for that session id",
                        "candidates": [c.session_id for c in candidates]}
            events = logs.iter_events(logs.log_paths(home))
            verdict = classify.classify(conn, ledger, target, now, config, events)
            if not verdict.involuntary:
                return {"ok": False, "action": "skip", "reason": verdict.why}
            diagnosis = diagnose.diagnose(conn, home, target.session_id, now, config)
            healing = heal.heal(diagnosis, home, config)
            diagnosis.fixed, diagnosis.blocking = healing.fixed, healing.blocking
            allowed, why = resume.gate(conn, ledger, target, diagnosis, now=now, cfg=config)
            if not allowed:
                return {"ok": False, "action": "defer", "reason": why}
            ok = resume.do_resume(_injector(ctx),
                                  resume.build_prompt(target, diagnosis),
                                  target.session_key or "")
            if ok:
                store.append(ledger, {"kind": "resume", "session_id": target.session_id,
                                      "session_key": target.session_key, "source": target.source,
                                      "reason": target.reason, "ok": True, "manual": True})
            return {"ok": ok, "action": "resume" if ok else "defer",
                    "reason": why if ok else "injector refused or unavailable",
                    "prompt": resume.build_prompt(target, diagnosis)}
        finally:
            conn.close()


def _status(ctx, config: Config, ledger: str, state_db: Path, state_dir: Path) -> dict:
    heartbeat = None
    try:
        heartbeat = json.loads((Path(state_dir) / "worker.json").read_text())
    except Exception:
        heartbeat = None
    return {
        "ok": True,
        "injector_available": _can_inject(ctx),
        "state_db": str(state_db),
        "state_db_exists": state_db.exists(),
        "ledger": ledger,
        "worker": heartbeat,
        "config": {k: v for k, v in asdict(config).items()},
    }


def _handle(ctx, cfg_get, ledger: str, state_db: Path, home: str, state_dir: Path,
            args: dict) -> dict:
    action = (args or {}).get("action") or "status"
    config = cfg_get()
    if action == "status":
        return _status(ctx, config, ledger, state_db, state_dir)
    if action == "scan":
        return _run_guarded(ctx, config, ledger, state_db, home, state_dir, force_dry_run=True)
    if action == "ledger":
        limit = int((args or {}).get("limit") or 40)
        records = store.read_all(ledger)[-limit:]
        return {"ok": True, "count": len(records), "entries": records}
    if action == "diagnose":
        session_id = (args or {}).get("session_id")
        if not session_id:
            return {"ok": False, "error": "session_id is required"}
        if not state_db.exists():
            return {"ok": False, "error": f"state.db not found at {state_db}"}
        conn = db.connect(state_db)
        try:
            diagnosis = diagnose.diagnose(conn, home, session_id, time.time(), config)
            return {"ok": True, "session_id": session_id, "causes": diagnosis.causes,
                    "blocking": diagnosis.blocking, "notes": diagnosis.notes}
        finally:
            conn.close()
    if action == "resume":
        session_id = (args or {}).get("session_id")
        if not session_id:
            return {"ok": False, "error": "session_id is required"}
        return _targeted_resume(ctx, config, ledger, state_db, home, state_dir, session_id)
    if action == "stalls":
        if not state_db.exists():
            return {"ok": False, "error": f"state.db not found at {state_db}"}
        events = logs.iter_events(logs.log_paths(home))
        conn = db.connect(state_db)
        try:
            found = stall.find_stalls(conn, events, time.time(), config)
            return {"ok": True, "count": len(found), "stalls": [
                {"session_id": item.session_id, "kind": item.kind,
                 "attribution": item.attribution, "detail": item.detail,
                 "evidence": item.evidence, "successor": item.has_queued_successor}
                for item in found]}
        finally:
            conn.close()
    if action == "recover":
        session_id = (args or {}).get("session_id")
        if not session_id:
            return {"ok": False, "error": "session_id is required"}
        ok, output = stall.recover_fence(session_id)
        resolved = ok
        if state_db.exists():
            conn = db.connect(state_db)
            try:
                resolved = ok and stall.fence_resolved(conn, session_id)
            finally:
                conn.close()
        return {"ok": resolved, "output": output,
                "reason": "" if resolved else "recovery did not clear the fence"}
    return {"ok": False, "error": f"unknown action: {action}"}


# --------------------------------------------------------------------------- worker

def _write_heartbeat(state_dir: Path, ctx, config: Config) -> None:
    """A worker liveness stamp the `status` action can read back — a silent thread is unverifiable."""
    try:
        (Path(state_dir) / "worker.json").write_text(json.dumps({
            "pid": os.getpid(),
            "ts": time.time(),
            "enabled": config.enabled,
            "injector": _can_inject(ctx),
        }))
    except Exception:
        pass


def _start_worker(ctx, cfg_get, ledger: str, state_db: Path, home: str, state_dir: Path,
                  stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        first_pass = True
        while not stop.is_set():
            try:
                config = cfg_get()
                _write_heartbeat(state_dir, ctx, config)
                if first_pass:
                    first_pass = False
                    if stop.wait(max(0, config.startup_grace_seconds)):
                        return
                if config.enabled and _can_inject(ctx):
                    try:
                        _run_guarded(ctx, config, ledger, state_db, home, state_dir)
                    except Exception:
                        logger.debug("session-triage: worker pass failed", exc_info=True)
                interval = max(5, config.interval_seconds)
            except Exception:
                logger.debug("session-triage: worker iteration failed", exc_info=True)
                interval = 60
            stop.wait(interval)

    thread = threading.Thread(target=loop, name="session-triage", daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------- register

def register(ctx) -> None:
    """Wire the plugin into the running host. Never raises, never bails before wiring."""
    state_dir = _resolve_data_dir(ctx)
    ledger = str(state_dir / store.LEDGER_NAME)
    home = _hermes_home(ctx)
    state_db = _state_db(home)

    def cfg_get() -> Config:
        try:
            return Config.resolve(lambda key, default: ctx.get_config(key, default))
        except Exception:
            logger.debug("session-triage: config resolve failed; using defaults", exc_info=True)
            return Config()

    try:
        ctx.register_hook("on_session_end", lambda **kw: _on_session_end(ledger, kw))
        ctx.register_hook("agent_loop_stopped", lambda **kw: _on_loop_stopped(ledger, kw))
    except Exception:
        logger.warning("session-triage: hook registration failed", exc_info=True)

    try:
        ctx.register_tool(
            name=TOOL,
            toolset=TOOLSET,
            schema=_TOOL_SCHEMA,
            handler=lambda args: json.dumps(
                _handle(ctx, cfg_get, ledger, state_db, home, state_dir, args or {}),
                indent=2, default=str,
            ),
            description="Detect, diagnose and resume abandoned Hermes sessions.",
        )
    except Exception:
        logger.warning("session-triage: tool registration failed", exc_info=True)

    try:
        def _command(raw_args: str) -> str:
            parts = (raw_args or "").split()
            action = parts[0] if parts else "status"
            args: dict = {"action": action}
            if len(parts) > 1:
                args["session_id"] = parts[1]
            return json.dumps(_handle(ctx, cfg_get, ledger, state_db, home, state_dir, args),
                              indent=2, default=str)

        ctx.register_command("triage", _command,
                             description="Session triage: status | scan | stalls | resume|recover <session_id>",
                             args_hint="[status|scan|ledger|stalls|resume|recover <session_id>]")
    except Exception:
        logger.warning("session-triage: command registration failed", exc_info=True)

    stop = threading.Event()
    _start_worker(ctx, cfg_get, ledger, state_db, home, state_dir, stop)
    try:
        ctx.on_unload(stop.set)
    except Exception:
        logger.debug("session-triage: on_unload registration failed", exc_info=True)
