# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""zcode-usage: report ZCode model/tool usage from ~/.zcode/cli/db/db.sqlite.

Read-only. The database is opened read-only (file:...?mode=ro) so a report can
never mutate ZCode's data, and EVERY query is fully parameter-bound — including
LIMIT — so no SQL text is ever assembled from user input.

Usage:
  uv run --no-project usage.py                 # overview: totals by provider/model
  uv run --no-project usage.py --days 7        # only the last 7 days
  uv run --no-project usage.py --daily         # per-day trend
  uv run --no-project usage.py --errors        # error breakdown
  uv run --no-project usage.py --latency       # duration + TTFT per model
  uv run --no-project usage.py --cache         # prompt-cache hit rate
  uv run --no-project usage.py --tools         # tool call statistics
  uv run --no-project usage.py --sessions      # per-session totals
  uv run --no-project usage.py --turn          # retries / tool density
  uv run --no-project usage.py --all           # every section
  uv run --no-project usage.py --sql           # print the SQL behind each section
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = (
    Path(os.environ["ZCODE_DB"])
    if os.environ.get("ZCODE_DB")
    else Path(os.environ["USERPROFILE"]) / ".zcode" / "cli" / "db" / "db.sqlite"
)

LOCALTIME = "datetime({col}/1000,'unixepoch','localtime')"
DAY = "date({col}/1000,'unixepoch','localtime')"

TITLES = {
    "overview": "usage by provider / model",
    "daily": "daily trend",
    "errors": "errors",
    "latency": "latency (completed calls, n>=3)",
    "cache": "prompt-cache hit rate",
    "tools": "tool calls",
    "sessions": "sessions (most expensive)",
    "turn": "retries / tool density",
}
ORDER = list(TITLES)

# ---------------------------------------------------------------- queries
# Each builder returns (sql, params). All filters are placeholders bound at
# execution time; LIMIT is a placeholder too.
_Q_OVERVIEW = """
    SELECT m.provider_id AS provider, m.model_id AS model,
           COUNT(*) AS reqs,
           SUM(m.input_tokens) AS in_tok,
           SUM(m.output_tokens) AS out_tok,
           SUM(m.reasoning_tokens) AS reason_tok,
           SUM(m.cache_read_input_tokens) AS cache_read,
           SUM(CASE WHEN m.status='error' THEN 1 ELSE 0 END) AS errors
    FROM model_usage m
    {where}
    GROUP BY m.provider_id, m.model_id
    ORDER BY (SUM(m.input_tokens) + SUM(m.output_tokens)) DESC
"""

_Q_DAILY = """
    SELECT {day} AS day,
           COUNT(*) AS reqs,
           SUM(m.input_tokens) AS in_tok,
           SUM(m.output_tokens) AS out_tok,
           SUM(m.reasoning_tokens) AS reason_tok,
           SUM(CASE WHEN m.status='error' THEN 1 ELSE 0 END) AS errors
    FROM model_usage m
    {where}
    GROUP BY day
    ORDER BY day
"""

_Q_ERRORS = """
    SELECT m.provider_id AS provider, m.model_id AS model,
           COALESCE(m.error_type,'(none)') AS error_type,
           COALESCE(m.error_code,'') AS error_code,
           COUNT(*) AS n,
           MAX(NULLIF(m.error_message,'')) AS sample_message
    FROM model_usage m
    {where}
    GROUP BY m.provider_id, m.model_id, error_type, error_code
    ORDER BY n DESC
"""

_Q_LATENCY = """
    SELECT m.provider_id AS provider, m.model_id AS model,
           COUNT(*) AS n,
           ROUND(AVG(m.duration_ms)) AS avg_ms,
           ROUND(AVG(m.time_to_first_token_ms)) AS avg_ttft_ms,
           MAX(m.duration_ms) AS max_ms,
           SUM(m.tool_call_count) AS tool_calls
    FROM model_usage m
    {where}
    GROUP BY m.provider_id, m.model_id
    HAVING COUNT(*) >= 3
    ORDER BY avg_ms DESC
"""

_Q_CACHE = """
    SELECT m.provider_id AS provider, m.model_id AS model,
           SUM(m.input_tokens) AS total_in,
           SUM(m.cache_read_input_tokens) AS cached,
           SUM(m.cache_creation_input_tokens) AS cache_write,
           ROUND(100.0 * SUM(m.cache_read_input_tokens)
                 / NULLIF(SUM(m.input_tokens), 0), 1) AS hit_pct
    FROM model_usage m
    {where}
    GROUP BY m.provider_id, m.model_id
    ORDER BY total_in DESC
"""

_Q_TOOLS = """
    SELECT t.tool_name AS tool,
           COUNT(*) AS calls,
           SUM(CASE WHEN t.status='error' THEN 1 ELSE 0 END) AS errors,
           ROUND(AVG(t.duration_ms)) AS avg_ms,
           SUM(t.output_bytes) AS out_bytes,
           SUM(t.read_only) AS read_only_calls,
           SUM(t.destructive) AS destructive_calls
    FROM tool_usage t
    {where}
    GROUP BY t.tool_name
    ORDER BY calls DESC
"""

