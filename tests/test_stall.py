import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from . import _fixture
from triage import db, logs, stall
from triage.config import Config


def _iso(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _line(ts, sid, logger, msg, level="INFO"):
    return f"{_iso(ts)},{int(ts % 1 * 1000):03d} {level} [{sid}] {logger}: {msg}"


class StallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs").mkdir()
        self.now = time.time()

    def _db(self, admissions):
        self._n = getattr(self, "_n", 0) + 1
        path = self.tmp / f"state-{self._n}.db"
        conn = db.connect(_fixture.make_db(path, admissions=admissions))
        self.addCleanup(conn.close)
        return conn

    def _events(self, lines):
        (self.tmp / "logs" / "agent.log").write_text("\n".join(lines) + "\n")
        return logs.iter_events(logs.log_paths(self.tmp))

    def test_post_fence_with_successor(self):
        conn = self._db([
            {"admission_id": "a1", "target_session_id": "s1", "status": "unknown"},
            {"admission_id": "a2", "target_session_id": "s1", "status": "queued"},
        ])
        found = stall.find_stalls(conn, [], self.now, Config())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "post_fence")
        self.assertTrue(found[0].has_queued_successor)

    def test_post_fence_without_successor(self):
        conn = self._db([{"admission_id": "a1", "target_session_id": "s1", "status": "unknown"}])
        found = stall.find_stalls(conn, [], self.now, Config())
        self.assertEqual(found[0].kind, "post_fence")
        self.assertFalse(found[0].has_queued_successor)

    def test_pre_fence_stall_attributed_to_tool(self):
        conn = self._db([{"admission_id": "a1", "target_session_id": "s1", "status": "started"}])
        events = self._events([_line(self.now - 600, "s1", "agent.conversation_loop",
                                     "API call #5: model=m provider=p in=1 out=2")])
        found = stall.find_stalls(conn, events, self.now, Config(stall_seconds=300))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "pre_fence")
        self.assertEqual(found[0].attribution, "tool")
        self.assertGreater(found[0].stalled_for or 0, 300)

    def test_pre_fence_fresh_is_not_a_stall(self):
        conn = self._db([{"admission_id": "a1", "target_session_id": "s1", "status": "started"}])
        events = self._events([_line(self.now - 5, "s1", "agent.tool_executor",
                                     "tool terminal completed (0.3s, 10 chars)")])
        self.assertEqual(stall.find_stalls(conn, events, self.now, Config(stall_seconds=300)), [])

    def test_pre_fence_provider_attribution(self):
        conn = self._db([{"admission_id": "a1", "target_session_id": "s1", "status": "started"}])
        events = self._events([_line(self.now - 600, "s1", "agent.turn_liveness",
                                     "waiting for non-streaming API response", level="ERROR")])
        found = stall.find_stalls(conn, events, self.now, Config(stall_seconds=300))
        self.assertEqual(found[0].attribution, "provider")

    def test_fence_resolved_reads_the_store(self):
        conn = self._db([{"admission_id": "a1", "target_session_id": "s1", "status": "unknown"}])
        self.assertFalse(stall.fence_resolved(conn, "s1"))
        conn2 = self._db([{"admission_id": "a1", "target_session_id": "s1", "status": "terminal"}])
        self.assertTrue(stall.fence_resolved(conn2, "s1"))


if __name__ == "__main__":
    unittest.main()
