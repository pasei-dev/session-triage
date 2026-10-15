"""The wiring half: ``register(ctx)`` boots cleanly and refuses to run where injection is unsafe."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from . import _fixture  # noqa: F401
from tests._fixture import ROOT


def _load_plugin_module():
    name = "session_triage_plugin_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _State:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Manager:
    def __init__(self, *, cli_ref=None, gateway=False, tui=False):
        self._cli_ref = cli_ref
        self.has_gateway_message_injector = gateway
        self.has_tui_message_injector = tui


class _Ctx:
    def __init__(self, data_dir, manager):
        self.state = _State(data_dir)
        self._manager = manager
        self.hooks = {}
        self.tools = []
        self.commands = {}
        self.injects = []
        self.unloaded = None

    def get_config(self, key, default=None):
        return default

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, name, callback, **kwargs):
        self.commands[name] = callback

    def inject_message(self, content, *, session_key=None, origin=None):
        self.injects.append((content, session_key))
        return True

    def on_unload(self, callback):
        self.unloaded = callback


class PluginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plugin = _load_plugin_module()
        self._old_home = __import__("os").environ.get("SESSION_TRIAGE_HERMES_HOME")
        __import__("os").environ["SESSION_TRIAGE_HERMES_HOME"] = str(self.tmp / "home")

    def tearDown(self):
        import os

        if self._old_home is None:
            os.environ.pop("SESSION_TRIAGE_HERMES_HOME", None)
        else:
            os.environ["SESSION_TRIAGE_HERMES_HOME"] = self._old_home

    def test_register_wires_hooks_tool_and_command(self):
        ctx = _Ctx(self.tmp / "data", _Manager())
        self.plugin.register(ctx)
        self.assertIn("on_session_end", ctx.hooks)
        self.assertIn("agent_loop_stopped", ctx.hooks)
        self.assertEqual([t["name"] for t in ctx.tools], ["session_triage"])
        self.assertIn("triage", ctx.commands)
        self.assertIsNotNone(ctx.unloaded)
        ctx.unloaded()  # stop the worker

    def test_hooks_record_to_the_ledger(self):
        ctx = _Ctx(self.tmp / "data", _Manager())
        self.plugin.register(ctx)
        ctx.hooks["agent_loop_stopped"](session_key="k1", reason="user_stop")
        ctx.hooks["on_session_end"](session_id="s1", interrupted=True,
                                    turn_exit_reason="interrupted", platform="desktop")
        from triage import store

        records = store.read_all(Path(ctx.state.data_dir) / store.LEDGER_NAME)
        kinds = {r["kind"] for r in records}
        self.assertIn("stop", kinds)
        self.assertIn("interrupted", kinds)
        ctx.unloaded()

    def test_can_inject_refuses_cli_and_requires_a_host(self):
        can = self.plugin._can_inject
        self.assertFalse(can(_Ctx(self.tmp, _Manager(cli_ref=object()))))
        self.assertFalse(can(_Ctx(self.tmp, _Manager())))
        self.assertTrue(can(_Ctx(self.tmp, _Manager(gateway=True))))
        self.assertTrue(can(_Ctx(self.tmp, _Manager(cli_ref=None, tui=True))))

    def test_status_action_reports_injector_state(self):
        ctx = _Ctx(self.tmp / "data", _Manager(gateway=True))
        self.plugin.register(ctx)
        out = self.plugin._handle(ctx, lambda: self.plugin.Config(), str(self.tmp / "l.jsonl"),
                                  self.tmp / "state.db", str(self.tmp), self.tmp, {"action": "status"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["injector_available"])
        ctx.unloaded()


    def test_heartbeat_carries_pass_stats(self):
        import json

        # No register(): that would start the worker thread, which writes its own heartbeat and races
        # this assertion. _write_heartbeat is exercised directly (register() is what would mkdir the dir).
        state_dir = self.tmp / "data"
        state_dir.mkdir(parents=True, exist_ok=True)
        ctx = _Ctx(state_dir, _Manager(gateway=True))
        self.plugin._write_heartbeat(state_dir, ctx, self.plugin.Config(),
                                     last_pass={"considered": 3, "resumed": 1},
                                     last_error="", passes=2)
        data = json.loads((self.tmp / "data" / "worker.json").read_text())
        self.assertEqual(data["last_pass"]["resumed"], 1)
        self.assertEqual(data["passes"], 2)
        self.assertEqual(data["last_error"], "")


if __name__ == "__main__":
    unittest.main()
