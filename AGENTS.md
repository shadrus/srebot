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
- Target: **Python 3.14+**
- Active rule sets: `E`, `F`, `I` (isort), `UP` (pyupgrade)

### Key Rules

| Rule                                     | Policy                                                         |
| ---------------------------------------- | -------------------------------------------------------------- |
| `from __future__ import annotations`     | **Do not use** — Python 3.14 handles this natively via PEP 649 |
| Type unions                              | Use `X \| Y` (native), not `Optional[X]` or `Union[X, Y]`      |
| `timezone.utc`                           | Use `datetime.UTC` (UP017)                                     |
| f-strings without placeholders           | Remove `f` prefix (F541)                                       |
| Long lines in multiline string constants | Suppress with `# noqa: E501` on the assignment line            |
| Imports                                  | Sorted by ruff/isort automatically — never reorder manually    |

### Modern Type Hints (Python 3.13+)

All new code **must** use modern Python type hint conventions. Legacy `typing` module constructs are prohibited unless no native equivalent exists.

| Rule                                 | Policy                                                                                                   |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `from __future__ import annotations` | **Do not use** — Python 3.13+ handles this natively via PEP 649                                          |
| Type unions                          | Use `X \| Y` (PEP 604), not `Optional[X]` or `Union[X, Y]`                                               |
| Generic aliases                      | Use `list[X]`, `dict[K, V]`, `tuple[X, ...]` — not `typing.List`, `typing.Dict`                          |
| `TypedDict`                          | Use `class MyDict(TypedDict):` for structured dicts                                                      |
| `@override`                          | Use `typing.override` (PEP 698) when overriding base class methods                                       |
| Type narrowing                       | Use `isinstance(x, int) \| None` patterns; prefer `TypeGuard` / `TypeIs` (PEP 742) for custom guards     |
| Runtime type checking                | Prefer `isinstance` checks or `type(x) is Y` — avoid `typing.get_type_hints` at runtime unless necessary |

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

## Design Principles & Clean Code

All contributions must adhere to clean code principles to ensure maintainability, readability, and robustness:

*   **DRY (Don't Repeat Yourself)**: Never duplicate business logic. If a piece of logic (such as validation, query filters, alert parsing steps, external API call wrappers) is used in multiple places, extract it to a shared helper function, custom decorator, utility module, or dedicated service class.
*   **SOLID Principles**:
    *   *Single Responsibility (SRP)*: Keep functions and classes small. A function should do one thing and do it well. For example, keep Telegram bot callback handlers thin; move parsing and state tracking logic into dedicated modules (e.g., `parser/`, `state/`).
    *   *Interface Segregation & Dependency Inversion (DIP)*: Depend on abstractions rather than concrete implementations where possible. Inject dependencies (like Redis stores, HTTP clients, LLM clients) rather than hardcoding or instantiating them globally where they interfere with test isolation.
*   **KISS (Keep It Simple, Stupid)**: Avoid premature generalization and complex design patterns (like overly nested abstraction layers) unless explicitly required. Simple, clear, and readable code is always preferred over clever code.
*   **Defensive Programming (Fail Fast)**: Validate arguments and pre-conditions at the beginning of functions. Raise explicit exceptions or return clear error responses immediately rather than letting failures cascade deep into the logic.

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

### External MCP Compatibility

External MCP servers are independent open-source components that this project does not control.
Design integrations against the standard MCP contract and the tool fields actually provided by
the server, such as `name`, `description`, `inputSchema`, and standard annotations when present.

- Never make core functionality depend on custom metadata, proprietary schema extensions,
  upstream patches, forks, or changes to an external MCP server.
- Never assume that an external MCP exposes a particular vendor, tool name, query language, or
  observability capability unless it is present in the tool schema received at runtime.
- Treat capability detection from tool names and descriptions as best-effort only. Unknown or
  ambiguous tools must remain usable through their original schema and must not cause startup or
  analysis failures.
- Vendor-specific adapters or configuration mappings are allowed only as optional enhancements.
  They must have a vendor-neutral fallback that works with an unmodified MCP server.
- Safety requirements such as read-only access, cluster isolation, and write-tool filtering must
  be enforced by this project and its configuration; do not rely on an external MCP being changed
  to enforce them.

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

| Module                   | What to test                                                             |
| ------------------------ | ------------------------------------------------------------------------ |
| `parser/alert_parser.py` | Parsing, edge cases, multi-alert, firing/resolved, fingerprint stability |
| `state/store.py`         | All dedup state transitions with mock Redis                              |
| `mcp/tools.py`           | Happy path + cluster-not-found error with mock `httpx`                   |
| `llm/agent.py`           | Tool-call loop with mock OpenAI client                                   |

### E2E Testing Standards

When writing automated E2E/browser tests (using Playwright, Selenium, etc.), always follow these practices to avoid false-positive test runs:
1. **Simulate Real User Clicks**: Never use direct URL navigation (e.g. `page.goto`) to jump between internal application pages if a user interface element exists. Locate the navigation link/button and trigger a click event to ensure it is not obscured by overlay elements (like backdrops or stacking context overlays).
2. **No Force Actions**: Do not bypass native browser state checks using forced interactions (such as `force=True`). Let the test framework verify that elements are visible, active, and clickable.
3. **Verify Transitions**: After simulating a click, assert that the application state has successfully transitioned (e.g. wait for the URL change or check that a key element on the destination page becomes visible) before performing screenshots or finishing the test steps.

---

## Git Workflow

- All changes must pass `ruff check` and `pytest` before commit
- Commit messages: imperative mood, present tense (`Add ES log search tool`, not `Added...`)

---

## Environment & Configuration

- Copy `.env.example` → `.env` and fill in real values (never commit `.env`)
- Add clusters to `clusters.yml` — keys must exactly match the `cluster` label in Prometheus alerts
- `ALERT_FINGERPRINT_TTL` controls how long a firing alert is deduplicated (default: 24 h)
