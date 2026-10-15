import tempfile
import time
import unittest
from pathlib import Path

from . import _fixture  # noqa: F401  (puts the plugin root on sys.path)
from triage import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ledger = self.dir / "ledger.jsonl"

    def test_append_and_read_roundtrip(self):
        store.append(self.ledger, {"kind": "skip", "session_id": "s1"})
        store.append(self.ledger, {"kind": "resume", "session_id": "s1", "source": "lease",
                                   "reason": "turn_lease_expired"})
        records = store.read_all(self.ledger)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["kind"], "skip")
        self.assertIn("ts", records[0])

    def test_read_all_tolerates_missing_file_and_garbage(self):
        self.assertEqual(store.read_all(self.dir / "nope.jsonl"), [])
        self.ledger.write_text("not json\n{\"kind\":\"skip\",\"session_id\":\"s\"}\n")
        self.assertEqual(len(store.read_all(self.ledger)), 1)

    def test_stop_matches_by_session_key_and_id(self):
        store.append(self.ledger, {"kind": "stop", "session_key": "key-9"})
        self.assertTrue(store.stopped_any(self.ledger, ["sid", "key-9"], 0.0))
        self.assertFalse(store.stopped_any(self.ledger, ["other"], 0.0))

    def test_stop_respects_time_window(self):
        store.append(self.ledger, {"kind": "stop", "session_id": "s1", "ts": 100.0})
        self.assertFalse(store.stopped_any(self.ledger, ["s1"], 200.0))

    def test_resume_count_scopes_by_session(self):
        for _ in range(3):
            store.append(self.ledger, {"kind": "resume", "session_id": "a"})
        store.append(self.ledger, {"kind": "resume", "session_id": "b"})
        self.assertEqual(store.resume_count(self.ledger, since=0.0, session_id="a"), 3)
        self.assertEqual(store.resume_count(self.ledger, since=0.0), 4)
        self.assertEqual(store.resume_count(self.ledger, since=0.0, session_id="a"), 3)

    def test_fence_matches_source_and_reason(self):
        store.append(self.ledger, {"kind": "resume", "session_id": "a",
                                   "source": "lease", "reason": "turn_lease_expired"})
        now = time.time()
        self.assertTrue(store.fenced(self.ledger, "a", "lease", "turn_lease_expired",
                                     ttl=900, now=now))
        self.assertFalse(store.fenced(self.ledger, "a", "reaped", "startup_orphan_reap",
                                      ttl=900, now=now))


if __name__ == "__main__":
    unittest.main()
