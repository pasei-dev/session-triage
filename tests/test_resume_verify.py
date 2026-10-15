"""A resume is not a recovery: the verification stage has to say which it was.

``resumed`` only ever meant "the host accepted the injection". These tests pin the second half — a
resume settles as verified once a reply exists after it, and as failed once the grace passes with
none — so the ledger cannot quietly imply a fix that never happened.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from . import _fixture  # noqa: F401  (puts the plugin root on sys.path)
from triage import db, engine, store
from triage.config import Config


class ResumeVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / "logs").mkdir(parents=True)
        self.ledger = str(self.tmp / "ledger.jsonl")
        self.now = time.time()

    def _db(self, **kw):
        conn = db.connect(_fixture.make_db(self.tmp / "state.db", **kw))
        self.addCleanup(conn.close)
        return conn

    def _run(self, conn, cfg=None):
        return engine.run_pass(conn, hermes_home=str(self.home), ledger=self.ledger,
                               inject=lambda content, *, session_key=None: True,
                               cfg=cfg or Config(), now=self.now)

    def _resume(self, *, ts, ok=True, session_id="s1"):
        store.append(self.ledger, {"kind": "resume", "session_id": session_id, "session_key": "k1",
                                   "source": "lease", "reason": "turn_lease_expired",
                                   "ok": ok, "ts": ts})

    def _kinds(self):
        return [rec["kind"] for rec in store.read_all(self.ledger)]

    def test_a_reply_after_the_resume_verifies_it(self):
        self._resume(ts=self.now - 300)
        conn = self._db(messages=[{"session_id": "s1", "role": "assistant", "content": "done",
                                   "finish_reason": "stop", "timestamp": self.now - 288}])
        result = self._run(conn)
        self.assertEqual(result.resume_verified, 1)
        self.assertEqual(result.resume_failed, 0)
        self.assertIn("resume_verified", self._kinds())

    def test_no_reply_past_the_grace_fails_it(self):
        self._resume(ts=self.now - 300)
        result = self._run(self._db(), cfg=Config(resume_verify_seconds=60))
        self.assertEqual(result.resume_failed, 1)
        self.assertEqual(result.resume_verified, 0)
        self.assertIn("resume_failed", self._kinds())

    def test_an_unanswered_resume_inside_the_grace_stays_pending(self):
        self._resume(ts=self.now - 30)
        result = self._run(self._db(), cfg=Config(resume_verify_seconds=600))
        self.assertEqual(result.resume_verified, 0)
        self.assertEqual(result.resume_failed, 0)

    def test_a_resume_is_settled_only_once(self):
        self._resume(ts=self.now - 300)
        conn = self._db(messages=[{"session_id": "s1", "role": "assistant", "content": "done",
                                   "finish_reason": "stop", "timestamp": self.now - 200}])
        self._run(conn)
        result = self._run(conn)
        self.assertEqual(result.resume_verified, 0)
        self.assertEqual(self._kinds().count("resume_verified"), 1)

    def test_a_refused_injection_is_not_verified(self):
        self._resume(ts=self.now - 300, ok=False)
        result = self._run(self._db())
        self.assertEqual(result.resume_verified, 0)
        self.assertEqual(result.resume_failed, 0)

    def test_a_reply_from_before_the_resume_does_not_count(self):
        self._resume(ts=self.now - 300)
        conn = self._db(messages=[{"session_id": "s1", "role": "assistant", "content": "older",
                                   "finish_reason": "stop", "timestamp": self.now - 900}])
        result = self._run(conn, cfg=Config(resume_verify_seconds=60))
        self.assertEqual(result.resume_verified, 0)
        self.assertEqual(result.resume_failed, 1)


if __name__ == "__main__":
    unittest.main()
