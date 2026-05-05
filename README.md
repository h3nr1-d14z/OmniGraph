# OmniGraph

Distributed Retrieval-Augmented MCP Server for AI code assistants — local-first,
read-only, hybrid semantic + lexical search over a multi-language code graph.

OmniGraph indexes a developer's source repos in real time, exposes a four-tool
MCP bridge for Claude Code (and any other MCP client), and answers ranking
queries by fusing dense vector recall (Qdrant) with full-text BM25 (SQLite
FTS5) and a Tree-sitter-derived dependency graph (Memgraph).

| | |
|---|---|
| Status | Phase 5 (post-cutover-prep) — production for nomic; jina cutover gated by operator. |
| Languages indexed | Go, JavaScript / JSX, TypeScript / TSX, Python (Tree-sitter symbol extraction). |
| Embedding backends | ONNX Runtime (default), MLX (Apple Silicon), OpenVINO (Intel CPU), Jina v2 code (post-cutover target). |
| MCP tools | `semantic_search_code`, `get_dependency_graph`, `read_full_file`, `get_project_tree`. |
| Default port set | Hub API `9000`, Embed `8000`, Qdrant `6333`, Memgraph Bolt `7687`. |
| License | MIT |

## Why OmniGraph

Most retrieval helpers either:
1. shove the whole repo into context and hope grep finds the right line, or
2. stand up a heavyweight cloud-hosted indexer that mints API keys and ships
   source code to a vendor.

OmniGraph picks a third path: a **local-first** index that runs alongside the
developer's checkout, **never mutates source files**, and surfaces results
through MCP so any compliant assistant can use it. The four tools are
deliberately small and read-only — every operation can be replayed without
side-effects.

The system is designed for two scale points:

- **One developer, one machine** — `docker compose up` + `watcher run` and
  Claude Code immediately sees the corpus.
- **Per-team Hub** — Hub runs behind a Cloudflare Tunnel + Cloudflare Access;
  each developer's Watcher pushes events over Bearer-authed HTTPS. The MCP
  server still runs locally so source code never leaves the developer
  machine; only metadata (entities, file paths, content hashes) and chunked
  snippets ride the wire.

## Architecture

```
                                   ┌─────────────────────────┐
                                   │ Claude Code / MCP client│
                                   └────────────┬────────────┘
                                                │ stdio (4 tools)
                                   ┌────────────▼────────────┐
                                   │   MCP Server (Python)   │
                                   │  hub/mcp_server/server  │
                                   └────────────┬────────────┘
                                                │ in-process imports
                ┌───────────────────────────────┼───────────────────────────────┐
                │                               │                               │
       ┌────────▼─────────┐           ┌─────────▼────────┐           ┌──────────▼─────────┐
       │ Qdrant (vector)  │           │ Memgraph (graph) │           │ SQLite content +   │
       │ HNSW + payload   │           │ Cypher queries   │           │ FTS5 lexical index │
       │ indexes          │           │ atomic_replace   │           │ + project trees    │
       └────────▲─────────┘           └─────────▲────────┘           └──────────▲─────────┘
                │                               │                               │
                └───────────────────────────────┼───────────────────────────────┘
                                                │
                                  ┌─────────────▼─────────────┐
                                  │    Hub API (FastAPI)      │
                                  │  hub/api_server/main.py   │
                                  │  POST /batch  POST /search│
                                  │  GET  /stats  GET  /health│
                                  └─────────────▲─────────────┘
                                                │ HTTPS + Bearer + CF Access
                                  ┌─────────────┴─────────────┐
                                  │   Embed Service (FastAPI) │
                                  │   ONNX / MLX / OpenVINO   │
                                  │   / Jina v2 code          │
                                  └─────────────▲─────────────┘
                                                │ HTTP (localhost only)
                                                │
                                  ┌─────────────┴─────────────┐
                                  │   Go Watcher (per host)   │
                                  │   fsnotify + Tree-sitter  │
                                  │   SQLite outbox + replay  │
                                  └───────────────────────────┘
```

### Invariants the system enforces

- **Read-only contract.** No tool mutates the workspace; no Hub endpoint
  edits source files. Watcher events are append-only on the wire.
- **Atomic graph replace per file.** `memgraph.atomic_replace_file` fences
  delete + entity-upsert + relation-upsert inside one Bolt transaction so a
  mid-batch crash never tears the graph (`docs/consistency.md`).
- **Partial-success contract.** `/batch` commits Memgraph + SQLite first,
  then Qdrant, then content-hash. An embed-service failure surfaces as 502
  and the watcher's outbox replays — no double-write, no silent vector lag
  on retry.
