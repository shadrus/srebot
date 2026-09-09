# Dispatch Time Messenger events to a background task supervisor

The registered aiotimebot handler performs only cheap synchronous filters (configured channel,
own posts) and then runs `handle_posted_event` in a background task owned by a task supervisor,
returning `Propagation.STOP` immediately. aiotimebot serializes events of one channel until the
handler returns, so full analyses used to block every later post in that channel; background
dispatch removes that coupling without patching or forking the package.

The supervisor owns every task from spawn to completion: one task's exception is logged and cannot
terminate other analyses or the bot process, and shutdown stops accepting spawns, cancels active
tasks with a short shielded best-effort cleanup of posted placeholders, and only then lets the
Time client and shared resources close. Giving up aiotimebot's per-channel event ordering is an
accepted trade-off: commands posted right after an alert may now interleave with its analysis.
