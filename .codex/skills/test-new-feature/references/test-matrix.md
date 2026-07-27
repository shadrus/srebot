# Test Matrix

Read this file while mapping a feature to checks. Select only rows touched by the behavior or its
failure modes.

| Layer or boundary | Minimum useful evidence | Add when risk warrants |
|---|---|---|
| Pure logic | Focused unit test | Empty, malformed, boundary, property cases |
| Configuration | Parsed value and default behavior | Invalid value, missing secret, reload |
| HTTP API | Request, status, response contract | Auth, validation, idempotency, concurrency |
| Database | Intended persisted state | Isolation, rollback, migration, unintended writes |
| Queue or worker | Job accepted and processed | Retry, duplicate delivery, poison message |
| Cache or state store | Correct state transition | TTL, race, stale state, invalidation |
| Browser UI | Real user interaction and visible result | Reload persistence, errors, accessibility |
| Telegram or messaging | Approved transport plus processed result | Duplicate, edit, callback, delivery failure |
| LLM | Prompt inputs and structured output | Malformed output, context isolation, token cost |
| MCP or tools | Runtime schema and actual arguments | Invalid arguments, redundant calls, retry loop |
| External service | Contract-level integration result | Timeout, rate limit, degraded dependency |
| Permissions or tenancy | Allowed and denied paths | Cross-tenant access, privilege change |
| Observability | Expected log/metric/trace without secrets | Failure signal, correlation, alert noise |

## Coverage Heuristic

For each acceptance criterion, require:

1. one direct assertion at the lowest meaningful layer;
2. one boundary assertion for every serialization, process, or service boundary it crosses;
3. one real public-entry-point happy path through all affected runtime boundaries;
4. one negative path for the highest-impact plausible failure.

Do not multiply tests that prove the same fact. A broad E2E result does not replace focused failure
diagnostics, and isolated unit tests do not prove integration wiring.

The real public-entry-point path is mandatory whenever the environment can run it. A direct handler
call, mocked downstream, synthetic event, or manual database write does not satisfy this row.

## Runtime Cost Heuristic

Order checks by expected diagnostic value divided by runtime and side-effect cost, but always
complete the mandatory real E2E path before issuing a verdict:

1. static inspection and focused unit tests;
2. focused component and API tests;
3. local integration tests;
4. browser, messaging, LLM, MCP, and external-service checks;
5. broad regression suites.

Change the order when the suspected defect exists only at a higher boundary.
