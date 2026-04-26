# OmniGraph Status and Roadmap

Last updated: 2026-04-26

## Executive Summary

OmniGraph is now at a solid local-first baseline for code ingestion, retrieval, graph indexing, MCP access, and optional Go semantic enrichment. The core Hub stack runs locally with Docker, the watcher ingestion path has been optimized to debounce before parse/read, and the semantic resolver is wired behind config with a durable outbox in progress.

Current best next step: finish and commit **Phase 2E.5 durable semantic outbox**, then move to **Phase B search quality**: machine-scoped semantic search, query embedding cache, hybrid lexical/vector ranking, and project tree caps.

## Current Architecture

### Hub side

- **Hub API** receives watcher batches and writes to storage backends.
- **Embed service** owns embedding model execution. MCP does not load embedding models.
- **Qdrant** stores vector points for semantic search.
- **Memgraph** stores file/entity/relation graph data.
- **SQLite content store** stores file contents, FTS5 lexical index, project tree cache, and query embedding cache.
- **MCP server** exposes tools for Claude Code, including semantic search, file reads, project tree, stats, and dependency graph.

### Watcher side

- Go watcher observes project files.
- Ingestion is debounce-first: filesystem events are coalesced before file reads and Tree-sitter parsing.
- File content hashing skips unchanged content.
- Syntax events use local SQLite offline queue if Hub is unreachable.
- Optional Go semantic worker enriches graph with resolved imports/calls.
- Semantic enrichment is disabled by default and runs asynchronously.

## Completed Work

### 1. Local Hub baseline and full test environment

Completed:

- Docker Compose stack for Qdrant, Memgraph, embed-service, and hub-api.
- Health validation for runtime services.
- Root `requirements-dev.txt` so full Python test suite can run reliably.
- README test instructions updated to use the project Python environment.

Verification:

- Python suite has passed repeatedly: `28 passed`.
- Docker services have been observed healthy locally.

### 2. Hub `/stats` observability

Completed:

- Added protected `/stats` endpoint.
- Added scoped stats for content store, Qdrant, and Memgraph.
- Sanitized backend error responses to avoid leaking raw exception text.
- Fixed Qdrant scoped counts to use exact count by default where needed.

Why it matters:

- Runtime smoke tests can now verify ingestion, vector, and graph state directly.
- Debugging local watcher/Hub flows is much faster.

### 3. Watcher ingestion optimization

Completed:

- Moved expensive file reads and Tree-sitter parsing out of `handleEvent()`.
- Coalesced fsnotify bursts before parsing.
- Added content hash dedupe to skip unchanged file sends.
- Enforced Hub batch size chunking.
- Preserved delete/tombstone behavior.
- Added entity line ranges for future symbol-level chunking.

Acceptance status:

- `handleEvent()` no longer does `os.ReadFile` or syntax extraction.
- Rapid writes collapse to final content.
- No-op content changes are skipped.
- Delete clears hash state.
- Batch-size chunking is covered by tests.

### 4. Search and MCP quality improvements

Completed:

- SQLite FTS5 lexical search support.
- Query embedding cache support in content store.
- MCP dependency graph output now includes `summary` and relationship `groups`.
- MCP stdio E2E tests were added.
- `/stats` support and dependency graph assertions were tightened.

Important MCP behavior:

- MCP remains local/lightweight and calls Hub services for embeddings/search.
- MCP does not load embedding models.

### 5. Graph indexing improvements

Completed:

- Syntax graph relations from Tree-sitter:
  - `CONTAINS`
  - `IMPORTS`
  - `CALLS_SYNTAX`
- Semantic relation contract:
  - `IMPORTS_RESOLVED`
  - `CALLS_RESOLVED`
  - `REFERENCES`
- Semantic metadata fields:
  - `layer`
  - `status`
  - `symbol_id`
  - `target_ref`
  - `package`
  - `language`
  - `confidence`
