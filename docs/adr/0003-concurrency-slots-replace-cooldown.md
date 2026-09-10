# Admission concurrency slots replace the follow-up cooldown

Simultaneous analyses are bounded by two in-process FIFO limits instead of rejecting users: a
global `ANALYSIS_MAX_CONCURRENCY` (default 20) and a per-user `ANALYSIS_MAX_CONCURRENCY_PER_USER`
(default 3) keyed by platform, chat, and user. Requests beyond a limit wait for a free slot and are
served FIFO per dimension; slots are acquired user-first then global so a saturated user cannot
head-of-line block the global queue, and acquisition happens before the LLM timeout scope so
waiting never consumes analysis time. Alert analyses carry no user identity and are exempt from
the per-user limit; the global limit still applies. Cancellation and exceptions release slots
through a context manager, and idle per-user gates are dropped so keyed semaphores cannot leak.

The user cooldown (`FOLLOWUP_USER_COOLDOWN_SEC`) is removed entirely, which also removes the only
Redis-side gate for general queries; they are now bounded solely by concurrency slots. This is
deliberate: incident turn quotas stay in Redis because they must survive restarts and count
totals, while slots count simultaneous work and are intentionally process-local — deployments run
exactly one bot integration per process.
