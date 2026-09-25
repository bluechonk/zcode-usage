"""Tests for the zcode-usage desktop app.

Data-layer tests run against a throwaway SQLite database in a temp directory
(no real ZCode database is touched). UI-model tests run with an offscreen Qt
platform so no window is created.
"""

import argparse
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import usage  # noqa: E402
import usage_app  # noqa: E402

NOW = int(time.time() * 1000)
DAY_MS = 86_400_000

_SCHEMA = """
CREATE TABLE model_usage (
  session_id TEXT, provider_id TEXT, model_id TEXT, status TEXT,
  started_at INTEGER, input_tokens INTEGER, output_tokens INTEGER,
  reasoning_tokens INTEGER, cache_read_input_tokens INTEGER,
  cache_creation_input_tokens INTEGER, duration_ms INTEGER,
  time_to_first_token_ms INTEGER, tool_call_count INTEGER,
  retry_count INTEGER, context_exceeded INTEGER,
  error_type TEXT, error_code TEXT, error_message TEXT
);
CREATE TABLE tool_usage (
  session_id TEXT, started_at INTEGER, tool_name TEXT, status TEXT,
  duration_ms INTEGER, exit_code INTEGER, output_bytes INTEGER,
  read_only INTEGER, destructive INTEGER
);
"""

_MODEL_ROWS = [
    (
        "sess_test1",
        "mimo-x-bridge",
        "glm-5.3",
        "completed",
        NOW - DAY_MS,
        1000,
        200,
        50,
        800,
        100,
        1200,
        300,
        2,
        0,
        0,
        None,
        None,
        None,
    ),
    (
        "sess_test1",
        "mimo-x-bridge",
        "glm-5.3",
        "error",
        NOW - DAY_MS,
        500,
        0,
        0,
        0,
        0,
        800,
        None,
        0,
        1,
        0,
        "rate_limit",
        "429",
        "slow down",
    ),
    (
        "sess_test2",
        "other",
        "m2",
        "completed",
        NOW - 60 * DAY_MS,
        999,
        999,
        0,
        0,
        0,
        5000,
        900,
        1,
        0,
        0,
        None,
        None,
        None,
    ),
]

_TOOL_ROWS = [
    ("sess_test1", NOW - DAY_MS, "Bash", "completed", 120, 0, 2048, 0, 0),
    ("sess_test1", NOW - DAY_MS, "Read", "completed", 10, 0, 512, 1, 0),
]

_SESSION_SCHEMA = """
CREATE TABLE session (
    id text primary key,
    project_id text not null,
    workspace_id text,
    parent_id text,
    slug text not null,
    directory text not null,
    path text,
    title text not null,
    version text not null,
    share_url text,
    summary_additions integer,
    summary_deletions integer,
    summary_files integer,
    summary_diffs text,
    revert text,
    permission text,
    time_created integer not null,
    time_updated integer not null,
    time_compacting integer,
    time_archived integer,
    task_type text not null default 'interactive',
    title_source text not null default 'first_input',
    title_message_id text,
    time_title_updated integer,
    trace_id text
);
CREATE TABLE message (
    id text primary key,
    session_id text not null references session(id) on delete cascade,
    time_created integer not null,
    time_updated integer not null,
    data text not null,
    sequence integer
);
CREATE TABLE part (
    id text primary key,
    message_id text not null references message(id) on delete cascade,
    session_id text not null,
    time_created integer not null,
    time_updated integer not null,
    data text not null,
    sequence integer
);
"""

_SESSION_ROWS = [
    (
        "sess_test1",
        "proj1",
        None,
        None,
        "test-session-1",
        "/c/Users/bluechonk/test1",
        None,
        "Test Session 1",
        "1.0",
        None,
        0,
        0,
        0,
        None,
        None,
        "read_write",
        NOW - 2 * DAY_MS,
        NOW - DAY_MS,
        None,
        None,
        "interactive",
        "first_input",
        None,
        NOW - DAY_MS,
        "trace1",
    ),
    (
        "sess_test2",
        "proj1",
        None,
        None,
        "test-session-2",
        "/c/Users/bluechonk/test2",
        None,
        "Test Session 2",
        "1.0",
        None,
        0,
        0,
        0,
        None,
        None,
        "read_write",
        NOW - 3 * DAY_MS,
        NOW - 2 * DAY_MS,
        None,
        NOW - DAY_MS,
        "interactive",
        "first_input",
        None,
        NOW - 2 * DAY_MS,
        "trace2",
    ),
]

_MESSAGE_ROWS = [
    (
        "msg1",
        "sess_test1",
        NOW - DAY_MS,
        NOW - DAY_MS,
        '{"role":"user","metadata":{"inputIntent":{"text":"hello world"}}}',
        0,
    ),
    (
        "msg2",
        "sess_test1",
        NOW - DAY_MS + 1000,
        NOW - DAY_MS + 1000,
        '{"role":"assistant","modelId":"glm-5.3","providerId":"mimo-x-bridge","finish":"stop"}',
        1,
    ),
]

