# OmniGraph — Re-index & Cutover Runbook

Last updated: 2026-04-27 (Phase 4 — jina-v2-code embedder swap)

## When to use

- Embedder swap (nomic → jina or future).
- Chunking strategy change (sliding-window params, MAX_CHUNKS_PER_FILE).
- Memgraph schema change requiring entity rebuild.

## Pre-flight

- [ ] Plan v4 Phase 0.5 baseline captured at `eval/baseline.json` against current `code_v1_nomic`.
- [ ] Phase 1, 2, 3 commits all merged (correctness fixes precede re-index).
- [ ] Git working tree clean (so rollback via revert is unambiguous).
- [ ] Two collections will coexist during cutover; storage budget verified (~6GB per 1M vectors).

## 5-step quiesce + cutover protocol

```
┌─ Step 1: Disable semantic worker (drain inflight)
│   Edit ~/.config/omnigraph/watcher.yaml: semantic.enabled: false
│   Restart watcher; wait for logs "[watcher] semantic worker stopped"
│
├─ Step 2: Drain outbox (≤30s)
│   sqlite3 ~/.config/omnigraph/watcher-queue.db \
│     "SELECT COUNT(*) FROM events; SELECT COUNT(*) FROM semantic_jobs WHERE state='pending';"
│   Both should reach 0. If not, abort cutover and surface state for manual
│   investigation (a stuck job blocks deterministic cutover).
│
├─ Step 3: Build new Qdrant collection (Hub still on code_v1_nomic)
│   docker compose exec hub-api python -c \
│     "from db.qdrant_client import QdrantCodeStore; \
│      QdrantCodeStore(collection='code_v2_jina')"
│
├─ Step 3.5: Pre-warm jina cache while nomic still serves (no downtime)
│   docker compose exec embed-service python -c \
│     "from huggingface_hub import snapshot_download; \
│      snapshot_download(repo_id='jinaai/jina-embeddings-v2-base-code', \
│      allow_patterns=['onnx/*','tokenizer.json','config.json'], \
│      cache_dir='/models')"
│   This pulls ~250MB ONNX into the model_cache named volume while hub-api
│   is still on nomic. After Step 4 restart, _load() finds files cached and
│   ONNX init is 1-5s instead of 30-90s.
│
├─ Step 4: Stop Hub & embed-service, switch backend
│   docker compose stop hub-api embed-service
│   # Edit .env:
│   #   EMBED_BACKEND=jina
│   #   EMBED_MODEL_NAME=jinaai/jina-embeddings-v2-base-code
│   #   QDRANT_COLLECTION=code_v2_jina
│   docker compose build embed-service hub-api    # picks up new code
│   docker compose up -d embed-service hub-api
│   NOTE: skip `docker compose build` if only .env changed (image is identical).
│   Use `docker compose stop` + `docker compose up -d` instead — preserves
│   build cache and named volumes.
│
├─ Step 4.5: Healthcheck gate (120s timeout; was 60s before ONNX warm-up fix)
│   curl --max-time 120 --retry 24 --retry-delay 5 \
│        --retry-connrefused http://localhost:9000/health
│   curl http://localhost:8000/ready
│   If timeout: roll back step 4 (.env restore + restart) and abort.
│
└─ Step 5: Re-index from content store
    .venv/bin/python scripts/reindex.py --collection code_v2_jina
    # Restart watcher (re-enables semantic.enabled if desired)
```

## Rollback matrix

| Component | Method | RTO |
|---|---|---|
| Qdrant active collection | Edit `.env` `QDRANT_COLLECTION=code_v1_nomic`, `docker compose restart hub-api` | seconds |
| Memgraph composite index | `MATCH (n) DETACH DELETE n` is destructive; instead `DROP INDEX ON :Entity(machine_id, project, file_path, name)` via `mgconsole` | seconds; no data loss |
| Sliding-window chunking logic | `git revert <Phase-4 commits>` + re-index against `code_v1_nomic` | minutes |
| Per-backend prefix dispatch | `git revert` (additive interface; nomic call site preserved) | seconds |
| jina backend registration | `.env` `EMBED_BACKEND=mlx` (or `auto`), `docker compose restart embed-service` | seconds |
| `docker compose down -v` | NEVER USE during cutover — destroys model_cache volume, forces full re-download | n/a |

## Acceptance gate (Phase 4 quantitative)

After step 5 completes:

```bash
.venv/bin/python scripts/eval/run_baseline.py \
    --output eval/jina_v2.json
```

Then compare `eval/jina_v2.json` vs `eval/baseline.json`:

- ≥80% of queries show ≥1 improvement OR identical top-3 set
- **AND** no query loses >2 known-good results from baseline top-10

If the gate fails:
1. Rollback per matrix above.
2. Capture failure diff per category in `eval/jina_v2_failure.md`.
3. Decide: retry with different chunk params, or stay on nomic.

## Troubleshooting

**"Hub returns 502 during re-index"**
The partial-success contract (see docs/consistency.md) means Hub commits graph + content
before embed. If embed-service is mid-warm-up, /batch returns 502 and
reindex.py marks the file failed. Re-run reindex.py once /ready passes — it
is idempotent.

**"jina model download blocks on first request"**
First /embed call triggers ~500MB download. Pre-warm:
```bash
docker compose exec embed-service python -c \
  "from huggingface_hub import snapshot_download; \
   snapshot_download(repo_id='jinaai/jina-embeddings-v2-base-code')"
```

**"Both collections present after cutover"**
Intentional. Drop `code_v1_nomic` only after `code_v2_jina` proves stable
through one full session of usage. Keep at least one iteration as rollback
target.
