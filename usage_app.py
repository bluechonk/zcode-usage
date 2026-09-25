"""zcode-usage app: a PySide6 desktop UI over usage.py's read-only reports.

One native window, eight tabs (one per usage.py section), a filter bar and a
30s auto-refresh toggle. Queries reuse usage.build() unchanged — every filter
is bound as a SQL parameter and the database stays read-only (?mode=ro).
Queries run on a worker QThread so the UI never blocks on SQLite.

Usage:
  uv run usage_app.py              # open the desktop panel
  uv run usage_app.py --db PATH    # other database (or set ZCODE_DB)
"""

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QValueAxis,
)
from PySide6.QtCore import QAbstractTableModel, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

import usage

TAB_LABELS = [
    ("overview", "概览"),
    ("daily", "每日趋势"),
    ("errors", "错误"),
    ("latency", "延迟"),
    ("cache", "缓存命中率"),
    ("tools", "工具调用"),
    ("sessions", "会话"),
    ("turn", "轮次与重试"),
    ("browser", "会话浏览"),
]


# ---------------------------------------------------------------- session browser
def fetch_sessions(archived: bool = False, limit: int = 200) -> list[dict]:
    """Return session metadata rows, most recently updated first."""
    if not usage.DB_PATH.exists():
        raise FileNotFoundError(str(usage.DB_PATH))
    conn = usage.connect(usage.DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.title, s.directory, s.time_created, s.time_updated,
                   s.time_archived, s.task_type, s.parent_id,
                   (SELECT COUNT(*) FROM message WHERE session_id = s.id) AS msg_count,
                   (SELECT COALESCE(SUM(input_tokens + output_tokens), 0)
                    FROM model_usage WHERE session_id = s.id) AS total_tokens
            FROM session s
            WHERE s.time_archived IS {} NULL
            ORDER BY s.time_updated DESC
            LIMIT ?
            """.format("NOT" if archived else ""),
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_session_messages(session_id: str) -> list[dict]:
    """Return messages for a session with their parts, in conversation order."""
    if not usage.DB_PATH.exists():
        raise FileNotFoundError(str(usage.DB_PATH))
    conn = usage.connect(usage.DB_PATH)
    try:
        msgs = conn.execute(
            "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
        parts = conn.execute(
            "SELECT message_id, data, sequence FROM part WHERE session_id = ? ORDER BY sequence, time_created",
            (session_id,),
        ).fetchall()
        parts_by_msg: dict[str, list[dict]] = {}
        for p in parts:
            parts_by_msg.setdefault(p["message_id"], []).append(json.loads(p["data"]))
        result = []
        for m in msgs:
            data = json.loads(m["data"])
            user_text = None
            if data.get("role") == "user":
                user_text = (data.get("metadata", {}).get("inputIntent") or {}).get("text")
            result.append(
                {
                    "id": m["id"],
                    "role": data.get("role"),
                    "time_created": m["time_created"],
                    "model": data.get("modelId") or data.get("providerId"),
                    "user_text": user_text,
                    "parts": parts_by_msg.get(m["id"], []),
                    "finish": data.get("finish"),
                    "error": data.get("error"),
                }
            )
        return result
    finally:
        conn.close()


class FetchSessionsThread(QThread):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, archived: bool, parent=None):
        super().__init__(parent)
        self._archived = archived

    def run(self):
        try:
            self.done.emit(fetch_sessions(archived=self._archived))
        except (FileNotFoundError, SystemExit):
            self.failed.emit(f"database not found:\n{usage.DB_PATH}")
        except Exception:
            traceback.print_exc()
            self.failed.emit("query failed (details on console)")


class FetchMessagesThread(QThread):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, session_id: str, parent=None):
        super().__init__(parent)
        self._session_id = session_id

    def run(self):
        try:
            self.done.emit(fetch_session_messages(self._session_id))
        except (FileNotFoundError, SystemExit):
            self.failed.emit(f"database not found:\n{usage.DB_PATH}")
        except Exception:
            traceback.print_exc()
            self.failed.emit("query failed (details on console)")


class SessionListWidget(QWidget):
    """Left panel: filterable session list with active/archived tabs."""

    session_selected = Signal(str)  # session id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sessions: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        toggle_bar = QHBoxLayout()
        self.active_btn = QPushButton("活跃")
        self.active_btn.setCheckable(True)
        self.active_btn.setChecked(True)
        self.archived_btn = QPushButton("已归档")
        self.archived_btn.setCheckable(True)
        self.active_btn.clicked.connect(lambda: self._show_active())
        self.archived_btn.clicked.connect(lambda: self._show_archived())
        toggle_bar.addWidget(self.active_btn)
        toggle_bar.addWidget(self.archived_btn)
        toggle_bar.addStretch(1)
        layout.addLayout(toggle_bar)

        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("搜索标题 / 目录…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        filter_bar.addWidget(self.filter_edit)
        self.scope_label = QLabel("活跃会话")
        filter_bar.addWidget(self.scope_label)
        layout.addLayout(filter_bar)

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget)

        self.count_label = QLabel("共 0 个会话")
        layout.addWidget(self.count_label)

    def _show_active(self):
        if self.active_btn.isChecked() and not self.archived_btn.isChecked():
            return  # already active, skip
        self.active_btn.setChecked(True)
        self.archived_btn.setChecked(False)
        self.scope_label.setText("活跃会话")
        self.refresh_requested.emit(False)

    def _show_archived(self):
        if self.archived_btn.isChecked() and not self.active_btn.isChecked():
            return  # already archived, skip
        self.active_btn.setChecked(False)
        self.archived_btn.setChecked(True)
        self.scope_label.setText("已归档会话")
        self.refresh_requested.emit(True)

    refresh_requested = Signal(bool)  # archived

    def set_sessions(self, sessions: list[dict]):
        self._sessions = sessions
        self._apply_filter()

    def _apply_filter(self):
        text = self.filter_edit.text().strip().lower()
        filtered = [
            s
            for s in self._sessions
            if not text
            or text in (s.get("title") or "").lower()
            or text in (s.get("directory") or "").lower()
        ]
        self.list_widget.clear()
        for s in filtered:
            item = QListWidget()
            title = s.get("title") or "(无标题)"
            ts = datetime.fromtimestamp(s["time_updated"] / 1000).strftime("%m-%d %H:%M")
            task = s.get("task_type", "interactive")
            msg_n = s.get("msg_count", 0)
            tok_n = s.get("total_tokens", 0)
            label = f"{title}\n{ts} · {task} · {msg_n} 条消息 · {tok_n:,} tokens"
            item.setText(label)
            item.setData(Qt.ItemDataRole.UserRole, s["id"])
            self.list_widget.addItem(item)
        self.count_label.setText(f"共 {len(filtered)} 个会话")

    def _on_item_clicked(self, item):
        self.session_selected.emit(item.data(Qt.ItemDataRole.UserRole))


class MessageThreadWidget(QWidget):
    """Right panel: renders the full conversation context of a session."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel("← 选择一个会话查看完整上下文")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setVisible(False)
        layout.addWidget(self.scroll)
        self.content = QLabel()
        self.content.setWordWrap(True)
        self.content.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.content.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self.content)

    def set_messages(self, messages: list[dict]):
        if not messages:
            self.label.setText("← 选择一个会话查看完整上下文")
            self.label.setVisible(True)
            self.scroll.setVisible(False)
            return
        self.label.setVisible(False)
        self.scroll.setVisible(True)
        self.content.setText(self._render_html(messages))

    def _render_html(self, messages: list[dict]) -> str:
        blocks = []
        for msg in messages:
            ts = datetime.fromtimestamp(msg["time_created"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            if msg["role"] == "user":
                text = msg.get("user_text") or "(空消息)"
                blocks.append(
                    f'<div class="msg user"><div class="meta">👤 用户 · {ts}</div>'
                    f'<div class="body">{_escape(text)}</div></div>'
                )
            elif msg["role"] == "assistant":
                parts_html = []
                for part in msg.get("parts", []):
                    ptype = part.get("type")
                    if ptype == "text":
                        parts_html.append(
                            f'<div class="body">{_escape(part.get("text", ""))}</div>'
                        )
                    elif ptype == "reasoning":
                        parts_html.append(
                            f'<div class="reasoning">💭 {_escape(part.get("text", ""))}</div>'
                        )
                    elif ptype == "tool":
                        tool_name = part.get("tool", "?")
                        status = part.get("state", {}).get("status", "?")
                        title = part.get("title", tool_name)
                        inp = json.dumps(part.get("state", {}).get("input", {}), ensure_ascii=False)
                        if len(inp) > 300:
                            inp = inp[:300] + "…"
                        status_icon = {"completed": "✅", "error": "❌", "running": "🔄"}.get(
                            status, "❓"
                        )
                        parts_html.append(
                            f'<div class="tool">🔧 {_escape(title)} '
                            f'<span class="status">{status_icon} {status}</span>'
                            f'<pre class="input">{_escape(inp)}</pre></div>'
                        )
                    elif ptype == "step-finish":
                        tokens = part.get("tokens", {}).get("total", 0)
                        parts_html.append(f'<div class="step">↳ 步骤完成 · {tokens:,} tokens</div>')
                    elif ptype == "compaction":
                        parts_html.append(
                            f'<div class="compaction">📦 上下文压缩 · {_escape(part.get("compactReason", ""))}</div>'
                        )
                if msg.get("error"):
                    err = msg["error"]
                    parts_html.append(
                        f'<div class="error">❌ 错误：{_escape(err.get("name", ""))} — '
                        f"{_escape(err.get('data', {}).get('message', ''))}</div>"
                    )
                finish = msg.get("finish", "")
                meta = f"🤖 助手 · {ts} · {msg.get('model', '')} · {finish}"
                blocks.append(
                    f'<div class="msg assistant"><div class="meta">{meta}</div>{"".join(parts_html)}</div>'
                )
            else:
                blocks.append(
                    f'<div class="msg other"><div class="meta">? {msg["role"]} · {ts}</div></div>'
                )
        return _THREAD_TEMPLATE.format(body="".join(blocks))


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    )


_THREAD_TEMPLATE = """\
<html><head><style>
body {{ font-family: "Segoe UI", "PingFang SC", sans-serif; font-size: 13px;
       background: #161a22; color: #e6e9ef; padding: 12px; }}
.msg {{ margin: 0 0 14px 0; padding: 10px 12px; border-radius: 8px; }}
.user {{ background: #1a2535; }}
.assistant {{ background: #1a1f29; }}
.meta {{ color: #8b94a7; font-size: 11px; margin-bottom: 6px; }}
.body {{ white-space: pre-wrap; word-break: break-word; }}
.reasoning {{ color: #a0aec0; font-style: italic; margin: 4px 0; white-space: pre-wrap; }}
.tool {{ background: #0f1115; border: 1px solid #232a36; border-radius: 6px;
         padding: 8px 10px; margin: 6px 0; }}
.tool .status {{ color: #8b94a7; font-size: 11px; margin-left: 8px; }}
.tool .input {{ color: #a0aec0; font-size: 11px; margin: 4px 0 0 0;
                white-space: pre-wrap; word-break: break-all; }}
.step {{ color: #37d0a2; font-size: 11px; margin: 2px 0; }}
.compaction {{ color: #ffb454; font-size: 11px; margin: 4px 0; }}
.error {{ color: #ff6b6b; }}
</style></head><body>{body}</body></html>
"""


class SessionBrowserWidget(QWidget):
    """Full browser: left session list + right conversation thread."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.session_list = SessionListWidget()
        self.session_list.setFixedWidth(380)
        layout.addWidget(self.session_list)

        self.thread_view = MessageThreadWidget()
        layout.addWidget(self.thread_view, 1)

        self.session_list.session_selected.connect(self._load_session)
        self.session_list.refresh_requested.connect(self._reload_sessions)
        self._sessions_worker: FetchSessionsThread | None = None
        self._messages_worker: FetchMessagesThread | None = None

    def refresh(self):
        self.session_list.scope_label.setText("活跃会话")
        self._reload_sessions(archived=False)

    def refresh_archived(self):
        self.session_list.scope_label.setText("已归档会话")
        self._reload_sessions(archived=True)

    def _reload_sessions(self, archived: bool):
        if self._sessions_worker is not None and self._sessions_worker.isRunning():
            return
        if self._sessions_worker is not None:
            self._sessions_worker.done.disconnect(self.session_list.set_sessions)
        self._sessions_worker = FetchSessionsThread(archived=archived, parent=self)
        self._sessions_worker.done.connect(self.session_list.set_sessions)
        self._sessions_worker.start()

    def _load_session(self, session_id: str):
        if self._messages_worker is not None and self._messages_worker.isRunning():
            return
        if self._messages_worker is not None:
            self._messages_worker.done.disconnect(self.thread_view.set_messages)
        self.thread_view.label.setText("加载中…")
        self.thread_view.label.setVisible(True)
        self.thread_view.scroll.setVisible(False)
        self._messages_worker = FetchMessagesThread(session_id, parent=self)
        self._messages_worker.done.connect(self.thread_view.set_messages)
        self._messages_worker.start()


# ---------------------------------------------------------------- data layer
def fetch_sections(names, args):
    """Run usage.build() for each section, return {name: {"columns", "rows"}}."""
    if not usage.DB_PATH.exists():
        raise FileNotFoundError(str(usage.DB_PATH))
    conn = usage.connect(usage.DB_PATH)
    try:
        out = {}
        for name in names:
            sql, params = usage.build(name, args)
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            out[name] = {"columns": cols, "rows": [list(row) for row in cur.fetchall()]}
        return out
    finally:
        conn.close()


class FetchThread(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, names, args, parent=None):
        super().__init__(parent)
        self._names, self._args = names, args

    def run(self):
        try:
            self.done.emit(fetch_sections(self._names, self._args))
        except (FileNotFoundError, SystemExit):
            self.failed.emit(f"database not found:\n{usage.DB_PATH}")
        except Exception:
            traceback.print_exc()
            self.failed.emit("query failed (details on console)")


# ---------------------------------------------------------------- formatting
def _human_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    for i, unit in enumerate(units):
        if size < 1024 or i == len(units) - 1:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _fmt_value(col: str, value) -> str:
    if value is None:
        return "-"
    if col == "out_bytes" and isinstance(value, int):
        return _human_bytes(value)
    if isinstance(value, int) and abs(value) >= 10000:
        return f"{value:,}"
    return str(value)


# ---------------------------------------------------------------- table model
class TableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns: list = []
        self._rows: list = []
        self._numeric: list = []

    def update_data(self, columns, rows):
        self.beginResetModel()
        self._columns = list(columns)
        self._rows = [list(row) for row in rows]
        self._numeric = [
            all(v is None or isinstance(v, (int, float)) for v in (row[i] for row in self._rows))
            for i in range(len(self._columns))
        ]
        self.endResetModel()

    def rowCount(self, parent=None):
        return 0 if (parent is not None and parent.isValid()) else len(self._rows)

    def columnCount(self, parent=None):
        return 0 if (parent is not None and parent.isValid()) else len(self._columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        col = self._columns[index.column()]
        value = self._rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return _fmt_value(col, value)
        if role == Qt.ItemDataRole.TextAlignmentRole and self._numeric[index.column()]:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            text = _fmt_value(col, value)
            return text if len(text) > 60 else None
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and col == "errors"
            and isinstance(value, int)
            and value > 0
        ):
            return QColor("#ff6b6b")
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self._columns)
        ):
            return self._columns[section]
        return None

    def sort(self, column, order):
        if not 0 <= column < len(self._columns):
            return
        self.layoutAboutToBeChanged.emit()
        descending = order == Qt.SortOrder.DescendingOrder
        non_null = sorted(
            (row for row in self._rows if row[column] is not None),
            key=lambda row: row[column],
            reverse=descending,
        )
        nulls = [row for row in self._rows if row[column] is None]
        self._rows = non_null + nulls  # None sorts last regardless of direction
        self.layoutChanged.emit()


# ---------------------------------------------------------------- chart
def _build_daily_chart(rows):
    set_reqs, set_errors = QBarSet("requests"), QBarSet("errors")
    set_reqs.setColor(QColor("#4f8cff"))
    set_errors.setColor(QColor("#ff6b6b"))
    for row in rows:
        set_reqs.append(row[1] or 0)
        set_errors.append(row[5] or 0)

    series = QBarSeries()
    series.append(set_reqs)
    series.append(set_errors)

    chart = QChart()
    chart.addSeries(series)
    chart.setTheme(QChart.ChartTheme.ChartThemeDark)
    chart.setTitle("每日请求量 / 错误数")
    chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)

    axis_x = QBarCategoryAxis()
    axis_x.append([str(row[0])[-5:] for row in rows])
    axis_y = QValueAxis()
    peak = max((row[1] or 0) for row in rows) or 1
    axis_y.setRange(0, peak * 1.15)
    axis_y.applyNiceNumbers()
    chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
    chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
    series.attachAxis(axis_x)
    series.attachAxis(axis_y)

    def hover(status: bool, index: int):
        if status:
            row = rows[index]
            QToolTip.showText(
                QCursor.pos(),
                f"{row[0]}：{row[1]} 次请求，错误 {row[5] or 0}",
            )

    set_reqs.hovered.connect(hover)
    return chart


# ---------------------------------------------------------------- main window
def _scope_text(args) -> str:
    parts = []
    if args.days:
        parts.append(f"last {args.days}d")
    if args.provider:
        parts.append(f"provider={args.provider}")
    if args.model:
        parts.append(f"model={args.model}")
    if args.session:
        parts.append(f"session={args.session}")
    return ", ".join(parts) or "all time"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ZCode 用量面板")
        self._worker: FetchThread | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.days = QComboBox()
        for label, value in [
            ("全部时间", None),
            ("最近 1 天", 1),
            ("最近 3 天", 3),
            ("最近 7 天", 7),
            ("最近 14 天", 14),
            ("最近 30 天", 30),
            ("最近 90 天", 90),
        ]:
            self.days.addItem(label, value)
        self.provider = QLineEdit()
        self.provider.setPlaceholderText("provider_id")
        self.model = QLineEdit()
        self.model.setPlaceholderText("model_id")
        self.session = QLineEdit()
        self.session.setPlaceholderText("session_id")
        self.limit = QSpinBox()
        self.limit.setRange(1, 500)
        self.limit.setValue(20)
        self.limit.setPrefix("会话≤")
        self.limit.setToolTip("sessions 分区的行数上限")
        self.auto = QCheckBox("30s 自动刷新")
        self.refresh_btn = QPushButton("刷新")
        for widget in (
            self.days,
            self.provider,
            self.model,
            self.session,
            self.limit,
            self.refresh_btn,
            self.auto,
        ):
            bar.addWidget(widget)
        bar.addStretch(1)
        root.addLayout(bar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.models: dict[str, TableModel] = {}
        self.chart_view: QChartView | None = None
        self.browser: SessionBrowserWidget | None = None
        self._browser_loaded = False
        for key, label in TAB_LABELS:
            if key == "browser":
                self.browser = SessionBrowserWidget()
                self.tabs.addTab(self.browser, label)
                continue
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            if key == "daily":
                self.chart_view = QChartView()
                self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
                self.chart_view.setMinimumHeight(190)
                layout.addWidget(self.chart_view)
            view = QTableView()
            model = TableModel()
            view.setModel(model)
            view.setSortingEnabled(True)
            view.verticalHeader().setVisible(False)
            view.setAlternatingRowColors(True)
            view.horizontalHeader().setStretchLastSection(True)
            view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            layout.addWidget(view)
            self.tabs.addTab(page, label)
            self.models[key] = model

        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self.refresh)
        self.auto.toggled.connect(self._toggle_auto)
        self.refresh_btn.clicked.connect(self.refresh)
        for line_edit in (self.provider, self.model, self.session):
            line_edit.returnPressed.connect(self.refresh)

        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.statusBar().showMessage(f"db: {usage.DB_PATH}")
        self.refresh()

    def _on_tab_changed(self, index: int):
        if (
            self.browser is not None
            and not self._browser_loaded
            and self.tabs.widget(index) is self.browser
        ):
            self._browser_loaded = True
            self.browser.refresh()

    def _toggle_auto(self, checked: bool):
        if checked:
            self.timer.start()
        else:
            self.timer.stop()

    def _current_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            days=self.days.currentData(),
            provider=self.provider.text().strip() or None,
            model=self.model.text().strip() or None,
            session=self.session.text().strip() or None,
            limit=self.limit.value(),
        )

    def refresh(self):
        if self._worker is not None and self._worker.isRunning():
            return
        args = self._current_args()
        self.statusBar().showMessage(
            f"db: {usage.DB_PATH}　·　scope: {_scope_text(args)}　·　加载中…"
        )
        self.refresh_btn.setEnabled(False)
        self._worker = FetchThread(list(usage.ORDER), args, self)
        self._worker.done.connect(self._apply)
        self._worker.failed.connect(self._fail)
        self._worker.finished.connect(self._worker_cleanup)
        self._worker.start()

    def _worker_cleanup(self):
        self.refresh_btn.setEnabled(True)
        self._worker = None

    def _apply(self, payload: dict):
        for key, section in payload.items():
            self.models[key].update_data(section["columns"], section["rows"])
            if key == "daily" and self.chart_view is not None:
                old = self.chart_view.chart()
                if section["rows"]:
                    self.chart_view.setChart(_build_daily_chart(section["rows"]))
                else:
                    empty = QChart()
                    empty.setTitle("(暂无数据)")
                    self.chart_view.setChart(empty)
                if old is not None:
                    old.deleteLater()
        self.statusBar().showMessage(f"db: {usage.DB_PATH}　·　更新于 {datetime.now():%H:%M:%S}")

    def _fail(self, message: str):
        self.statusBar().showMessage(f"db: {usage.DB_PATH}")
        QMessageBox.warning(self, "加载失败", message)


# ---------------------------------------------------------------- dark theme
def _apply_dark_palette(app: QApplication):
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(0x0F, 0x11, 0x15))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(0xE6, 0xE9, 0xEF))
    palette.setColor(QPalette.ColorRole.Base, QColor(0x16, 0x1A, 0x22))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(0x1A, 0x1F, 0x29))
    palette.setColor(QPalette.ColorRole.Text, QColor(0xE6, 0xE9, 0xEF))
    palette.setColor(QPalette.ColorRole.Button, QColor(0x1A, 0x1F, 0x29))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(0xE6, 0xE9, 0xEF))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(0x4F, 0x8C, 0xFF))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(0x1A, 0x1F, 0x29))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(0xE6, 0xE9, 0xEF))
    disabled = QPalette.ColorGroup.Disabled
    muted = QColor(0x6B, 0x72, 0x82)
    palette.setColor(disabled, QPalette.ColorRole.Text, muted)
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, muted)
    palette.setColor(disabled, QPalette.ColorRole.WindowText, muted)
    app.setPalette(palette)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", help="override database path (or set ZCODE_DB)")
    cli = ap.parse_args()
    if cli.db:
        usage.DB_PATH = Path(cli.db)
    if not usage.DB_PATH.exists():
        print(f"database not found: {usage.DB_PATH}", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    _apply_dark_palette(app)
    window = MainWindow()
    window.resize(1280, 820)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