_PART_ROWS = [
    (
        "part1",
        "msg2",
        "sess_test1",
        NOW - DAY_MS + 1000,
        NOW - DAY_MS + 1000,
        '{"type":"text","text":"Hello! How can I help?"}',
        0,
    ),
    (
        "part2",
        "msg2",
        "sess_test1",
        NOW - DAY_MS + 1001,
        NOW - DAY_MS + 1001,
        '{"type":"tool","callID":"call_1","tool":"Bash","state":{"status":"completed","input":{"command":"ls"}}}',
        1,
    ),
]


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executescript(_SESSION_SCHEMA)
    conn.executemany("INSERT INTO model_usage VALUES (" + ",".join(["?"] * 18) + ")", _MODEL_ROWS)
    conn.executemany("INSERT INTO tool_usage VALUES (" + ",".join(["?"] * 9) + ")", _TOOL_ROWS)
    conn.executemany("INSERT INTO session VALUES (" + ",".join(["?"] * 25) + ")", _SESSION_ROWS)
    conn.executemany("INSERT INTO message VALUES (" + ",".join(["?"] * 6) + ")", _MESSAGE_ROWS)
    conn.executemany("INSERT INTO part VALUES (" + ",".join(["?"] * 7) + ")", _PART_ROWS)
    conn.commit()
    conn.close()


def _ns(**overrides):
    base = {"days": None, "provider": None, "model": None, "session": None, "limit": 20}
    base.update(overrides)
    return argparse.Namespace(**base)


class DatabaseBackup:
    """Swap usage.DB_PATH and restore it afterwards."""

    def __enter__(self):
        self._original = usage.DB_PATH
        return self

    def __exit__(self, *exc):
        usage.DB_PATH = self._original
        return False


class DataLayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"
        _seed(self.db)
        self._backup = DatabaseBackup()
        self._backup.__enter__()
        usage.DB_PATH = self.db

    def tearDown(self):
        self._backup.__exit__()
        self.tmp.cleanup()

    def test_fetch_sections_returns_every_section(self):
        payload = usage_app.fetch_sections(list(usage.ORDER), _ns())
        self.assertEqual(set(payload), set(usage.ORDER))
        overview = payload["overview"]
        cols = overview["columns"]
        # completed + error rows of the same provider/model merge via GROUP BY
        providers = [row[cols.index("provider")] for row in overview["rows"]]
        self.assertEqual(providers, ["other", "mimo-x-bridge"])
        mimo = overview["rows"][1]
        self.assertEqual(mimo[cols.index("reqs")], 2)
        self.assertEqual(mimo[cols.index("errors")], 1)
        cache_read = sum(row[cols.index("cache_read")] for row in overview["rows"])
        self.assertEqual(cache_read, 800)

    def test_days_filter_excludes_old_rows(self):
        payload = usage_app.fetch_sections(["overview"], _ns(days=7))
        self.assertEqual(len(payload["overview"]["rows"]), 1)

    def test_errors_section_returns_the_single_error(self):
        payload = usage_app.fetch_sections(["errors"], _ns())
        section = payload["errors"]
        self.assertEqual(len(section["rows"]), 1)
        self.assertEqual(section["rows"][0][section["columns"].index("error_type")], "rate_limit")

    def test_limit_is_bound_for_sessions(self):
        payload = usage_app.fetch_sections(["sessions"], _ns(limit=1))
        self.assertEqual(len(payload["sessions"]["rows"]), 1)

    def test_missing_database_raises(self):
        usage.DB_PATH = Path(self.tmp.name) / "nope.sqlite"
        with self.assertRaises(FileNotFoundError):
            usage_app.fetch_sections(["overview"], _ns())

    def test_build_keeps_filters_and_limit_bound(self):
        sql, params = usage.build("overview", _ns(days=7, provider="p"))
        self.assertIn("started_at >= ?", sql)
        self.assertEqual(len(params), 2)
        # _filters binds the day cutoff first, then the provider value
        self.assertIsInstance(params[0], int)
        self.assertEqual(params[1], "p")

        sql, params = usage.build("sessions", _ns(limit=5))
        self.assertIn("LIMIT ?", sql)
        self.assertEqual(params[-1], 5)


