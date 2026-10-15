import tempfile
import unittest
from pathlib import Path

from . import _fixture  # noqa: F401
from triage import logs


def _line(ts, sid, logger, msg, level="INFO"):
    bracket = f"[{sid}] " if sid else ""
    return f"{ts} {level} {bracket}{logger}: {msg}"


class LogsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs").mkdir()

    def _write(self, lines, name="agent.log"):
        (self.tmp / "logs" / name).write_text("\n".join(lines) + "\n")

    def _events(self):
        return logs.iter_events(logs.log_paths(self.tmp))

    def test_parse_shape_and_last_ts(self):
        self._write([_line("2026-10-15 20:54:12,874", "s1", "agent.tool_executor",
                           "tool write_file completed (1.77s, 279 chars)")])
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].session_id, "s1")
        self.assertEqual(events[0].logger, "agent.tool_executor")
        self.assertGreater(logs.last_event_ts(events, "s1"), 0)

    def test_attribute_provider(self):
        self._write([_line("2026-10-15 20:54:12,874", "s1", "agent.turn_liveness",
                           "no progress: waiting for non-streaming API response", level="ERROR")])
        self.assertEqual(logs.attribute(self._events(), "s1", 0.0).kind, "provider")

    def test_attribute_tool_from_checkpoint_failure(self):
        self._write([_line("2026-10-15 20:54:12,874", "s1", "tools.checkpoint_manager",
                           "Git command failed: /opt/homebrew/bin/git add -A (rc=128) stderr=fatal: "
                           "Unable to create '.../.hermes/pr...'", level="ERROR")])
        self.assertEqual(logs.attribute(self._events(), "s1", 0.0).kind, "tool")

    def test_attribute_tool_from_open_api_call(self):
        self._write([_line("2026-10-15 20:54:12,874", "s1", "agent.conversation_loop",
                           "API call #23: model=m provider=p in=1 out=2")])
        self.assertEqual(logs.attribute(self._events(), "s1", 0.0).kind, "tool")

    def test_attribute_infrastructure_overrides(self):
        self._write([
            _line("2026-10-15 20:54:12,874", "s1", "agent.conversation_loop", "API call #23: ..."),
            _line("2026-10-15 20:55:00,000", None, "gateway.run",
                  "Received SIGTERM as a planned gateway stop", level="WARNING"),
        ])
        self.assertEqual(logs.attribute(self._events(), "s1", 0.0).kind, "infrastructure")

    def test_attribute_unknown_when_nothing_matches(self):
        self._write([_line("2026-10-15 20:54:12,874", "s1", "agent.tool_executor",
                           "tool terminal completed (0.34s, 1433 chars)")])
        self.assertEqual(logs.attribute(self._events(), "s1", 0.0).kind, "unknown")

    def test_no_logs_yields_unknown(self):
        self.assertEqual(logs.attribute([], "s1", 0.0).kind, "unknown")

    def test_planned_restart_flag(self):
        self._write([_line("2026-10-15 20:55:00,000", None, "gateway.run",
                           "Stopping gateway — draining in-flight runs", level="WARNING")])
        self.assertTrue(logs.planned_restart(self._events(), 0.0))


    def test_paused_sessions_extracts_id_and_reason(self):
        self._write([
            _line("2026-10-15 21:13:36,150", None, "gateway.session_local_recovery",
                  "Local session sX paused: admission_conflict", level="WARNING"),
            _line("2026-10-15 21:13:02,977", None, "gateway.session_authority",
                  "Session sY paused: runtime_draining", level="WARNING"),
        ])
        found = {sid: reason for sid, reason, _ts in logs.paused_sessions(self._events())}
        self.assertEqual(found.get("sX"), "admission_conflict")
        self.assertEqual(found.get("sY"), "runtime_draining")


if __name__ == "__main__":
    unittest.main()
