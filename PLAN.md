# OmniGraph: Distributed RAG-MCP Server

## Master Plan Review & Phased Execution

**Ngày review:** 2026-04-23
**Trạng thái dự án:** Greenfield (thư mục trống)
**Mục tiêu:** Review kỹ lại SDD, điều chỉnh cho thực tế, chia thành 4 phase plan chi tiết.

**Quyết định kiến trúc (đã confirm):**
- Embedding: Multi-OS — ONNX Runtime là default, MLX là optional cho macOS ARM.
- Graph DB: Memgraph ngay từ Phase 1 (Docker).
- Networking: Cloudflare Tunnel từ đầu.
- AST Parser: Tree-sitter (Go CGO bindings).

---

## 1. Review & Điều Chỉnh Thiết Kế Gốc

### 1.1. Điểm mạnh của SDD gốc
- **Triết lý rõ ràng:** Read-only, 4-tools, local-first — giúp giảm đáng kể attack surface và hallucination.
- **Tech stack hợp lý cho ARM/macOS:** Qdrant (Rust) + Memgraph (C++) + MLX (Apple Silicon) tận dụng tốt phần cứng.
- **Watcher bằng Go:** Binary độc lập, dễ deploy, không cần runtime.

### 1.2. Các điểm cần làm rõ / điều chỉnh

| # | Vấn đề | Quyết định / Giải pháp |
|---|--------|------------------------|
| 1 | **Cloudflare Tunnel** yêu cầu account/cloudflare-cli. | ✅ **Dùng CF Tunnel từ đầu.** Cung cấp script setup `cloudflared`. Watcher config chỉ cần tunnel URL + token. |
| 2 | **Tree-sitter Go bindings** phức tạp (CGO). | ✅ **Dùng Tree-sitter.** Chấp nhận CGO complexity để có AST chính xác đa ngôn ngữ. Build cross-platform cần cẩn thận. |
| 3 | **Memgraph** yêu cầu Docker/khá nặng cho máy dev. | ✅ **Memgraph ngay từ Phase 1.** `docker-compose.yml` bao gồm sẵn. Graph phức tạp cần Cypher query. |
| 4 | **Tombstone deletion** trong Memgraph cần cypher query phức tạp khi file rename/move. | Lưu `file_path` làm unique key. Khi rename → xóa old + insert new. Watcher gửi cả old_path và new_path. |
| 5 | **Batching + Debounce** cần retry logic và circuit breaker nếu Hub tạm down. | Watcher lưu queue vào SQLite local khi Hub unreachable, sync lại khi online. |
| 6 | **MCP Server protocol** (stdio vs SSE) cần quyết định. Claude Code dùng stdio hoặc SSE. | Dùng **stdio** cho local, **SSE** nếu chạy remote qua tunnel. |
| 7 | **Embedding multi-OS.** MLX chỉ chạy macOS ARM. Cần hỗ trợ Linux/Windows/Intel. | ✅ **ONNX Runtime là default backend.** MLX là optional, auto-detect nếu chạy trên macOS ARM. |
| 8 | **Security:** Watcher gửi toàn bộ source code qua HTTP (kể cả qua tunnel). Cần mTLS hoặc ít nhất HTTPS. | CF Tunnel đã có TLS. Thêm Bearer Token auth header cho Watcher → Hub. |

### 1.3. Kiến trúc điều chỉnh (Architecture v1.1)

```
┌─────────────────┐      HTTPS (CF Tunnel)      ┌──────────────────────────┐
│   Go Watcher    │◄────────────────────────────►│      Hub Engine          │
│  (per machine)  │   Batched JSON + Auth Token  │  ┌──────────────────┐   │
│                 │                              │  │  MCP Server      │   │
│  - fsnotify     │                              │  │  (Python/FastMCP)│   │
│  - debounce     │                              │  └────────┬─────────┘   │
│  - Tree-sitter  │                              │           │             │
│  - local queue  │                              │  ┌────────▼─────────┐   │
│                 │                              │  │  Embedding Svc   │   │
└─────────────────┘                              │  │  (ONNX / MLX)    │   │
                                                 │  └────────┬─────────┘   │
                                                 │           │             │
                                                 │  ┌────────▼─────────┐   │
                                                 │  │  Qdrant (vector) │   │
                                                 │  └────────┬─────────┘   │
                                                 │           │             │
                                                 │  ┌────────▼─────────┐   │
                                                 │  │  Memgraph (graph)│   │
                                                 │  └──────────────────┘   │
                                                 └──────────────────────────┘
                                                            │
                                                            │ stdio / SSE
                                                            ▼
                                                 ┌──────────────────────────┐
                                                 │    Claude Code / IDE     │
                                                 └──────────────────────────┘
```