- Hub semantic-only events are safe: they enrich graph without deleting content/vector/syntax state.
- Memgraph orphan cleanup was improved for `Module`, `UnresolvedSymbol`, and `ResolvedSymbol`.

### 6. Go semantic resolver prototype

Completed:

- Go semantic resolver using `golang.org/x/tools/go/packages` and `go/types`.
- Resolves imports and calls for one Go file.
- Supports overlay content so watcher can resolve unsaved/latest content.
- Receiver-qualified method identity avoids ambiguity:
  - `example.com/demo.Alpha.Run`
  - `example.com/demo.Beta.Run`
- Fail-closed library behavior for broken packages/missing imports.
- Strict CLI behavior for manual probe.

CLI:

```bash
go -C watcher run . semantic -root ./my-module -file ./my-module/main.go
```

Verification:

- Resolver unit tests cover imports, alias imports, receiver methods, overlays, broken overlays, missing imports, and non-Go files.
- Benchmark exists for file-scoped resolver.

### 7. Semantic worker core

Completed and committed:

- Commit: `ff0dfcd Add async semantic worker core`

Capabilities:

- In-memory async job worker.
- Coalesces jobs by machine/project/path.
- Latest content-hash guard.
- Per-job timeout.
- Retry delay and max retry guard.
- Idempotent Start/Stop.
- Stop cancels blocked resolver.
- Race-tested worker package.

### 8. Config-gated semantic worker wiring

Completed and committed:

- Commit: `add8edf Wire semantic worker behind config`

Capabilities:

- `semantic.enabled` defaults to `false`.
- Semantic config includes:
  - `worker_count`
  - `queue_capacity`
  - `timeout_ms`
  - `retry_delay_ms`
  - `max_retries`
  - `cache_size`
- Semantic worker starts only when enabled.
- Syntax ingest remains primary and non-blocking.
- Semantic jobs are only generated after syntax sends directly to Hub or after offline queue replay succeeds.
- Semantic-only payloads send relations without content/entities.
- Watch root normalization expands `~` and writes normalized root back into config before watcher construction.
- Project root resolution falls back to watch root if autodetect misses.

Runtime smoke evidence:

- `semantic.enabled=true` smoke passed with temp Go module.
- Before cleanup:
  - `files=1`
  - `qdrant points=2`
  - `memgraph edges=5`
- After cleanup:
  - scoped files/vectors/graph returned to `0`.

### 9. Semantic relation cache

Completed and committed as part of semantic wiring checkpoint:

- Bounded in-memory cache keyed by root/path/content hash.
- Clones relations on put/get to avoid mutation hazards.
- `cache_size: 0` explicitly disables cache.
- Omitted `cache_size` defaults to `256`.

### 10. Durable semantic outbox

Implemented but not committed at the time this doc was written.

Files modified:

- `watcher/semantic/worker/worker.go`
- `watcher/watcher/fs.go`
- `watcher/watcher/integration_test.go`
- `watcher/watcher/queue.go`
- `watcher/watcher/semantic.go`

Current behavior:

- Adds SQLite `semantic_jobs` outbox table in watcher queue DB.
- Syntax delivery remains the source of truth.
- After syntax direct send or queued replay succeeds, watcher upserts a durable semantic job.
- Semantic poll loop claims due jobs from SQLite and feeds existing semantic worker.
- Jobs transition through:
  - `pending`
  - `running`
  - `done`
  - `dead`
- Retry uses exponential backoff.
- Jobs become `dead` after max retry count.
- Delete/rename removes pending semantic job for that file.
- New content hash replaces older pending/running job for the same file.
- Claiming uses `lease_version` fencing:
  - each claim increments `lease_version`
  - worker job carries `OutboxID` and `LeaseVersion`
  - current/done/fail/release require matching lease version and `state=running`
  - stale owner after lease expiry cannot send or finalize after reclaim

Verification completed:

