"""The row reader must survive a schema that differs from the build, and the fixture must match the
columns the plugin reads — the class of bug that let a renamed column silently disable detection."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import _fixture  # noqa: F401  (puts the plugin root on sys.path)
from triage import db
from triage.model import RESUME_SENTINEL


class SessionRowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _conn(self, **kw):
        conn = db.connect(_fixture.make_db(self.tmp / "state.db", **kw))
        self.addCleanup(conn.close)
        return conn

    def test_fixture_has_every_column_the_plugin_reads(self):
        conn = self._conn()
        for table, columns in db.READ_COLUMNS.items():
            missing = [name for name in columns if name not in db.table_columns(conn, table)]
            self.assertEqual(missing, [], f"fixture table {table} lacks {missing}")

    def test_session_row_returns_a_row(self):
        conn = self._conn(sessions=[{"id": "s1", "session_key": "k1", "last_activity_at": 123.0}])
        row = db.session_row(conn, "s1")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["session_key"], "k1")

    def test_session_row_survives_a_schema_missing_a_column(self):
        path = self.tmp / "slim.db"
        raw = sqlite3.connect(path)
        raw.execute("create table sessions(id text primary key, session_key text, end_reason text)")
        raw.execute("insert into sessions(id, session_key) values('s1','k1')")
        raw.commit()
        raw.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        row = db.session_row(conn, "s1")  # a schema without the compression columns must still work
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["session_key"], "k1")

    def test_max_user_ts_ignores_the_resume_note(self):
        conn = self._conn(messages=[
            {"session_id": "s1", "role": "user", "content": "real ask", "timestamp": 100.0},
            {"session_id": "s1", "role": "user",
             "content": RESUME_SENTINEL + " automated note", "timestamp": 200.0},
        ])
        self.assertEqual(db.max_user_ts(conn, "s1"), 100.0)

    def test_connect_is_read_only(self):
        conn = self._conn(sessions=[{"id": "s1"}])
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("insert into sessions(id) values('s2')")


if __name__ == "__main__":
    unittest.main()
