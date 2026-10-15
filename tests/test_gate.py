import os
import tempfile
import time
import unittest
from pathlib import Path

from . import _fixture
from triage import db, resume, store
from triage.config import Config
from triage.model import Candidate, Diagnosis


class GateTest(unittest.TestCase):
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

    def _gate(self, conn, diagnosis=None, cfg=None, candidate=None):
        return resume.gate(conn, self.ledger, candidate or self.candidate,
                           diagnosis or Diagnosis(), now=self.now, cfg=cfg or Config())

    def test_happy_path_is_allowed(self):
        allowed, why = self._gate(self._db())
        self.assertTrue(allowed, why)

    def test_auto_resume_off_blocks(self):
        allowed, why = self._gate(self._db(), cfg=Config(auto_resume=False))
        self.assertFalse(allowed)
        self.assertIn("auto_resume", why)

    def test_blocking_cause_blocks(self):
        allowed, why = self._gate(self._db(),
                                  diagnosis=Diagnosis(blocking=["credentials_unavailable"]))
        self.assertFalse(allowed)
        self.assertIn("blocking", why)

    def test_live_lease_blocks(self):
        conn = self._db(leases=[{"conversation_id": "s1", "holder": f"pid={os.getpid()}",
                                 "expires_at": self.now + 100}])
        allowed, why = self._gate(conn)
        self.assertFalse(allowed)
        self.assertIn("live lease", why)

    def test_missing_session_key_blocks(self):
        candidate = Candidate(session_id="s1", session_key=None, source="lease",
                              reason="turn_lease_expired", signal_ts=self.now - 120)
        allowed, why = self._gate(self._db(), candidate=candidate)
        self.assertFalse(allowed)
        self.assertIn("session_key", why)

    def test_per_session_budget_blocks(self):
        for _ in range(2):
            store.append(self.ledger, {"kind": "resume", "session_id": "s1"})
        allowed, why = self._gate(self._db(), cfg=Config(max_resumes_per_session=2))
        self.assertFalse(allowed)
        self.assertIn("budget", why)

    def test_fence_blocks_a_repeat(self):
        store.append(self.ledger, {"kind": "resume", "session_id": "s1", "source": "lease",
                                   "reason": "turn_lease_expired"})
        allowed, why = self._gate(self._db())
        self.assertFalse(allowed)
        self.assertIn("fenced", why)

    def test_pause_file_blocks(self):
        pause = self.tmp / "PAUSE"
        pause.write_text("")
        allowed, why = self._gate(self._db(), cfg=Config(pause_file=str(pause)))
        self.assertFalse(allowed)
        self.assertIn("paused", why)


if __name__ == "__main__":
    unittest.main()