_Q_SESSIONS = """
    SELECT m.session_id AS session,
           COUNT(*) AS reqs,
           SUM(m.input_tokens) AS in_tok,
           SUM(m.output_tokens) AS out_tok,
           MIN({localtime}) AS first_at,
           MAX({localtime}) AS last_at
    FROM model_usage m
    {where}
    GROUP BY m.session_id
    ORDER BY (SUM(m.input_tokens) + SUM(m.output_tokens)) DESC
    LIMIT ?
"""

_Q_TURN = """
    SELECT m.provider_id AS provider, m.model_id AS model,
           COUNT(*) AS turns,
           SUM(m.retry_count) AS retries,
           ROUND(AVG(m.tool_call_count), 2) AS avg_tools_per_turn,
           SUM(m.context_exceeded) AS ctx_exceeded
    FROM model_usage m
    {where}
    GROUP BY m.provider_id, m.model_id
    HAVING COUNT(*) >= 3
    ORDER BY SUM(m.retry_count) DESC
"""


def _filters(args, alias="m", status=None):
    """Parameterised WHERE for model_usage / tool_usage."""
    parts, params = [], []
    if status:
        parts.append(f"{alias}.status = ?")
        params.append(status)
    if args.days:
        parts.append(f"{alias}.started_at >= ?")
        params.append(int((time.time() - args.days * 86400) * 1000))
    if args.provider:
        parts.append(f"{alias}.provider_id = ?")
        params.append(args.provider)
    if args.model:
        parts.append(f"{alias}.model_id = ?")
        params.append(args.model)
    if args.session:
        parts.append(f"{alias}.session_id = ?")
        params.append(args.session)
    return ("WHERE " + " AND ".join(parts)) if parts else "", params


def build(name, args):
    """Return (sql, params) for a section."""
    if name == "overview":
        w, p = _filters(args)
        return _Q_OVERVIEW.format(where=w), p
    if name == "daily":
        w, p = _filters(args)
        return _Q_DAILY.format(where=w, day=DAY.format(col="m.started_at")), p
    if name == "errors":
        w, p = _filters(args, status="error")
        return _Q_ERRORS.format(where=w), p
    if name == "latency":
        w, p = _filters(args, status="completed")
        return _Q_LATENCY.format(where=w), p
    if name == "cache":
        w, p = _filters(args)
        return _Q_CACHE.format(where=w), p
    if name == "tools":
        # tool_usage has no provider/model columns — filter on session/days only
        tool_args = argparse.Namespace(
            days=args.days, session=args.session, provider=None, model=None
        )
        w, p = _filters(tool_args, alias="t")
        return _Q_TOOLS.format(where=w), p
    if name == "sessions":
        w, p = _filters(args)
        p = p + [args.limit]
        return _Q_SESSIONS.format(where=w, localtime=LOCALTIME.format(col="m.started_at")), p
    if name == "turn":
        w, p = _filters(args)
        return _Q_TURN.format(where=w), p
    raise KeyError(name)


# ---------------------------------------------------------------- output
def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, int) and abs(v) >= 10000:
        return f"{v:,}"
    return str(v)


def render(rows, title):
    if not rows:
        print(f"\n=== {title} ===\n  (no data)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(str(c)), *(len(fmt(r[c])) for r in rows)) for c in cols}
    head = "  ".join(str(c).ljust(widths[c]) for c in cols)
    print(f"\n=== {title} ===")
    print(head)
    print("-" * len(head))
    for r in rows:
        print("  ".join(fmt(r[c]).ljust(widths[c]) for c in cols))


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"database not found: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, help="only the last N days")
    ap.add_argument("--provider", help="filter by provider_id")
    ap.add_argument("--model", help="filter by model_id")
    ap.add_argument("--session", help="filter by session_id")
    ap.add_argument("--limit", type=int, default=20, help="row limit for --sessions (default 20)")
    for name in ORDER:
        ap.add_argument(f"--{name}", action="store_true", help=f"show: {TITLES[name]}")
    ap.add_argument("--all", action="store_true", help="show every section")
    ap.add_argument("--sql", action="store_true", help="print the SQL instead of running it")
    ap.add_argument("--db", help="override database path")
    args = ap.parse_args()

    global DB_PATH
    if args.db:
        DB_PATH = Path(args.db)

    chosen = [n for n in ORDER if getattr(args, n)] or (ORDER if args.all else ["overview"])

    if args.sql:
        for n in chosen:
            sql, params = build(n, args)
            print(f"-- {TITLES[n]}")
            print(sql.strip())
            if params:
                print(f"-- bound params: {params}")
            print()
        return 0

    scope = []
    if args.days:
        scope.append(f"last {args.days}d")
    if args.provider:
        scope.append(f"provider={args.provider}")
    if args.model:
        scope.append(f"model={args.model}")
    if args.session:
        scope.append(f"session={args.session}")

    print(f"db: {DB_PATH}")
    print(f"scope: {', '.join(scope) if scope else 'all time'}")

    conn = connect(DB_PATH)
    try:
        for n in chosen:
            sql, params = build(n, args)
            render(conn.execute(sql, params).fetchall(), TITLES[n])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
