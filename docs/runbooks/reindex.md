# OmniGraph — Re-index & Cutover Runbook

Last updated: 2026-04-27 (Phase 4-cutover prep — jina-v2-code embedder swap)

## When to use

- Embedder swap (nomic → jina or future).
- Chunking strategy change (sliding-window params, MAX_CHUNKS_PER_FILE).
- Memgraph schema change requiring entity rebuild.

## Pre-flight

- [ ] Plan v4 Phase 0.5 baseline captured at `eval/baseline.json` against current `code_v1_nomic`.
- [ ] **Baseline collection-field validation:** open `eval/baseline.json` and confirm `"collection": "code_v1_nomic"`. Older captures may have been taken against the legacy `omnigraph_code` collection — those are stale references and the Phase 4 gate is meaningless against them. If stale, re-capture against `code_v1_nomic` before proceeding (Step 0.5 below).
- [ ] Phase 1, 2, 3 commits all merged (correctness fixes precede re-index).
- [ ] Git working tree clean (so rollback via revert is unambiguous).
- [ ] Two collections will coexist during cutover; storage budget verified (~6GB per 1M vectors).
- [ ] Test fixtures purged from real corpus: `sqlite3 data/hub-content.db "SELECT machine_id, count(*) FROM files GROUP BY machine_id;"` — if `test-machine-01`, `m1`, or `local` rows leaked into the production content store, run `reindex.py --skip-machine-ids test-machine-01,m1,local` to keep them out of `code_v2_jina`.

## Step 0.5 — (Optional) Re-capture nomic baseline

Run only if the pre-flight `eval/baseline.json` collection-field check failed
or the baseline is missing.

```
QDRANT_COLLECTION=code_v1_nomic \
  .venv/bin/python scripts/eval/run_baseline.py \
    --collection code_v1_nomic \
    --output eval/baseline.json
.venv/bin/python scripts/eval/run_baseline.py --collection code_v1_nomic --check
cp eval/baseline.json tests/eval/baselines/code_v1_nomic.json   # freeze
```

The frozen `tests/eval/baselines/code_v1_nomic.json` is the immutable
comparison reference for Phase 4. See `tests/eval/baselines/README.md` for
the frozen-baseline policy.

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
│      allow_patterns=['onnx/model.onnx','tokenizer.json','config.json'], \
│      cache_dir='/models')"
│   This pulls ~614 MB (model.onnx + tokenizer) into the model_cache named
│   volume while hub-api is still on nomic. The narrowed allow_patterns skips
│   460 MB of unused FP16/INT8 ONNX variants. After Step 4 restart, _load()
│   finds files cached and ONNX init is 1-5s instead of 30-90s.
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
├─ Step 4.6: Smoke embed (verify 768-dim output before re-indexing)
│   curl -s -X POST http://localhost:8000/embed \
│     -H 'Content-Type: application/json' \
│     -d '{"texts":["package main\n\nfunc main() {}"], "mode":"document"}' \
│     | python3 -c "
│   import sys, json
│   data = json.load(sys.stdin)
│   vecs = data.get('embeddings') or data.get('vectors') or data
│   assert len(vecs) == 1, f'expected 1 vector, got {len(vecs)}'
│   assert len(vecs[0]) == 768, f'expected 768 dims, got {len(vecs[0])}'
│   print(f'OK: 768-dim vector, first 3: {vecs[0][:3]}')
│   "
│   Failure here means the ONNX session loaded but produces wrong-shape
│   output (collection schema mismatch) — abort and rollback before reindex.
│
└─ Step 5: Re-index from content store
    .venv/bin/python scripts/reindex.py --collection code_v2_jina
    # The script validates Hub /stats reports the same active collection;
    # use --no-validate to bypass, --clean to drop+recreate the collection
    # before re-emit, --skip-machine-ids to exclude test fixtures.
    # Restart watcher (re-enables semantic.enabled if desired)
```

## Acceptance gate (Phase 4 quantitative)

After Step 5 completes, run the quantitative gate sequence:

### Step 5.1 — Capture jina baseline

```bash
QDRANT_COLLECTION=code_v2_jina \
  .venv/bin/python scripts/eval/run_baseline.py \
    --collection code_v2_jina \
    --output eval/jina_v2.json
```

### Step 5.2 — Reproducibility check

```bash
.venv/bin/python scripts/eval/run_baseline.py --collection code_v2_jina --check
```

Must report `worst jaccard >= 0.95`. Below that means the active collection
is non-deterministic and any A/B comparison is noise.

### Step 5.3 — Run the comparison gate

```bash
.venv/bin/python scripts/eval/compare_baselines.py \
    tests/eval/baselines/code_v1_nomic.json \
    eval/jina_v2.json
# exit 0 = PASS, 1 = FAIL, 2 = no overlapping query IDs
```

The script enforces:

- **Criterion A** — ≥80% of queries show ≥1 improvement OR identical top-3 set vs frozen nomic baseline.
- **Criterion B** — no query loses >2 known-good results from baseline top-10.

If exit 1: read the JSON failure block on stdout. Decide whether to retry
with different chunk params or roll back per matrix below.

### Step 5.4 — Final health check

```bash
.venv/bin/python scripts/check_health.py
```

Expected: `dead_semantic_jobs=0`, `vector_lag_ratio` close to 1.0 for the
re-indexed corpus. Any failure here means the re-index is incomplete.

### Step 5.5 — Freeze the jina baseline

If Steps 5.1-5.4 all pass, freeze the jina baseline as the C.1 reference:

```bash
cp eval/jina_v2.json tests/eval/baselines/code_v2_jina.json
git add tests/eval/baselines/code_v2_jina.json && git commit
```

The frozen file is **immutable** until Phase C.2 evaluation. See
`tests/eval/baselines/README.md` for the full frozen-baseline policy.

## Rollback matrix

| Component | Method | RTO |
|---|---|---|
| Qdrant active collection | Edit `.env` `QDRANT_COLLECTION=code_v1_nomic`, `docker compose restart hub-api` | seconds |
| Memgraph composite index | `MATCH (n) DETACH DELETE n` is destructive; instead `DROP INDEX ON :Entity(machine_id, project, file_path, name)` via `mgconsole` | seconds; no data loss |
| Sliding-window chunking logic | `git revert <Phase-4 commits>` + re-index against `code_v1_nomic` | minutes |
| Per-backend prefix dispatch | `git revert` (additive interface; nomic call site preserved) | seconds |
| jina backend registration | `.env` `EMBED_BACKEND=mlx` (or `auto`), `docker compose restart embed-service` | seconds |
| `docker compose down -v` | NEVER USE during cutover — destroys model_cache volume, forces full re-download | n/a |

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
