# zcode-usage

Query and report **ZCode's own model/tool usage** from its local SQLite database.

Read-only: the database is opened with `?mode=ro`, and every query is fully
parameter-bound (including `LIMIT`), so a report can never read-tamper or mutate
ZCode's data.

## Quick start

```powershell
uv run --no-project usage.py              # usage by provider / model
uv run --no-project usage.py --all        # every section
uv run --no-project usage.py --days 7     # last 7 days only
```

## Desktop app

`usage_app.py` serves the same read-only reports as a native desktop panel
(**PySide6 / Qt**, the project's only third-party dependency — the CLI itself
stays zero-dependency):

```powershell
uv sync                        # one-time: install PySide6
uv run usage_app.py            # open the desktop panel
uv run usage_app.py --db PATH  # other database (or set ZCODE_DB)
```

Eight tabs (one per section), a daily-trend bar chart (QtCharts), clickable
column sorting, filters (days / provider / model / session), a per-tab row
limit, a 30s auto-refresh toggle and a dark theme. Queries run on a worker
thread; every filter is bound as a SQL parameter and the database stays
read-only — same guarantees as the CLI.

### Session browser

The **会话浏览** tab (rightmost) is a CC-switch-style conversation browser:

- **Left panel** — session list with 活跃 / 归档 toggle buttons, a text
  filter (title / directory), and per-session metadata (time, task type,
  message count, token total). Most recently updated first.
- **Right panel** — the full conversation context of the selected session:
  user messages (from `message.data.metadata.inputIntent.text`), assistant
  text blocks, reasoning, tool calls (name, status, input), step/token
  summaries, compaction events and errors — rendered as a readable chat
  transcript with dark-theme HTML.

Both panels load lazily on tab switch; message threads load on session
click, each on its own worker thread.

## Options

| Flag | Meaning |
|---|---|
| `--days N` | limit to the last N days |
| `--provider ID` | filter by `provider_id` (e.g. `mimo-x-bridge`) |
| `--model ID` | filter by `model_id` |
| `--session ID` | filter by `session_id` |
| `--limit N` | row limit for `--sessions` (default 20) |
| `--db PATH` | override the database path (or set `ZCODE_DB`) |

## Sections

| Section | What it shows |
|---|---|
| `--overview` | requests, input/output/reasoning tokens, cache reads, errors per provider+model |
| `--daily` | per-day request and token trend |
| `--errors` | failures by error type/code with a sample message |
| `--latency` | average duration, time-to-first-token (TTFT), max duration |
| `--cache` | prompt-cache read share of input tokens (cost indicator) |
| `--tools` | tool call counts, failures, avg duration, bytes, read-only/destructive |
| `--sessions` | per-session token totals, most expensive first |
| `--turn` | retries and tool-call density per turn |

`--sql` prints the SQL behind the chosen sections (with bound params) instead of
running them — handy for pasting into `sqlite3` or a BI tool.

## Database

Default path: `%USERPROFILE%\.zcode\cli\db\db.sqlite`

Relevant tables (all timestamps are **milliseconds** since epoch):

- `model_usage` — one row per LLM call: `provider_id`, `model_id`, `status`,
  `input_tokens`, `output_tokens`, `reasoning_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens`, `duration_ms`, `time_to_first_token_ms`,
  `tool_call_count`, `error_type`, `error_code`
- `turn_usage` — per-turn aggregate
- `tool_usage` — one row per tool call: `tool_name`, `status`, `duration_ms`,
  `exit_code`, `output_bytes`, `read_only`, `destructive`

Convert a timestamp in raw SQL with:

```sql
datetime(started_at/1000, 'unixepoch', 'localtime')
```

## Raw sqlite3 usage

Note that SQL must be passed as a **string argument** to `sqlite3`; typing it
straight into a PowerShell prompt makes PowerShell try to parse it as cmdlets.

```powershell
sqlite3 -header -column "C:\Users\<you>\.zcode\cli\db\db.sqlite" `
  "SELECT provider_id, model_id, COUNT(*) FROM model_usage GROUP BY 1,2;"
```

## Development

Lint and format (config in `pyproject.toml`, ruff pinned as a dev dependency):

```bash
uv run ruff check .
uv run ruff format --check .
```

Tests (throwaway DB, Qt runs offscreen):

```bash
uv run --no-project python -m unittest discover -s tests -t .
```

## Notes

- Provider ids come from your client config; local bridge providers appear as
  their configured id (e.g. `mimo-x-bridge`, `workbuddyai-bridge`).
- `reasoning_tokens` is only populated by providers that report it.
- A `hit_pct` of `0.0` means the upstream reported no cache reads for that
  provider, not that caching failed.
