import os
import tempfile
import time
import unittest
from pathlib import Path

from . import _fixture
from triage import db, detect
from triage.config import Config

CFG = Config(min_age_seconds=10, max_age_seconds=6 * 3600, lease_grace_seconds=5)


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.now = time.time()

    def _db(self, **kw):
        path = _fixture.make_db(self.tmp / "state.db", **kw)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        return conn

    def test_expired_lease_with_dead_pid_is_a_candidate(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1", "last_activity_at": self.now - 120}],
            leases=[{"conversation_id": "s1", "holder": f"pid={_fixture.DEAD_PID}:turn=x",
                     "expires_at": self.now - 30}],
        )
        found = detect.find_candidates(conn, self.now, CFG)
        self.assertEqual([c.session_id for c in found], ["s1"])
        self.assertEqual(found[0].source, "lease")
        self.assertEqual(found[0].session_key, "k1")

    def test_expired_lease_with_live_pid_is_left_alone(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1", "last_activity_at": self.now - 120}],
            leases=[{"conversation_id": "s1", "holder": f"pid={os.getpid()}:turn=x",
                     "expires_at": self.now - 30}],
        )
        self.assertEqual(detect.find_candidates(conn, self.now, CFG), [])

    def test_fresh_lease_is_not_yet_a_candidate(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1", "last_activity_at": self.now - 2}],
            leases=[{"conversation_id": "s1", "holder": f"pid={_fixture.DEAD_PID}",
                     "expires_at": self.now - 1}],
        )
        self.assertEqual(detect.find_candidates(conn, self.now, CFG), [])

    def test_unsettled_admission_is_a_candidate(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1", "last_activity_at": self.now - 120}],
            admissions=[{"admission_id": "a1", "target_session_id": "s1", "status": "started"}],
        )
        found = detect.find_candidates(conn, self.now, CFG)
        self.assertEqual([c.source for c in found], ["admission"])

    def test_settled_admission_is_not_a_candidate(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1", "last_activity_at": self.now - 120}],
            admissions=[{"admission_id": "a1", "target_session_id": "s1", "status": "started"},
                        {"admission_id": "a2", "target_session_id": "s1", "status": "terminal",
                         "outcome": "completed"}],
        )
        self.assertEqual([c for c in detect.find_candidates(conn, self.now, CFG) if c.source == "admission"], [])

    def test_reaped_session_with_completed_reply_is_skipped(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1", "end_reason": "startup_orphan_reap",
                       "last_activity_at": self.now - 120}],
            messages=[{"session_id": "s1", "role": "assistant", "content": "done",
                       "finish_reason": "stop", "timestamp": self.now - 119}],
        )
        self.assertEqual([c for c in detect.find_candidates(conn, self.now, CFG) if c.source == "reaped"], [])

    def test_reaped_session_without_reply_is_a_candidate(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1", "end_reason": "ws_orphan_reap",
                       "last_activity_at": self.now - 120}],
        )
        found = [c for c in detect.find_candidates(conn, self.now, CFG) if c.source == "reaped"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].reason, "ws_orphan_reap")

    def test_ledger_interruption_is_a_candidate(self):
        from triage import store

        conn = self._db(sessions=[{"id": "s1", "session_key": "k1",
                                   "last_activity_at": self.now - 120}])
        ledger = self.tmp / "ledger.jsonl"
        store.append(ledger, {"kind": "interrupted", "session_id": "s1",
                              "reason": "incomplete", "ts": self.now - 120})
        found = [c for c in detect.find_candidates(conn, self.now, CFG, ledger) if c.source == "hook"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].session_key, "k1")

    def test_ledger_interruption_for_unknown_session_is_ignored(self):
        from triage import store

        conn = self._db()
        ledger = self.tmp / "ledger.jsonl"
        store.append(ledger, {"kind": "interrupted", "session_id": "ghost", "ts": self.now - 120})
        self.assertEqual(detect.find_candidates(conn, self.now, CFG, ledger), [])

    def test_lease_wins_over_other_sources_for_same_session(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1", "end_reason": "startup_orphan_reap",
                       "last_activity_at": self.now - 120}],
            leases=[{"conversation_id": "s1", "holder": f"pid={_fixture.DEAD_PID}",
                     "expires_at": self.now - 30}],
        )
        found = detect.find_candidates(conn, self.now, CFG)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source, "lease")


if __name__ == "__main__":
    unittest.main()