---

## 2. Infrastructure & Deployment (Không Hardcode)

### 2.1. Target Platforms

| Platform | Embedding Backend | Notes |
|----------|-------------------|-------|
| **Linux x86_64 (Intel/AMD)** | ONNX Runtime default | OpenVINO EP optional để tận dụng AVX-512/AMX |
| **macOS ARM (Apple Silicon)** | Auto-detect MLX → fallback ONNX | MLX tận dụng Metal/ANE |
| **macOS Intel / Windows** | ONNX Runtime | Không hỗ trợ MLX |

**Dev/Test Environment (máy hiện tại):**
- Platform: macOS ARM (Apple Silicon) — M-series
- Docker: Docker Desktop để chạy Qdrant + Memgraph containers
- Embedding: MLX là primary (test local), ONNX fallback được verify
- MCP Server: chạy local qua stdio, không expose qua internet

### 2.2. Cấu hình Resource Limits (`.env`)

Toàn bộ resource limits, ports, và paths phải đọc từ `.env` / `config.yaml`. **Không hardcode** trong source hoặc docker-compose.

```
# .env.example — copy sang .env rồi điều chỉnh theo spec máy
QDRANT_PORT=6333
MEMGRAPH_BOLT_PORT=7687
MEMGRAPH_HTTP_PORT=3000

# Resource limits — điều chỉnh theo RAM/CPU máy deploy
QDRANT_MEM_LIMIT=8g
MEMGRAPH_MEM_LIMIT=6g
EMBED_WORKERS=4

# Embedding backend: onnx | mlx | auto
EMBED_BACKEND=auto

# Networking
CF_TUNNEL_URL=https://your-tunnel.trycloudflare.com
HUB_AUTH_TOKEN=changeme
```

### 2.3. Docker Compose — Dynamic

`docker-compose.yml` phải dùng variable substitution:
```yaml
services:
  qdrant:
    image: qdrant/qdrant:latest
    mem_limit: ${QDRANT_MEM_LIMIT:-4g}
    environment:
      - QDRANT__STORAGE__ON_DISK_PAYLOAD=true
  memgraph:
    image: memgraph/memgraph:latest
    mem_limit: ${MEMGRAPH_MEM_LIMIT:-4g}
```

### 2.4. Intel CPU Optimization (Optional)

- Cài `onnxruntime-openvino` thay vì `onnxruntime` để dùng OpenVINO Execution Provider.
- Tự động detect qua `onnxruntime.get_available_providers()`.
- Không bắt buộc — nếu không có OpenVINO thì fallback về CPUExecutionProvider.

### 2.5. Security Architecture (Custom Domain)

Hub expose qua domain riêng: `knowledge-db.example.com`. Bảo mật 3 lớp:

#### Lớp 1: Transport (TLS + CF Tunnel)
- Cloudflare Tunnel làm reverse proxy. Không mở port public, không cần public IP.
- DNS: CNAME `knowledge-db` → `<tunnel-id>.cfargotunnel.com`
- TLS 1.3 termination tại Cloudflare edge.
- Script: `scripts/setup_tunnel.sh` — tạo tunnel, route DNS, output config.

#### Lớp 2: Cloudflare Access (Zero Trust)
- **Self-hosted Application** trong Cloudflare Zero Trust dashboard.
- **Service Auth (mTLS token)** cho Watcher → bypass human login.
- **Identity provider** (Email OTP / Google / GitHub) cho admin access Memgraph UI.
- Watcher dùng Service Token để authenticate với CF Access.

#### Lớp 3: Application Auth (Bearer Token)
```http
POST https://knowledge-db.example.com/batch
Authorization: Bearer <machine_token>
X-Machine-ID: dev-macbook-m1
```
- Hub verify token trong allowlist (`.env` hoặc `config/machines.yaml`).
- Rate limit: 100 req/min per `machine_id`.
- WAF rule: block request không có `Authorization` header.

