# AGENTS.md — Development Standards

This document defines the standards for all contributors (human and AI agents) working on **ai-observability-bot**.

---

## Package Management

Use **`uv`** exclusively. Never use `pip` directly.

```bash
uv sync --dev          # install all deps incl. dev
uv add <package>       # add runtime dependency
uv add --dev <package> # add dev-only dependency
uv run <command>       # run any command in the venv
```

---

## Code Style

### Formatter & Linter — Ruff

Run after **every** set of changes:

```bash
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
```

Configuration lives in `pyproject.toml` (`[tool.ruff]`):
- Line length: **100**
- Target: **Python 3.12+**
- Active rule sets: `E`, `F`, `I` (isort), `UP` (pyupgrade)

### Key Rules

| Rule | Policy |
|---|---|
| `from __future__ import annotations` | **Do not use** — Python 3.14 handles this natively via PEP 649 |
| Type unions | Use `X \| Y` (native), not `Optional[X]` or `Union[X, Y]` |
| `timezone.utc` | Use `datetime.UTC` (UP017) |
| f-strings without placeholders | Remove `f` prefix (F541) |
| Long lines in multiline string constants | Suppress with `# noqa: E501` on the assignment line |
| Imports | Sorted by ruff/isort automatically — never reorder manually |

### Docstrings

Use Google-style docstrings with `Args:` / `Returns:` sections.
The `mcp/registry.py` auto-generates OpenAI tool schemas from these — keep them accurate.

```python
async def query_prometheus(cluster: str, expr: str) -> dict:
    """
    Run an instant PromQL query against the given cluster.

    Args:
        cluster: Cluster name matching clusters.yml key.
        expr: PromQL expression.

    Returns:
        Prometheus API response dict.
    """
```

---

## Project Structure

```
src/srebot/
├── config.py          # Settings (pydantic-settings) + ClusterRegistry
├── parser/            # Telegram message → Alert objects
├── state/             # Redis dedup store
├── mcp/               # LLM tool functions + schema builder
├── llm/               # Prompts + agentic tool-call loop
└── bot/               # Telegram handlers + entry point
```

### Adding a New MCP Tool

1. Add an `async def` function to `mcp/tools.py` with a Google-style docstring.
2. Add it to `_TOOL_FUNCTIONS` list in `mcp/registry.py`.
3. The schema is auto-generated — no manual JSON schema needed.
4. Add a unit or integration test.

---

## Testing

### Running Tests

```bash
uv run pytest tests/ -v        # verbose
uv run pytest tests/ -q        # quiet (CI)
uv run pytest tests/ -x        # stop on first failure
```

### Standards

- Framework: **pytest** + **pytest-asyncio** (mode: `auto`)
- All async tests work without `@pytest.mark.asyncio` — it's applied automatically
- Use `pytest-mock` / `unittest.mock.AsyncMock` for external dependencies (Redis, HTTP, LLM)
- **Never** make real network calls in unit tests
- Test file naming: `test_<module_name>.py`
- Fixtures go in `conftest.py` (shared) or at top of test file (local)

### Coverage Expectations

| Module | What to test |
|---|---|
| `parser/alert_parser.py` | Parsing, edge cases, multi-alert, firing/resolved, fingerprint stability |
| `state/store.py` | All dedup state transitions with mock Redis |
| `mcp/tools.py` | Happy path + cluster-not-found error with mock `httpx` |
| `llm/agent.py` | Tool-call loop with mock OpenAI client |

---

## Git Workflow

- All changes must pass `ruff check` and `pytest` before commit
- Commit messages: imperative mood, present tense (`Add ES log search tool`, not `Added...`)

---

## Environment & Configuration

- Copy `.env.example` → `.env` and fill in real values (never commit `.env`)
- Add clusters to `clusters.yml` — keys must exactly match the `cluster` label in Prometheus alerts
- `ALERT_FINGERPRINT_TTL` controls how long a firing alert is deduplicated (default: 24 h)