- Focused semantic outbox tests passed.
- `go -C watcher test ./...` passed.
- `go -C watcher test -race ./...` passed.
- `git diff --check` passed.
- Python tests passed: `28 passed`.
- Runtime smoke with `semantic.enabled=true` passed and cleaned up smoke data.
- Independent code review approved lease fencing with no critical/high/medium findings.

Known state:

- Durable semantic outbox changes are currently uncommitted.
- Next action should be a local commit checkpoint after final status check.

## Important Design Decisions

### MCP stays local/lightweight

Decision:

- MCP should run close to Claude Code/local tooling and call Hub APIs.
- MCP should not load embedding models.

Reason:

- Keeps MCP startup fast and simple.
- Embedding model lifecycle belongs to Hub/embed service.

### Watcher optimizes hot path first

Decision:

- Do not run expensive semantic resolution in fsnotify hot path.
- Debounce and hash before any heavy parse/resolve work.

Reason:

- Editor saves and formatters can emit many events.
- Only final content matters for indexing.

### Semantic enrichment is opt-in and asynchronous

Decision:

- `semantic.enabled=false` by default.
- Semantic worker runs after syntax delivery.

Reason:

- Syntax/content/vector ingest must remain reliable and fast.
- Semantic resolution can be expensive and can fail due to package state.

### Durable semantic jobs use an outbox, not the existing event queue

Decision:

- Use dedicated `semantic_jobs` table instead of mixing semantic derived work into raw file event queue.

Reason:

- Separate retry policy.
- Better coalescing by file/hash.
- Clearer state transitions and observability.
- Easier dead-letter handling.

### Lease fencing is required

Decision:

- `lease_version` is incremented on every claim.
- Completion/failure/release requires matching lease version.

Reason:

- Prevents stale workers from finalizing a job after another worker reclaims it.
- Makes reclaim safe after timeouts/restarts.

## Current Verification Matrix

Most recent relevant checks:

```bash
go -C watcher test ./...
go -C watcher test -race ./...
git diff --check
/tmp/omnigraph-test-venv/bin/python -m pytest -q tests
go -C watcher build -o /tmp/omnigraph-watcher-smoke .
```

Runtime semantic outbox smoke:

- Watcher started with `semantic.enabled=true`.
- Temp Go module saved with `fmt.Println` and local helper call.
- Hub stats before cleanup:
  - `files=1`
  - `qdrant points=2`
  - `memgraph entities=2`
  - `memgraph edges=5`
- Hub stats after cleanup:
  - `files=0`
  - `qdrant points=0`
  - `memgraph entities=0`
  - `memgraph edges=0`

## Current Git State

Committed checkpoints:

- `ff0dfcd Add async semantic worker core`
- `add8edf Wire semantic worker behind config`

Uncommitted work:

- Phase 2E.5 durable semantic outbox.

Files with uncommitted changes:

- `watcher/semantic/worker/worker.go`
- `watcher/watcher/fs.go`
- `watcher/watcher/integration_test.go`
- `watcher/watcher/queue.go`
- `watcher/watcher/semantic.go`

Recommended immediate action:

```bash
git add watcher/semantic/worker/worker.go \
        watcher/watcher/fs.go \
        watcher/watcher/integration_test.go \
        watcher/watcher/queue.go \
        watcher/watcher/semantic.go

git commit -m "Add durable semantic outbox"
```

Use the project commit trailer:

