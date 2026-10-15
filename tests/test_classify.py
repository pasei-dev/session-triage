import tempfile
import time
import unittest
from pathlib import Path

from . import _fixture
from triage import classify, db, store
from triage.config import Config
from triage.model import Candidate

CFG = Config(resume_window_seconds=3600)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = self.tmp / "ledger.jsonl"
        self.now = time.time()
        self.candidate = Candidate(session_id="s1", session_key="k1", source="lease",
                                   reason="turn_lease_expired", signal_ts=self.now - 120)

    def _db(self, **kw):
        conn = db.connect(_fixture.make_db(self.tmp / "state.db", **kw))
        self.addCleanup(conn.close)
        return conn

    def test_plain_interruption_is_involuntary(self):
        conn = self._db(sessions=[{"id": "s1", "session_key": "k1"}])
        verdict = classify.classify(conn, self.ledger, self.candidate, self.now, CFG)
        self.assertTrue(verdict.involuntary)

    def test_recorded_stop_is_not_resumable(self):
        store.append(self.ledger, {"kind": "stop", "session_key": "k1",
                                   "ts": self.now - 100})
        conn = self._db(sessions=[{"id": "s1", "session_key": "k1"}])
        verdict = classify.classify(conn, self.ledger, self.candidate, self.now, CFG)
        self.assertFalse(verdict.involuntary)
        self.assertIn("manual stop", verdict.why)

    def test_deliberate_close_is_not_resumable(self):
        conn = self._db(sessions=[{"id": "s1", "session_key": "k1", "end_reason": "cli_close"}])
        verdict = classify.classify(conn, self.ledger, self.candidate, self.now, CFG)
        self.assertFalse(verdict.involuntary)
        self.assertIn("deliberately", verdict.why)

    def test_steered_turn_is_not_resumable(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1"}],
            admissions=[{"admission_id": "a1", "target_session_id": "s1", "status": "terminal",
                         "intent": "steer"}],
        )
        verdict = classify.classify(conn, self.ledger, self.candidate, self.now, CFG)
        self.assertFalse(verdict.involuntary)
        self.assertIn("steered", verdict.why)

    def test_superseded_by_newer_user_turn(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1"}],
            messages=[{"session_id": "s1", "role": "user", "content": "new question",
                       "timestamp": self.now - 30}],
        )
        verdict = classify.classify(conn, self.ledger, self.candidate, self.now, CFG)
        self.assertFalse(verdict.involuntary)
        self.assertIn("superseded", verdict.why)

    def test_completed_reply_after_interruption_is_not_resumable(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1"}],
            messages=[{"session_id": "s1", "role": "assistant", "content": "done",
                       "finish_reason": "stop", "timestamp": self.now - 30}],
        )
        verdict = classify.classify(conn, self.ledger, self.candidate, self.now, CFG)
        self.assertFalse(verdict.involuntary)
        self.assertIn("already completed", verdict.why)

    def test_user_message_before_interruption_does_not_supersede(self):
        conn = self._db(
            sessions=[{"id": "s1", "session_key": "k1"}],
            messages=[{"session_id": "s1", "role": "user", "content": "the ask",
                       "timestamp": self.now - 300}],
        )
        verdict = classify.classify(conn, self.ledger, self.candidate, self.now, CFG)
        self.assertTrue(verdict.involuntary)


if __name__ == "__main__":
    unittest.main()