- **Deterministic eval.** Phase 0.5 baselines + `compare_baselines.py` gate
  every embedder change against criteria A (≥80% improvement-or-identical
  top-3) and B (no query loses >2 known-good results from baseline top-10).
- **Frozen baselines.** `tests/eval/baselines/<collection>.json` are
  immutable comparison references; never overwrite, only add.

## Tech stack

| Layer | Technology | Why |
|---|---|---|
| Vector store | [Qdrant](https://qdrant.tech) | Rust core, on-disk payload, fast HNSW + payload-filter combos. |
| Graph | [Memgraph](https://memgraph.com) | C++ Cypher engine, sub-ms label scans, Bolt protocol parity with Neo4j drivers. |
| Lexical | SQLite FTS5 | Zero-ops, ships with stdlib `sqlite3`, BM25 scoring built in. |
| Embeddings | nomic-embed-text-v1.5 (current) → jina-embeddings-v2-base-code (cutover target) | Both 768-dim, both 8192-token context, jina is code-specialized. |
| Embedding runtime | ONNX Runtime + Apple MLX + Intel OpenVINO | CPU-portable; MLX uses Metal/ANE on Apple Silicon. |
| Watcher | Go + [fsnotify](https://github.com/fsnotify/fsnotify) + [Tree-sitter](https://github.com/tree-sitter) bindings | Single static binary, no GC pause sensitivity in fs event loops. |
| Hub | FastAPI + httpx + structlog | Async, structured logs, request-scoped tracing. |
| MCP | [FastMCP](https://github.com/jlowin/fastmcp) over stdio | Native protocol for Claude Code. |
| Type-check | mypy with `namespace_packages + explicit_package_bases` | Zero-error baseline on `hub/`. |
| Lint | ruff (E/F/T20/I/UP/B/SIM) + pre-commit + gitleaks | One toolchain replaces flake8 / isort / pyupgrade / bugbear. |

## Quick start (5-minute path)

```bash
# 0. Clone
git clone https://github.com/h3nr1-d14z/OmniGraph.git
cd OmniGraph

# 1. Config
cp .env.example .env       # edit HUB_AUTH_TOKEN, QDRANT_MEM_LIMIT, etc.
echo ".env" >> .gitignore  # already ignored, double-check

# 2. Start the storage tier + embed service
docker compose up -d qdrant memgraph embed-service hub-api

# 3. Wait for the embed model to download (~250 MB nomic, one-time)
curl --retry 24 --retry-delay 5 --retry-connrefused http://localhost:8000/ready
# → {"status":"ready","backend":"onnx-...","vector_dim":768}

# 4. Build the Go watcher and initialise its config
(cd watcher && go build -o ../bin/watcher .)
./bin/watcher init                       # writes ~/.config/omnigraph/watcher.yaml
# → edit watch_root and hub.url, then:
./bin/watcher watch                      # starts the fs event loop

# 5. Wire the MCP server into Claude Code
#    See "MCP wiring" below — point Claude Code at hub/mcp_server/server.py.

# 6. Verify
curl -s -H "Authorization: Bearer changeme-strong-token-here" \
     http://localhost:9000/stats | jq '.qdrant.points, .latency'
```

After step 4 the watcher debounces fs events for ~3 s, batches up to 50
events, sends over HTTPS, and the Hub fans the batch into Memgraph + Qdrant
+ FTS5. Phase 5C.2 latency observability records P50/P95/P99 per route and
surfaces them under `/stats.latency`.

## Components

### `hub/api_server/` — receives batches and serves search

- `POST /batch` — Bearer-authed ingestion endpoint. Wrapped sync DB calls
  with `asyncio.to_thread` (Phase 5C.4) so the FastAPI loop keeps accepting
  the next watcher batch while one is mid-commit.
- `POST /search` — Phase 5C.4 HTTP mirror of the MCP `semantic_search_code`
  tool. Bearer-authed. Lets non-MCP clients (CI eval gates, ad-hoc curl)
  hit the same hybrid ranker.
- `GET /stats` — counts, dead semantic jobs, vector-lag ratio, latency
  histograms, optional watcher queue mirror.
- `GET /health` — liveness; Docker healthcheck targets this.
- Latency middleware records on the **matched route template**
  (`route_label(scope)`), so unmatched paths collapse into `_other_` and
  never grow the tracker key set unbounded.

### `hub/embed_service/` — multi-backend embedding service

| Backend | Default for | Notes |
|---|---|---|
| `onnx` | Linux x86_64, macOS Intel, Windows | Reference implementation; ships nomic + jina ONNX. |
| `mlx` | macOS Apple Silicon | Uses HuggingFace transformers + mlx weights. |
| `openvino` | Intel Linux/Windows with Intel iGPU/NPU | Inherits ONNX path with OpenVINO EP. |
| `jina` | Phase 4 cutover target | Code-specialized 768-dim, 8192-context; **no** instruction prefixes (Jina v2 base code is identity-prefix). |

`EMBED_BACKEND=auto` (default) picks `mlx` on Apple Silicon, otherwise
`onnx`. Override with `EMBED_BACKEND=jina` after running through
`docs/runbooks/reindex.md`.

### `hub/mcp_server/` — the four-tool MCP bridge

| Tool | Input | Output |
|---|---|---|
| `semantic_search_code` | `query: str`, optional `project_scope`, optional `machine_id` | JSON list of `{file_path, machine_id, project, snippet, score, matched_entities}`. Hybrid RRF with Phase 5B.3 token-overlap boost. |
| `get_dependency_graph` | `entity_name: str`, `direction: upstream\|downstream\|both`, optional `machine_id` | `{nodes, edges, summary, groups}`. Edges drop dangling endpoints when truncation kicks in (caps `DEP_GRAPH_MAX_NODES=50`, `MAX_EDGES=100`). |
| `read_full_file` | `machine_id: str`, `file_path: str` | Full file content as the watcher last sent it. Falls through SQLite content store. |
| `get_project_tree` | `machine_id: str`, `project_name: str` | Bounded directory listing (`MAX_TREE_DEPTH=4`, `MAX_TREE_ENTRIES=200`, `MAX_TREE_LINES=300`); appends `[truncated]` marker when caps hit. |

Tool returns are compact-encoded (`separators=(',', ':')`) to keep the
prompt budget small.

### `watcher/` — Go file-system daemon

- **fsnotify** event loop with two-tier debounce (≈277 ms event-coalesce
  inner, 5 s outer batch).
- **Tree-sitter** symbol extraction per supported language, with `StartLine`
  / `EndLine` populated so symbol-level chunking is already in place
  (Phase 5D foundation).
- **Outbox** in SQLite — when the Hub is unreachable, events queue into
  `events` and the durability path replays on reconnection. Outbox uses
  WAL + `busy_timeout` and lease-versioned semantic-job rows.
- **Reconciler** (Phase 5C.1) — a low-frequency loop that auto-replays
  transient-error dead semantic jobs (timeout / connection-refused / 5xx /
  408 / 429 / unexpected EOF). Cursor-paged with `reconcileMaxScanRounds`
  bound and cross-tick persistence so a non-transient prefix can never
  starve transient rows behind it. Gated on `cfg.Semantic.Enabled` so we
  never flip dead → pending without a worker to consume them.
- **HTTP transport** clones `http.DefaultTransport` so `HTTP_PROXY` /
  `HTTPS_PROXY` / `NO_PROXY` continue to work; idle-conn knobs tuned for
  long-running daemons.
- **Backpressure** — `LocalQueue.Enqueue` takes a context; the fs loop
  caps it at 2 s so a slow SQLite never stalls fsnotify.

### `scripts/`

- `setup_tunnel.sh` — interactive Cloudflare Tunnel bootstrap. Override
  `TUNNEL_HOSTNAME` and `HUB_API_PORT` via env.
- `reindex.py` — idempotent re-emit of every event in the content store
  into a target collection. Flags: `--clean`, `--skip-machine-ids`,
  `--no-validate`. Used during Phase 4 cutover.
- `check_health.py` — operator dashboard probe. Reports
  `vector_lag_ratio`, `dead_semantic_jobs`, basic counts.
- `eval/run_baseline.py` + `eval/compare_baselines.py` — Phase 0.5 capture
  + Phase 4 gate.

## Hybrid ranking

Search fuses two lists with [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf):

```
score(file) = Σ over rankers r:
                 weight_r × 1 / (k + rank_r(file))
```

with `k = 20` (calibrated for short prefetch lists, Cormack defaults too
flat at n=10), semantic weight `1.0`, lexical weight `0.7`.

**Phase 5B.3 token-overlap boost** runs *after* RRF and only fires when
the query has identifier-style tokens (≥3 chars, post-stop-word). Two
categories — path overlap and entity-name overlap — each contribute at
most one rank-1 RRF unit per file, scaled by the share of query tokens
that hit. The same identifier splitter is used everywhere so
`HTMLParser`, `SHA256Hash`, `getUserById`, and `OAuth` are tokenized
consistently between query and entity. Path overlap strips OS home-dir
prefixes (`/Users/<name>/`, `/home/<name>/`, Windows drive letters) so
user-names never leak into the boost.

**Defaults** (env-overridable):

```
RRF_K=20
RRF_SEMANTIC_WEIGHT=1.0
RRF_LEXICAL_WEIGHT=0.7
RRF_PATH_BOOST=0.30
RRF_ENTITY_BOOST=0.50
MCP_SNIPPET_MAX_CHARS=250
DEP_GRAPH_MAX_NODES=50
DEP_GRAPH_MAX_EDGES=100
```

Set `RRF_PATH_BOOST=0` and `RRF_ENTITY_BOOST=0` to fall back to the
pre-5B.3 ranking behaviour.

## Phase 4 cutover — nomic → jina

Operator-driven; runbook lives at `docs/runbooks/reindex.md`. The
five-step quiesce + cutover protocol drains the watcher outbox, builds
the new collection alongside the legacy one (no downtime), pre-warms the
jina ONNX model cache (~614 MB FP32), then atomically switches the
backend on Hub + embed-service. The frozen
`tests/eval/baselines/code_v1_nomic.json` (or `omnigraph_code.json` for
deployments still using the legacy collection name) is the immutable
comparison reference; the gate accepts the new embedder only when both
acceptance criteria pass.

Rollback is `EMBED_BACKEND=mlx` (or `auto`) + `QDRANT_COLLECTION=code_v1_nomic`
+ `docker compose restart hub-api embed-service`. RTO is seconds because
both collections coexist post-cutover until the operator drops the old
one.

## Configuration

Every knob has a sane default. Override via `.env`. Selected envs:

| Env | Default | Used by |
|---|---|---|
| `HUB_AUTH_TOKEN` | `changeme-strong-token-here` | Hub Bearer auth — change before running. |
| `EMBED_BACKEND` | `auto` | `auto` / `onnx` / `mlx` / `openvino` / `jina`. |
| `EMBED_MODEL_NAME` | `nomic-ai/nomic-embed-text-v1.5` | HuggingFace repo or local path. |
| `QDRANT_COLLECTION` | `code_v1_nomic` (defaults), `omnigraph_code` (legacy compose) | Collection-per-model isolation. |
| `QDRANT_MEM_LIMIT` | `8g` | Docker resource cap. |
| `MEMGRAPH_MEM_LIMIT` | `6g` | Docker resource cap. |
| `EMBED_WORKERS` | `1` | Embed-service uvicorn workers. |
| `MAX_CHUNKS_PER_FILE` | `64` | Hard cap to bound embed-service load on minified bundles. |
| `SLIDING_WINDOW_CHARS` / `SLIDING_STRIDE_CHARS` | `2400` / `600` | Whole-file fallback window when no entities extracted. |
| `RRF_PATH_BOOST` / `RRF_ENTITY_BOOST` | `0.30` / `0.50` | Phase 5B.3 boosts; `0` disables. |
| `WATCHER_QUEUE_STATS_URL` | `http://localhost:9100/queue/stats` | Optional outbox dashboard mirror in `/stats`. |

`hub/embed_service/.env` and `~/.config/omnigraph/watcher.yaml` carry
component-specific overrides; `watcher init` writes a starter file.

## Testing

```bash
# Python unit tests (fast, default)
.venv/bin/python -m pytest tests -q
# → 173 passed, 3 skipped

# Smoke tests (require the live stack)
.venv/bin/python -m pytest tests/smoke -m smoke --smoke -v

# Go tests with race detector
(cd watcher && go test -race ./...)

# mypy (zero-error baseline)
.venv/bin/mypy hub
# → Success: no issues found in 25 source files

# Lint
.venv/bin/ruff check hub tests
(cd watcher && go vet ./... && gofmt -l .)
```

Coverage highlights:
- `tests/test_us011_rrf.py` — RRF merge math.
- `tests/test_phase5b3_boost.py` — token-overlap boost (camelCase, acronym,
  digit boundaries, OAuth-style two-letter initialisms, path home-prefix
  stripping for macOS / Linux / Windows).
- `tests/test_latency.py` — percentile math, threading, route bucketing.
- `tests/test_dependency_graph_truncation.py` — node-cap ⇒ edge-filter
  ordering.
- `tests/test_hub_search_endpoint.py` — `/search` HTTP wrapper + auth.
- `watcher/watcher/reconcile_test.go` — auto-replay classification, cursor
  paging, content-hash reset, batch-cap-no-skip.
- `watcher/sender/client_test.go` — `http.DefaultTransport.Clone()`
  preserves `ProxyFromEnvironment`.

## Project structure

```
OmniGraph/
├── .env.example                     Template; copy to .env
├── docker-compose.yml               Hub + dependencies; uses ${VAR:-default}
├── PLAN.md                          Phase 1-4 design history
├── docs/
│   ├── PLAN_V5.md                   Phase 5 roadmap (5A polish ... 5D chunking)
│   ├── consistency.md               Partial-success contract
│   └── runbooks/
│       └── reindex.md               Phase 4 cutover runbook
├── hub/
│   ├── api_server/main.py           POST /batch /search, GET /stats /health
│   ├── embed_service/               Multi-backend embedding FastAPI service
│   │   └── backends/                onnx / mlx / openvino / jina_code
│   ├── mcp_server/
│   │   ├── server.py                FastMCP entry; 4 tools
│   │   ├── tools/                   semantic_search / dependency_graph / read_file / project_tree
│   │   ├── db/                      qdrant_client / memgraph_client / content_store
│   │   └── models/schema.py         Pydantic I/O contracts
│   └── latency.py                   Phase 5C.2 LatencyTracker + route_label
├── watcher/
│   ├── main.go                      `init`, `watch`, `replay`, `semantic` subcommands
│   ├── config/                      YAML + env defaults; hasYAMLPath for explicit-zero
│   ├── sender/client.go             HTTP transport (DefaultTransport.Clone)
│   ├── watcher/                     fs.go, queue.go (outbox), reconcile.go (auto-replay)
│   ├── semantic/                    Go-resolver + worker pool
│   └── models/                      FileEvent, Entity, Relation
├── scripts/
│   ├── setup_tunnel.sh              Cloudflare Tunnel bootstrap
│   ├── reindex.py                   Idempotent re-emit (--clean / --skip-machine-ids)
│   ├── check_health.py              Operator probe
│   └── eval/                        run_baseline.py, compare_baselines.py
├── tests/
│   ├── eval/baselines/              Frozen comparison references (immutable)
│   ├── smoke/                       Live-stack tests (--smoke)
│   └── test_*.py                    Unit + integration; respx for HTTP mocks
├── eval/
│   ├── queries.json                 12 hand-curated baseline queries
│   ├── baseline.json                Phase 0.5 working capture (scratch)
│   └── README.md                    Eval harness rationale
└── pyproject.toml                   ruff + mypy + pytest config
```

## Roadmap status

| Phase | Description | Status |
|---|---|---|
| 1 | Hub + ingestion + 4 MCP tools | ✅ |
| 2 | Watcher + outbox + semantic worker | ✅ |
| 2E | Cleanup + tunnel + queue stats + NFC paths | ✅ |
| 3 | Plan v4 corrections (graph identity, embedding capacity, RRF, atomic-replace) | ✅ |
| 4 cutover prep | Gate scripts, reindex hardening, runbook | ✅ |
| 4 cutover exec | jina-v2 swap, eval gate decision | ⏳ Operator-only |
| 5A | Wire compaction, response caps, transport tuning, backpressure | ✅ |
| 5B.1/.2/.4 | machine_id end-to-end, jina prefixes, tree caps | ✅ |
| 5B.3 | Token-overlap boost (path + entity) | ✅ |
| 5C.1 | Auto-replay reconciliation | ✅ |
| 5C.2 | Latency P50/P95/P99 observability | ✅ |
| 5C.3 | mypy zero-error CI gate | ✅ |
| 5C.4 | async wrappers in /batch | ✅ |
| 5D | Entity payload index | ✅ |
| 5D | Symbol-level rebaseline (after Phase 4 cutover) | ⏳ |

## Contributing

The repo runs on pre-commit (ruff + ruff-format + gitleaks + go fmt + go
vet) and CI runs the same hooks plus mypy and the full pytest + go test
suite. Before opening a PR:

1. `pre-commit run --all-files` (or just touch one file and let the hook
   fire).
2. `pytest tests` and `(cd watcher && go test ./...)` are green locally.
3. New behaviour comes with a test. The eval gate
   (`scripts/eval/compare_baselines.py`) is the right reviewer for
   ranking changes.

## Documentation map

- `docs/runbooks/reindex.md` — Phase 4 cutover step-by-step.
- `docs/consistency.md` — partial-success ordering, why the graph is
  committed before the vector.
- `docs/PLAN_V5.md` — Phase 5 roadmap (5A polish through 5D symbol
  chunking).
- `tests/eval/baselines/README.md` — frozen-baseline policy.
- `eval/README.md` — eval harness rationale, `compare_baselines` exit
  codes.

## License

MIT — see `LICENSE` (TODO if absent).