```text
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

## Roadmap

### Phase 2E.5 — Durable semantic outbox

Status: implemented, verified, needs commit.

Remaining:

1. Final `git status` check.
2. Commit local checkpoint.
3. Optional post-commit smoke test.

### Phase 2E.6 — Semantic reconciliation

Goal:

- Heal cases where semantic jobs are missing or dead but syntax content exists.

Recommended scope:

- Add a low-frequency reconciliation loop.
- Re-enqueue semantic jobs for Go files where:
  - syntax content exists locally/currently known
  - semantic job is missing or dead
  - content hash differs from last successful semantic version
- Keep it disabled or conservative initially.

Why next:

- Durable outbox handles queued jobs.
- Reconciliation handles missed jobs, manual DB edits, old queue DBs, and edge restarts.

### Phase B — Search quality and efficiency

Goal:

- Improve retrieval accuracy and speed.

Recommended tasks:

1. Add `machine_id` to semantic search MCP input and thread it through Qdrant search.
2. Improve query embedding cache use.
3. Implement hybrid lexical + vector ranking:
   - exact phrase boost
   - token overlap boost
   - path/entity boost
   - semantic score remains primary initially
4. Cap project tree output:
   - max depth
   - max entries
   - max chars/lines
   - explicit truncated marker
5. Ensure embedding prefixes distinguish query/document mode where backend supports it.

### Phase C — Symbol-level chunking

Goal:

- Index one vector per function/class/symbol body rather than only file-level chunks.

Prerequisites:

- Watcher entity ranges are stable.
- Ingestion/hash/debounce/outbox pipeline is reliable.

Recommended tasks:

1. Use `Entity.StartLine`/`EndLine` to slice symbol body in Hub.
2. Store Qdrant point per symbol chunk.
3. Keep full file content in SQLite for `read_full_file`.
4. Delete/tombstone removes all chunks for a file.
5. Return function-level snippets in semantic search results.

### Phase D — Runtime and test hardening

Goal:

- Improve confidence and reduce regression risk.

Recommended tasks:

1. Add/verify Compose healthchecks for all services.
2. Tighten E2E assertions:
   - project
   - machine_id
   - top-k result stability
   - tombstone exclusions
3. Add smoke scripts for:
   - watcher syntax-only
   - watcher semantic-enabled
   - offline queue recovery
   - MCP stdio tools

## Risks and Watch Items

### Semantic resolver cost

Risk:

- `go/packages` can be expensive on large modules.

Mitigation:

- Keep semantic disabled by default.
- Use bounded cache.
- Durable outbox prevents repeated hot-path work.
- Future: package/root-level resolver cache or batch package loading.

### Semantic dead jobs

Risk:

- Package errors or broken Go files may push jobs to `dead`.

Mitigation:

- Keep DLQ state visible in SQLite.
- Add future stats for semantic job states.
- Add reconciliation to retry dead jobs after content changes.

### Duplicate semantic sends

Risk:

- At-least-once systems can duplicate messages.

Mitigation now:

- Hub semantic-only relation upserts should be idempotent.
- Outbox claims use `lease_version` fencing.
- Stale workers cannot finalize after reclaim.

Future improvement:

- Include semantic job id/version in payload if Hub-side dedupe is needed.

## Recommended Next Commands

After reviewing current uncommitted durable outbox changes:

```bash
go -C watcher test ./...
go -C watcher test -race ./...
/tmp/omnigraph-test-venv/bin/python -m pytest -q tests
git diff --check
```

Then commit:

```bash
git status --short
git add watcher/semantic/worker/worker.go \
        watcher/watcher/fs.go \
        watcher/watcher/integration_test.go \
        watcher/watcher/queue.go \
        watcher/watcher/semantic.go \
        docs/OMNIGRAPH_STATUS_AND_ROADMAP.md

git commit -m "$(cat <<'EOF'
Add durable semantic outbox

Persist semantic enrichment jobs separately from syntax delivery so semantic graph updates survive restarts and retry safely with lease fencing.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

## Bottom Line

OmniGraph has moved from a basic local RAG/MCP prototype to a much more reliable local-first code intelligence stack:

- syntax ingest is debounced, hashed, batched, and offline-durable;
- graph indexing supports syntax and semantic contracts;
- Go semantic resolution works behind config;
- semantic enrichment is asynchronous and now durable with outbox + lease fencing;
- full local test and smoke validation are in place.

The next highest-value work is to commit the durable semantic outbox checkpoint, then implement lightweight semantic reconciliation, then proceed to search quality and symbol-level chunking.
