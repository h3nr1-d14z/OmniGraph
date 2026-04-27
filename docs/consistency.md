# OmniGraph Consistency Contract

Last updated: 2026-04-27

## TL;DR

OmniGraph's ingestion pipeline is **at-least-once for graph + content**, **best-effort with auto-recovery for vectors**. Watcher → Hub `/batch` is durable via the SQLite outbox; Hub's embed step is not transactional with the graph/content writes that precede it.

## Per-component guarantees

### Graph (Memgraph) — at-least-once, idempotent
- `_handle_upserts` writes graph entities/relations BEFORE calling embed-service.
- All writes use `MERGE` on composite key `{file_path, machine_id, name}` → repeated writes for the same file are idempotent (no duplicate nodes, edge metadata is overwritten).
- Tombstones (`delete_file`) cascade-detach edges and prune orphan Module/UnresolvedSymbol/ResolvedSymbol nodes.

### Content store (SQLite) — at-least-once, idempotent
- `upsert_files` uses SQLite `ON CONFLICT(machine_id, file_path) DO UPDATE` → repeated writes overwrite content.
- FTS5 row replaced via `DELETE` + `INSERT` per file_path.

### Vectors (Qdrant) — best-effort with auto-recovery
- Embedding happens AFTER graph/content commit. If embed-service is unreachable, Hub returns **502 Bad Gateway**.
- Watcher receives 502 → batch stays in outbox → retries via `DrainQueue` on next loop or restart.
- On retry success, Hub re-runs the full upsert path: graph MERGE is idempotent (no-op writes for unchanged data), content is overwritten, vectors are now indexed.
- **Recovery model:** the next successful watcher send for a file (modification or queued retry) restores vector state. There is no Hub-side dead-letter for orphan vectors yet; Phase B+ may introduce one.

## Failure modes

### Embed-service down
- Watcher batch attempts → Hub commits graph + content → embed call fails → Hub returns 502.
- Watcher retries from outbox until embed-service recovers.
- During the gap: file appears in `get_dependency_graph` and `read_full_file`, but `semantic_search_code` misses it (vector absent). Lexical FTS5 still hits.

### Watcher crash mid-batch
- Outbox `events` table persists pending payloads.
- On watcher restart, `DrainQueue` replays pending batches in `id ASC` order (deterministic).

### Hub crash after graph commit, before vector
- Same as embed-service down: graph reflects the new state, vectors lag.
- Watcher's next ingest of the same file (or scheduled retry) restores parity.

### Same file modified twice rapidly
- Watcher debounce coalesces fsnotify bursts into the latest content hash before send.
- `lastContentHash` (bounded LRU 50k) suppresses redundant sends for unchanged content.
- Hub MERGE is idempotent on the latest hash → no compounding errors.

## Content-hash short-circuit (US-013)

Watcher restart leaves the in-memory `lastContentHash` LRU empty, so the
catch-up scan re-emits a `MODIFY` for every file. Without dedup, Hub would
`qdrant.delete_by_file` and re-embed the entire corpus (~167 minutes for 50k
files at 200ms/file). Hub now short-circuits identical-hash events using a
durable, in-memory cache seeded from SQLite at startup.

### Mechanism

1. **Schema:** `files.content_hash TEXT` column (nullable, idempotent
   migration via `ALTER TABLE ... ADD COLUMN` + duplicate-column catch).
   Legacy rows are NULL → first watcher event takes the full embed path
   (self-healing).
2. **Cache preload:** at lifespan startup, Hub bulk-loads
   `(machine_id, file_path) → content_hash` from `files WHERE
   content_hash IS NOT NULL` into `app.state.content_hashes`. O(1) lookup
   per event.
3. **Per-event decision in `_handle_upserts`:**
   - `existing_hash == ev.content_hash` → skip qdrant delete, skip
     `/embed`, skip qdrant upsert, skip graph atomic-replace (state already
     reflects this content). Content row's `updated_at` STILL refreshes.
   - Different / NULL existing → full path (graph atomic-replace, qdrant
     delete + re-upsert). After successful qdrant.upsert, write the new
     hash to both the in-memory cache AND `update_content_hash()` for
     persistence.
4. **Embed failure** (502 path): pending hash writes are NOT applied. The
   watcher's outbox retry will replay the batch and Hub will re-attempt
   the full embed path on the next try.
5. **DELETE / RENAME:** the corresponding cache entries are evicted in
   `receive_batch` so re-creation of the same path takes the full path.

### Pattern provenance

Validated against:
- Continue.dev `tag_catalog` (SQLite hash table, pre-filter before re-embed).
- Parcel-watcher snapshot (file-state hashes diffed at startup).
- Watchman `clock` (token compared against persisted state to skip
  reindex).
- Debezium offsets (durable cursor that survives restart and prevents
  reprocessing).

### Failure modes

- **Cache read-write race under concurrent /batch requests:** the
  `app.state.content_hashes` dict is a plain Python mapping shared
  across async handlers. Two concurrent `/batch` requests for the same
  `(machine_id, file_path)` with **different** new hashes can both pass
  the `hash_match` check against a stale cache, both run the full embed
  path, and last-writer-wins in cache + DB. Outcome is benign — final
  state matches the last Qdrant upsert — but you may pay one extra
  embed. Accepted as eventually-consistent per Continue.dev's
  `tag_catalog` contract; in practice the watcher serializes batches
  per-file so the race window is narrow.
- **Hash collision:** content_hash is the watcher's blake3 (or sha256)
  fingerprint. Collision probability is negligible for any realistic
  corpus; if it ever happened, the file would silently miss a re-embed.
  Mitigation: corpus reindex (`scripts/reindex.py`) clears the column.
- **Cache stale after manual Qdrant ops:** if an operator drops a Qdrant
  point out-of-band, the cache still records the hash and refuses to
  re-embed until the watcher emits a new content_hash. Mitigation: the
  reindex runbook (`docs/reindex.md`) clears `content_hash` as part of
  the cutover.
- **Hub crash mid-batch:** cache update happens AFTER successful
  `qdrant.upsert`, but `content.update_content_hash` is a separate
  transaction. A crash between them leaves the cache and DB out of sync
  with Qdrant, but only by a single hash value — the next watcher event
  for the same file repairs it (idempotent MERGE + COALESCE on UPSERT).

## Future work

- Hub-side DLQ for chunks that fail embed after N retries (blocked by Phase 4.5 hybrid-search decisions).
- Symbol-context enrichment may revisit "vector lags graph" semantics if it requires graph cross-reference at embed time.
