# OmniGraph Runbooks

Operational runbooks for repeatable Hub + Watcher procedures. Each
runbook is a self-contained recipe with pre-flight checks, steps, and
rollback. Index entries name the trigger condition and the typical
caller (operator / on-call).

| Runbook | Trigger | Caller |
|---------|---------|--------|
| [reindex.md](reindex.md) | Embedder swap, chunking change, Memgraph schema change requiring entity rebuild | Operator |

When adding a new runbook:
1. Place the file under `docs/runbooks/<slug>.md`.
2. Append a row above with trigger condition + caller.
3. Lead with a "When to use" section so the trigger is unambiguous.
4. Include rollback steps; an unrecoverable runbook is a runtime hazard.
