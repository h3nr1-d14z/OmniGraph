# OmniGraph — Plan v5 (Post Phase 4-cutover)

**Ngày tạo:** 2026-04-27
**Trạng thái:** Phase 4-cutover prep đã merge (a5ea8b5). Cutover execution (jina-v2 swap) là việc tiếp theo.
**Phạm vi:** Roadmap 4 phase tiếp theo sau khi cutover hoàn tất.

---

## 1. Audit Verdict

OmniGraph đã được tối ưu **khá tốt** ở các hot path. Phần còn lại là (a) vài quick wins ở wire format & connection tuning, (b) các feature đã được nhận diện sẵn trong roadmap (Phase B/C/D).

### Strengths cần giữ nguyên

| Layer | Đã tối ưu |
|-------|-----------|
| Hub Python | Durable outbox, content-hash short-circuit cache, batched embed (32/req), hybrid RRF (semantic + FTS5), httpx pooled clients, structured TTY-aware logging, atomic Memgraph TX với exp backoff |
| Watcher Go | WAL + busy_timeout, lease-versioned outbox fencing, two-tier debounce (277ms event + 5s batch), bounded contentHash dedup (50k), graceful shutdown với semanticWg, NFC normalization |
| Tests/CI | 113 tests · baseline-driven eval (Jaccard ≥0.95) · ruff + gitleaks · healthchecks toàn bộ services · cutover gate script |

### Gaps cần xử lý

**Hub:**
- `mcp_server/server.py:38,52` — `json.dumps(indent=2)` thổi response ~30%.
- `tools/semantic_search.py:111` — snippet 500 chars có thể giảm xuống 250.
- `tools/dependency_graph.py:19-24` — chưa cap node/edge.
- `api_server/main.py:303,320,328,376` — sync DB calls block event loop trong async `/batch`.

**Watcher:**
- `sender/client.go:34` — `http.Client` thiếu transport tuning (`MaxIdleConns`, `IdleConnTimeout`, `MaxIdleConnsPerHost`).
- `fs.go:434` — `queue.Enqueue` không có ctx deadline (no backpressure).
- `queue.go:132,166,208+` — `time.Now().UnixMilli()` gọi nhiều lần/cycle.

**Tests/CI:**
- Không có latency SLA (P50/P95/P99).
- Không có mypy / type checking trên ~3,300 LOC Python.
- syrupy dùng nhưng chưa pin trong `requirements-dev.txt`.
- conftest hardcode localhost URLs.

---

## 2. Phase 5A — Polish & Quick Wins

**Mục tiêu:** thu hết quick wins trước khi phóng feature mới.
**Effort:** 1-2 ngày.
**Prereq:** Phase 4 cutover (jina-v2) đã verify gate Criterion A/B.

### 5A.1 Hub MCP wire compaction
- File: `hub/mcp_server/server.py:38,52`
- Drop `indent=2`, dùng `separators=(',', ':')`.
- Acceptance: response size giảm ≥25% trên `semantic_search_code` 10-result fixture.

### 5A.2 Response size caps
- `hub/mcp_server/tools/semantic_search.py:111` — snippet 500 → 250 chars.
- `hub/mcp_server/tools/dependency_graph.py:19-24` — cap 50 nodes / 100 edges + warning log khi truncate.
- Acceptance: `get_dependency_graph` trên file >100 edges trả về ≤30KB và log warning.

### 5A.3 Watcher HTTP transport tuning
- File: `watcher/sender/client.go`
- Set `Transport: &http.Transport{ MaxIdleConns: 100, IdleConnTimeout: 90s, MaxIdleConnsPerHost: 10, DisableKeepAlives: false }`.
- Acceptance: long-running watcher (24h soak) không leak FD.

### 5A.4 Watcher backpressure
- File: `watcher/watcher/fs.go:434` + `watcher/watcher/queue.go`
- `Enqueue(ctx context.Context, ...)` với deadline 2s; caller pass ctx từ fsnotify loop.
- Acceptance: SQLite slow → fsnotify loop không stall, log warning + drop oldest event.

### 5A.5 Test deps + config
- Pin `syrupy>=4.0` trong `requirements-dev.txt`.
- Conftest URLs configurable qua env: `HUB_URL`, `QDRANT_URL`, `MEMGRAPH_BOLT_URL`, `EMBED_URL`.
- Acceptance: `pytest tests` chạy được trên CI matrix với env override.

---

## 3. Phase 5B — Search Quality (theo roadmap Phase B)

**Mục tiêu:** scope theo machine + tăng precision.
**Effort:** 3-5 ngày.
**Prereq:** 5A xong.

### 5B.1 machine_id parameter
- MCP `semantic_search_code` thêm optional `machine_id`.
- Hub `/search` filter Qdrant payload `machine_id == ?`.
- Watcher đã gửi machine_id (verify tại sender).
- Acceptance: query với `machine_id="dev-mac"` chỉ trả về point từ máy đó.