#### MCP Server không expose qua internet
```
Claude Code (local macOS) ← stdio → MCP Server (localhost)
                                   ↓
                              HTTP localhost → Hub (Docker)
                                   ↓
                              CF Tunnel → knowledge-db.example.com
```
- MCP Server chạy local cùng máy với Claude Code.
- Watcher (máy khác / chính máy này) gọi Hub qua HTTPS domain.

#### Secret Management
- `.env` + `.gitignore` — không commit secret.
- Watcher token lưu tại `~/.config/omnigraph/watcher.yaml` (chmod 600).
- Hub tokens lưu trong `.env` trên server (chmod 600).
- Cloudflare Service Token lưu trong `~/.cloudflared/` (auto-managed).

---

## 3. Phased Execution Plans

### Phase 1: Hub Setup (Nền móng)
**Mục tiêu:** Có Hub chạy local với Qdrant + Memgraph, nhúng được code và query lại.

**Files cần tạo:**
```
hub/
  docker-compose.yml          # Qdrant + Memgraph (dùng ${VAR:-default})
  .env.example                # Template cấu hình resource, ports, backend
  embed_service/
    main.py                   # FastAPI service, endpoint /embed
    backends/
      onnx_backend.py         # Default: ONNX Runtime + nomic-embed-text ONNX
      mlx_backend.py          # Optional: MLX (auto-detect macOS ARM)
      openvino_backend.py     # Optional: OpenVINO EP cho Intel CPU
    requirements.txt          # onnxruntime | onnxruntime-openvino | mlx
  mcp_server/
    server.py                 # FastMCP hoặc official MCP SDK
    tools/
      semantic_search.py      # Query Qdrant
      dependency_graph.py     # Query Memgraph via Cypher
      read_file.py            # Read file content
      project_tree.py         # Return tree string
    models/
      schema.py               # Pydantic models cho request/response
    db/
      qdrant_client.py
      memgraph_client.py      # Cypher query wrapper via mgclient/gqlalchemy
    requirements.txt
```

**Bước thực hiện:**
1. Tạo `.env.example` với các biến resource limits, ports, backend selection.
2. Tạo `docker-compose.yml` dùng variable substitution (`${QDRANT_MEM_LIMIT:-4g}`). Không hardcode ports hay limits.
3. Viết `embed_service/main.py` với auto-detect backend theo thứ tự: `EMBED_BACKEND=openvino` → `onnx` → `mlx` → `auto` (detect platform).
4. Tải model `nomic-embed-text-v1.5` dạng ONNX từ HuggingFace (hoặc convert từ MLX).
5. Test embed + upsert 10 đoạn code vào Qdrant, query lại.
6. Tạo Memgraph schema: nodes `Entity` (file_path, name, type, project, content_hash), edges `DEPENDS_ON` (from, to, relation_type).
7. Viết test script end-to-end: embed → insert Qdrant + Memgraph → semantic search → dependency query Cypher.
8. Viết script setup `cloudflared tunnel` cho Hub (tạo tunnel, lấy URL, cấu hình Watcher).

**Rủi ro & giải pháp:**
- ONNX model size lớn → dùng quantized ONNX (`onnx/model_quantized.onnx`) hoặc `optimum` CLI để optimize.
- Memgraph RAM → giới hạn qua `.env` `MEMGRAPH_MEM_LIMIT`, không hardcode.
- Qdrant RAM usage → config `on_disk: true` + `mmap_threshold` qua env, collection dim=768.
- CF Tunnel setup phức tạp → cung cấp script `scripts/setup_tunnel.sh` tự động hóa.
- OpenVINO không khả dụng → fallback seamless sang CPUExecutionProvider.

---

### Phase 2: Go Watcher Node (Trạm thu thập)
**Mục tiêu:** Binary Go độc lập theo dõi file system và gửi batch về Hub.

**Files cần tạo:**
```
watcher/
  main.go                     # Entry point, config parsing (subcommands: init, watch)
  config/
    config.go                 # Struct config, env vars, defaults
  watcher/
    fs.go                     # fsnotify recursive, debounce, batching, project grouping
    ignore.go                 # .gitignore/.dockerignore parser + hardcoded excludes
    ast.go                    # Tree-sitter AST extraction (CGO bindings)
    project.go                # Auto-discovery: scan markers → resolve file path → project
    queue.go                  # Local SQLite queue khi Hub offline
  sender/
    client.go                 # HTTP client, retry, auth header
  models/
    event.go                  # Structs: FileEvent, Entity, BatchPayload
  go.mod
```

