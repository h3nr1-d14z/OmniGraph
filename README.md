# OmniGraph

Distributed RAG-MCP Server for AI code assistance. Read-only, local-first, 4-tool protocol.

## Quick Start — Phase 1: Hub Setup

### 1. Clone & Configure

```bash
cp .env.example .env
# Edit .env for your machine spec
```

### 2. Start Databases

```bash
docker compose up -d qdrant memgraph
```

### 3. Start Embed Service

```bash
cd hub/embed_service
pip install -r requirements.txt
python main.py
```

Or via Docker:

```bash
docker compose up -d embed-service
```

### 4. Test

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
(cd watcher && go test ./...)
```

## Architecture

- **Hub Engine**: Qdrant (vector) + Memgraph (graph) + Embed Service (ONNX/MLX/OpenVINO)
- **Watcher Node**: Go binary per machine (fsnotify + Tree-sitter + CF Tunnel)
- **MCP Server**: 4-tool bridge to Claude Code

## Project Structure

```
OmniGraph/
├── .env.example
├── docker-compose.yml
├── hub/
│   ├── embed_service/     # FastAPI embedding (multi-backend)
│   └── mcp_server/        # MCP 4-tool server
├── watcher/               # Go daemon (Phase 2)
├── scripts/
│   └── setup_tunnel.sh    # Cloudflare Tunnel setup
└── tests/
```

## License

MIT
