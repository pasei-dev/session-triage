"""The readers must match a real ``state.db``, not just our own fixture.

``tests/_fixture.py`` is hand-written, so a test comparing it to ``db.READ_COLUMNS`` proves only that
the two agree with each other — they can drift away from Hermes together, which is exactly how one
renamed column left every detector silently dead while the suite stayed green. This module checks the
same map against a store Hermes itself wrote, and skips when there is none to read.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from . import _fixture  # noqa: F401  (puts the plugin root on sys.path)
from triage import db


def _live_state_db() -> Path | None:
    """A real profile store: the override first, then the host's own home when it is importable."""
    override = os.environ.get("SESSION_TRIAGE_STATE_DB")
    if override and Path(override).is_file():
        return Path(override)
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        candidate = Path(get_hermes_home()) / "state.db"
    except Exception:
        return None
    return candidate if candidate.is_file() else None


class SchemaConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state_db = _live_state_db()

    def setUp(self):
        if self.state_db is None:
            self.skipTest("no live state.db (point SESSION_TRIAGE_STATE_DB at one to check it)")

    def _conn(self):
        assert self.state_db is not None  # setUp skips otherwise
        return db.connect(self.state_db)

    def test_every_table_the_stages_read_exists(self):
        conn = self._conn()
        self.addCleanup(conn.close)
        for table in db.READ_COLUMNS:
            self.assertTrue(db.has_table(conn, table), f"{table} is missing from the live schema")

    def test_every_column_the_stages_read_exists(self):
        conn = self._conn()
        self.addCleanup(conn.close)
        for table, columns in db.READ_COLUMNS.items():
            live = db.table_columns(conn, table)
            missing = [name for name in columns if name not in live]
            self.assertEqual(missing, [], f"live {table} lacks {missing}")

    def test_the_readers_run_against_a_live_store(self):
        conn = self._conn()
        self.addCleanup(conn.close)
        self.assertIsInstance(db.max_heartbeat(conn), (float, type(None)))
        self.assertIsNone(db.session_row(conn, "no-such-session-id"))


if __name__ == "__main__":
    unittest.main()
