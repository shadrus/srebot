# Branch-correct incident lineage on a group-level follow-up context

Parallel follow-ups on one alert group must not cross their incident lineage. The
fingerprint-level `incident_id` in the follow-up context is therefore written only by the initial
alert analysis (the root incident); follow-up responses stop overwriting it. Each response
registers its delivered messages with its own returned incident ID, and when a follow-up produces
none, its messages register the parent incident captured before the analysis started — never a
value re-read from the shared context.

The `rca_text` in the follow-up context remains group-level shared memory: with parallel
branches, a later follow-up sees whichever branch answered last. Per-branch isolation would
require changing the context protocol, so this is a documented exception — only incident lineage
is branch-correct, and requests sharing one incident are never serialized.
