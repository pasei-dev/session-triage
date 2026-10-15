import tempfile
import time
import unittest
from pathlib import Path

from . import _fixture
from triage import db, engine, store
from triage.config import Config


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / "logs").mkdir(parents=True)
        (self.home / "cache" / "spillover").mkdir(parents=True)
        self.ledger = str(self.tmp / "ledger.jsonl")
        self.now = time.time()
        self.injects = []

    def _inject(self, content, *, session_key=None):
        self.injects.append((content, session_key))
        return True

    def _db(self, **kw):
        conn = db.connect(_fixture.make_db(self.tmp / "state.db", **kw))
        self.addCleanup(conn.close)
        return conn

    def _interrupted_session(self):
        return dict(
            sessions=[{"id": "s1", "session_key": "k1", "last_activity_at": self.now - 120}],
            leases=[{"conversation_id": "s1", "holder": f"pid={_fixture.DEAD_PID}",
                     "expires_at": self.now - 30}],
        )

    def _run(self, conn, cfg=None):
        return engine.run_pass(conn, hermes_home=str(self.home), ledger=self.ledger,
                               inject=self._inject, cfg=cfg or Config(), now=self.now)

    def test_resumes_an_involuntary_interruption_once(self):
        result = self._run(self._db(**self._interrupted_session()))
        self.assertEqual(result.resumed, 1)
        self.assertEqual(len(self.injects), 1)
        self.assertEqual(self.injects[0][1], "k1")
        self.assertIn("interrupted", self.injects[0][0])

    def test_second_pass_is_fenced(self):
        conn = self._db(**self._interrupted_session())
        self._run(conn)
        result = self._run(conn)
        self.assertEqual(result.resumed, 0)
        self.assertEqual(len(self.injects), 1)  # no second injection

    def test_manual_stop_is_skipped(self):
        store.append(self.ledger, {"kind": "stop", "session_key": "k1", "ts": self.now - 60})
        result = self._run(self._db(**self._interrupted_session()))
        self.assertEqual(result.resumed, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(self.injects, [])

    def test_blocking_disk_cause_defers(self):
        result = self._run(
            self._db(**self._interrupted_session()),
            cfg=Config(disk_free_floor_mb=10 ** 9, auto_heal=False),
        )
        self.assertEqual(result.resumed, 0)
        self.assertEqual(result.deferred, 1)
        self.assertEqual(self.injects, [])

    def test_dry_run_never_injects(self):
        result = self._run(self._db(**self._interrupted_session()), cfg=Config(dry_run=True))
        self.assertEqual(result.resumed, 0)
        self.assertEqual(self.injects, [])
        self.assertTrue(any(a["action"] == "report" for a in result.actions))

    def test_injector_refusal_is_a_defer(self):
        def refusing(content, *, session_key=None):
            return False

        result = engine.run_pass(self._db(**self._interrupted_session()),
                                 hermes_home=str(self.home), ledger=self.ledger,
                                 inject=refusing, cfg=Config(), now=self.now)
        self.assertEqual(result.resumed, 0)
        self.assertEqual(result.deferred, 1)


if __name__ == "__main__":
    unittest.main()