class FormattingTest(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(usage_app._human_bytes(512), "512 B")
        self.assertEqual(usage_app._human_bytes(2048), "2.0 KB")
        self.assertEqual(usage_app._human_bytes(5 * 1024 * 1024), "5.0 MB")

    def test_fmt_value(self):
        self.assertEqual(usage_app._fmt_value("col", None), "-")
        self.assertEqual(usage_app._fmt_value("col", 123456), "123,456")
        self.assertEqual(usage_app._fmt_value("col", 42), "42")
        self.assertEqual(usage_app._fmt_value("col", "abc"), "abc")
        self.assertEqual(usage_app._fmt_value("out_bytes", 2048), "2.0 KB")


class TableModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _model(self):
        model = usage_app.TableModel()
        model.update_data(
            ["provider", "reqs", "errors"],
            [["b", 3, 0], ["a", 123456, 2], [None, None, 0]],
        )
        return model

    def test_display_formatting(self):
        model = self._model()
        self.assertEqual(model.data(model.index(1, 1), Qt.ItemDataRole.DisplayRole), "123,456")
        # None renders as "-" on purpose (same as the CLI formatter)
        self.assertEqual(model.data(model.index(2, 0), Qt.ItemDataRole.DisplayRole), "-")
        self.assertEqual(model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole), "b")

    def test_numeric_columns_align_right(self):
        model = self._model()
        align = model.data(model.index(0, 1), Qt.ItemDataRole.TextAlignmentRole)
        self.assertIsNotNone(align)
        self.assertTrue(int(align) & int(Qt.AlignmentFlag.AlignRight))
        align = model.data(model.index(0, 0), Qt.ItemDataRole.TextAlignmentRole)
        self.assertIsNone(align)

    def test_error_values_are_highlighted(self):
        model = self._model()
        red = model.data(model.index(1, 2), Qt.ItemDataRole.ForegroundRole)
        self.assertIsInstance(red, type(red) if red is None else red.__class__)
        plain = model.data(model.index(0, 2), Qt.ItemDataRole.ForegroundRole)
        self.assertIsNotNone(red)
        self.assertIsNone(plain)

    def test_sort_puts_none_last(self):
        model = self._model()
        model.sort(1, Qt.SortOrder.AscendingOrder)
        self.assertEqual([row[1] for row in model._rows], [3, 123456, None])
        model.sort(1, Qt.SortOrder.DescendingOrder)
        self.assertEqual([row[1] for row in model._rows], [123456, 3, None])

    def test_long_values_get_tooltips(self):
        model = usage_app.TableModel()
        model.update_data(["sample_message"], [["x" * 100]])
        tip = model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)
        self.assertEqual(tip, "x" * 100)


class SessionBrowserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"
        _seed(self.db)
        self._backup = DatabaseBackup()
        self._backup.__enter__()
        usage.DB_PATH = self.db

    def tearDown(self):
        self._backup.__exit__()
        self.tmp.cleanup()

    def test_fetch_sessions_returns_active_sessions(self):
        sessions = usage_app.fetch_sessions(archived=False)
        self.assertIsInstance(sessions, list)
        if sessions:
            s = sessions[0]
            self.assertIn("id", s)
            self.assertIn("title", s)
            self.assertIn("msg_count", s)
            self.assertIn("total_tokens", s)

    def test_fetch_sessions_archived_filter(self):
        all_sessions = usage_app.fetch_sessions(archived=False)
        archived = usage_app.fetch_sessions(archived=True)
        # seed: sess_test1 active, sess_test2 archived
        self.assertEqual(len(all_sessions), 1)
        self.assertEqual(len(archived), 1)
        self.assertTrue(all(s.get("time_archived") is None for s in all_sessions))
        self.assertTrue(all(s.get("time_archived") is not None for s in archived))

    def test_fetch_session_messages_returns_parts(self):
        sessions = usage_app.fetch_sessions(archived=False, limit=1)
        if not sessions:
            self.skipTest("no sessions in seed data")
        msgs = usage_app.fetch_session_messages(sessions[0]["id"])
        self.assertIsInstance(msgs, list)
        if msgs:
            m = msgs[0]
            self.assertIn("role", m)
            self.assertIn("parts", m)
            self.assertIn("user_text", m)

    def test_fetch_session_messages_missing_db(self):
        usage.DB_PATH = Path(self.tmp.name) / "nope.sqlite"
        with self.assertRaises(FileNotFoundError):
            usage_app.fetch_session_messages("any")

    def test_fetch_sessions_token_count_no_fanout(self):
        """M-1 regression: total_tokens must not inflate from JOIN fan-out.

        Seed: sess_test1 has 2 model_usage rows (1000+200 + 500+0 = 1700 tokens).
        With the old LEFT JOIN this returned 2×1700=3400 due to message fan-out.
        With the fixed subquery it must return exactly 1700.
        """
        sessions = usage_app.fetch_sessions(archived=False)
        self.assertTrue(len(sessions) >= 1)
        s = sessions[0]
        # exact token sum — no fan-out inflation
        self.assertEqual(s["total_tokens"], 1700)
        # msg_count must also be correct (DISTINCT)
        self.assertEqual(s["msg_count"], 2)


if __name__ == "__main__":
    unittest.main()