### 5B.2 Embedding prefixes (jina-v2 task-specific)
- Embed service: phân biệt `query: ...` vs `passage: ...` prefix.
- Hub `/search` truyền `task=query`; `/embed` ingest dùng `task=passage`.
- Acceptance: precision@10 cải thiện ≥10% trên eval baseline (mới re-capture sau jina-v2).

### 5B.3 Hybrid ranking refinement
- Thêm path-prefix boost (file_path startswith query token) + exact-token overlap weight.
- Tweak RRF k constant theo eval gate.
- Acceptance: Phase 4.5 acceptance gate vẫn pass với reranking mới.

### 5B.4 Project-tree caps
- `get_project_tree`: thêm `max_depth=8, max_entries=2000, max_chars=120000`.
- Append `<truncated: N entries hidden>` marker khi cap hit.
- Acceptance: monorepo >5000 files trả về ≤120KB response.

---

## 4. Phase 5C — Reliability & Observability

**Mục tiêu:** đóng gap quan sát + reconciliation.
**Effort:** 4-6 ngày.
**Có thể chạy parallel với 5B (file độc lập).**

### 5C.1 Phase 2E.6 — Semantic reconciliation
- Low-frequency loop trong watcher (mỗi 5 phút): scan SQLite `entities` Go có hash mới mà `semantic_jobs` không có row hoặc row dead với hash cũ → re-enqueue.
- Acceptance: integration test mới: kill semantic worker giữa chừng, restart, xác nhận missed job được re-enqueued.

### 5C.2 Latency instrumentation
- FastAPI middleware: P50/P95/P99 buckets cho `/embed`, `/search`, `/batch`.
- Expose qua `/stats` (đã có endpoint, mở rộng).
- Acceptance: `/stats` trả về `latency: { embed: { p50, p95, p99 }, search: ..., batch: ... }`.

### 5C.3 mypy on hub/
- Add `mypy hub/` step vào CI.
- Bắt đầu với `--ignore-missing-imports --no-strict-optional` rồi tighten.
- Acceptance: CI pass; baseline error count tracked.

### 5C.4 Async wrappers cho blocking DB
- File: `hub/api_server/main.py:303,320,328,376`
- Wrap `qdrant.upsert`, `memgraph.atomic_replace_file`, `content.upsert_files` bằng `asyncio.to_thread()`.
- Acceptance: load test 10 concurrent /batch không degrade latency >20%.

---

## 5. Phase 5D — Symbol-level Chunking (roadmap Phase C, biggest payoff)

**Mục tiêu:** 1 vector / function|class|method thay vì file-level → semantic_search precision tăng đáng kể.
**Effort:** 7-10 ngày.
**Prereq:** 5A + 5B xong + jina-v2 stable.

### 5D.1 Symbol slicing
- Watcher dùng `Entity.StartLine`/`EndLine` cắt body từng symbol.
- Embed payload thêm `entity_id, entity_kind, start_line, end_line, file_path`.

### 5D.2 New collection migration
- Tạo `code_v2_jina_symbol` collection mới song song.
- Reindex toàn bộ qua `scripts/reindex.py` với mode symbol.
- Old collection giữ làm fallback cho rollback.

### 5D.3 MCP search update
- `semantic_search_code` trả về function-level snippet + entity metadata.
- File-level RRF re-merge layer cho compatibility (top-K files chứa top-K symbols).

### 5D.4 Eval gate v2
- Capture baseline mới với 12 query trên symbol-level corpus.
- Acceptance: Criterion A (≥80% improvement OR identical top-3), Criterion B (no >2 known-good losses), Jaccard ≥0.95.

---

## 6. Execution Order

```
NOW    → Phase 4 cutover execution (jina-v2 swap, runbook step 1-5)
         ↓
WK 1   → Phase 5A (5 quick wins, parallel hub + watcher tracks)
         ↓
WK 2-3 → Phase 5B + Phase 5C (parallel: search team & reliability team)
         ↓
WK 4-5 → Phase 5D (symbol-level chunking)
```

---

## 7. Open Questions / Risks

1. **Dead-letter handling** — Phase 5C có thể cần Hub-side DLQ surface trong `/stats` (hiện chỉ count).
2. **Vector lag during embed downtime** — partial-success contract đã ổn; Phase 5C.1 reconciliation healable.
3. **Cache staleness sau manual Qdrant ops** — reindex runbook clear `content_hash` column. Phase 5C.2 instrumentation nên expose `vector_lag_ratio` rõ hơn.
4. **Symbol chunking storage cost** — trung bình 5-10 symbol/file × N file → vector count tăng 5-10×. Cần check Qdrant mem_limit khi reindex Phase 5D.

---

**Đề xuất:** sau khi review Plan v5, chốt scope cho Phase 5A trước, mở PR riêng cho từng task 5A.x để diff nhỏ, dễ review, dễ rollback.