**Bước thực hiện:**
1. Khởi tạo Go module, integrate `fsnotify`.
2. Viết `ignore.go`: parse `.gitignore` dùng thư viện `github.com/sabhiram/go-gitignore` + hardcoded excludes.
3. Viết `project.go`: auto-discovery projects bên trong `watch_root`. Markers: `.git`, `go.mod`, `package.json`, `Cargo.toml`, `pyproject.toml`, ...
   - Scan 1 lần khi startup → map[dir_path]project_name
   - Resolve: từ file_path đi lên parent cho đến khi gặp marker → return project name
4. Viết `fs.go`: debounce 3s, batch tối đa 50 events hoặc 5s interval. Group events by project rồi gửi từng batch.
5. Viết `ast.go`: dùng `github.com/smacker/go-tree-sitter` (CGO bindings). Hỗ trợ Go, Python, JavaScript, TypeScript, Rust, Java.
6. Viết `queue.go`: SQLite local lưu events khi Hub unreachable.
7. Viết `sender/client.go`: POST `/batch` qua CF Tunnel URL với Bearer token, retry 3 lần exponential backoff.
8. Cross-platform build script: `scripts/build_watcher.sh` hỗ trợ darwin/linux/windows (lưu ý CGO cần compiler tương ứng).
8. Test: chạy watcher trên 1 repo Go, sửa file, đợi 3s, verify Hub nhận batch qua tunnel.

**Rủi ro & giải pháp:**
- **Tree-sitter CGO phức tạp:** Cần C compiler (gcc/clang) khi build. Trên Windows cần MinGW-w64. Cung cấp script cài đặt dependency.
- **fsnotify không hỗ trợ recursive trên macOS FSEvents** → dùng `github.com/rjeczalik/notify` hoặc tự implement recursive.
- **Large file change storm (git checkout branch)** → throttle: max 100 events/sec, bỏ qua nếu vượt ngưỡng.
- **File rename** → Watcher phát hiện CREATE + DELETE gần nhau → merge thành RENAME event gửi lên Hub.
- **Binary size với CGO** → dùng `upx` hoặc `-ldflags="-s -w"` để giảm size.

---

### Phase 3: MCP Server (The Brain)
**Mục tiêu:** Kết nối Hub với Claude Code qua MCP protocol với đúng 4 tools.

**Files cần tạo/sửa:**
```
hub/mcp_server/
  server.py                   # FastMCP hoặc official MCP SDK
  tools/
    __init__.py
    semantic_search.py        # Query Qdrant, format JSON
    dependency_graph.py       # Query SQLite graph, BFS/DFS
    read_file.py              # Đọc file từ machine_id + path
    project_tree.py           # Trả về tree string
  auth/
    middleware.py             # Verify machine token
  config.yaml                 # Danh sách machines được phép
```

**Schema 4 Tools (chính xác cho MCP):**

```json
{
  "tools": [
    {
      "name": "semantic_search_code",
      "description": "Find code snippets/files by semantic meaning",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "Natural language description"},
          "project_scope": {"type": "string", "description": "Optional project name to filter"}
        },
        "required": ["query"]
      }
    },
    {
      "name": "get_dependency_graph",
      "description": "Analyze call flow and dependencies",
      "inputSchema": {
        "type": "object",
        "properties": {
          "entity_name": {"type": "string"},
          "direction": {"type": "string", "enum": ["upstream", "downstream", "both"]}
        },
        "required": ["entity_name", "direction"]
      }
    },
    {
      "name": "read_full_file",
      "description": "Read entire file content into context",
      "inputSchema": {
        "type": "object",
        "properties": {
          "machine_id": {"type": "string"},
          "file_path": {"type": "string"}
        },
        "required": ["machine_id", "file_path"]
      }
    },
    {
      "name": "get_project_tree",
      "description": "Get directory tree overview",
      "inputSchema": {
        "type": "object",
        "properties": {
          "machine_id": {"type": "string"},
          "project_name": {"type": "string"}
        },
        "required": ["machine_id", "project_name"]
      }
    }
  ]
}
```

**Bước thực hiện:**
1. Cài `fastmcp` hoặc `mcp` SDK (Python).
2. Implement 4 tools:
   - `semantic_search`: embed query → search Qdrant → return top-k với file_path, snippet, score.
   - `dependency_graph`: chạy Cypher query trên Memgraph (BFS/DFS theo direction). Return nodes + edges.
   - `read_full_file`: Hub lưu nội dung file vào local storage (hoặc query từ Memgraph node property `content`) khi Watcher gửi lên.
   - `get_project_tree`: query Memgraph hoặc local index cho cây thư mục project.
3. Viết `memgraph_client.py`: dùng `gqlalchemy` hoặc `neo4j` driver (Memgraph tương thích Bolt protocol).
4. `read_full_file` cần truy cập filesystem của machine — giải pháp: Hub lưu nội dung file từ Watcher (hoặc Watcher phục vụ file qua HTTP). **Quyết định:** Hub lưu nội dung file vào SQLite (content table) khi Watcher gửi lên → read_full_file query SQLite.
5. `get_project_tree` cũng query từ SQLite (lưu cây thư mục khi Watcher gửi TREE event ban đầu).
6. Auth middleware: verify Bearer token từ Watcher, verify machine_id hợp lệ.
7. Test với `mcp-inspector` hoặc Claude Code local.

---

### Phase 4: Integration Testing (Thực chiến)
**Mục tiêu:** Chạy 2 Watcher trên 2 project khác nhau, test cross-project reasoning.

**Kịch bản test:**
1. Project A: Repo Go (API server) — Watcher #1 gửi lên Hub.
2. Project B: Repo TypeScript (Frontend) — Watcher #2 gửi lên Hub.
3. Mở Claude Code, kết nối MCP server.
4. Prompt: *"Project A có struct User ở đâu? Viết interface tương ứng cho Project B."*
5. AI dùng `semantic_search_code` tìm struct User → `read_full_file` đọc → `get_project_tree` xem cấu trúc Project B → viết interface.

**Metrics thành công:**
- Semantic search top-3 chứa đúng file struct User.
- Dependency graph (Memgraph Cypher) hiển thị đúng import chain giữa các file.
- Watcher không miss event khi save liên tục 10 lần trong 5s.
- Tombstone: xóa file → Qdrant không trả về kết quả + Memgraph node/edge bị xóa.
- Cross-project: AI dùng `semantic_search_code` trên Project A, `read_full_file` đọc struct, `get_project_tree` xem Project B, viết interface đúng.

---

## 3. Project Structure Tổng hợp (Đề xuất)

```
OmniGraph/
├── README.md
├── .env.example                    # Template config resource, ports, backend
├── docker-compose.yml              # Phase 1 (dùng ${VAR:-default})
├── scripts/
│   ├── setup_tunnel.sh             # Cloudflare Tunnel auto-setup
│   └── build_watcher.sh            # Cross-platform Go build (CGO)
├── hub/
│   ├── embed_service/              # Phase 1
│   │   ├── main.py
│   │   ├── mlx_backend.py
│   │   ├── onnx_fallback.py
│   │   └── requirements.txt
│   └── mcp_server/                 # Phase 3
│       ├── server.py
│       ├── tools/
│       ├── auth/
│       └── requirements.txt
├── watcher/                        # Phase 2
│   ├── main.go
│   ├── config/
│   ├── watcher/
│   ├── sender/
│   ├── models/
│   └── go.mod
├── tests/                          # Phase 4
│   ├── test_hub.py
│   ├── test_watcher.go
│   └── fixtures/
└── docs/
    ├── ARCHITECTURE.md
    └── MCP_SCHEMA.json
```

---
---

## 4. Quyết định Kiến trúc đã Xác nhận

| Hạng mục | Quyết định |
|----------|-----------|
| Embedding Backend | ONNX Runtime (default, multi-OS). MLX optional auto-detect trên macOS ARM. |
| Graph Database | Memgraph ngay từ Phase 1 (Docker). |
| Networking | Cloudflare Tunnel từ đầu. Watcher → Hub qua HTTPS tunnel. |
| AST Parser | Tree-sitter (Go CGO bindings). Hỗ trợ đa ngôn ngữ từ đầu. |
| File Content Storage | SQLite local trên Hub (lưu content từ Watcher). Memgraph chỉ lưu metadata + graph. |
