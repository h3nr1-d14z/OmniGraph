"""Acceptance tests for Phase Cleanup deliverables.

Covers:
- NFC path normalization on Hub /batch ingress (Cleanup-6)
- Hub /stats watcher_queue graceful degrade on unreachable (Cleanup-5)
- print() removed from production hub paths (Cleanup-1, sanity recheck)
- structlog config wires uvicorn loggers (Cleanup-1)
- queue_stats env constants present (Cleanup-5)
"""

import asyncio
import os
import re
import sys
import unicodedata
from pathlib import Path
from unittest.mock import AsyncMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hub"))
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))

from api_server.main import _normalize_path, _fetch_watcher_queue_stats  # noqa: E402


def test_normalize_path_returns_nfc_for_decomposed_input():
    """HFS+ NFD form 'café.py' must normalize to NFC 'café.py'."""
    nfd = "caf" + "é" + ".py"  # decomposed (e + combining acute)
    nfc = unicodedata.normalize("NFC", nfd)
    assert nfd != nfc, "test setup invalid: NFD and NFC must differ"
    assert _normalize_path(nfd) == nfc


def test_normalize_path_idempotent_on_nfc():
    nfc = "café.py"  # already NFC
    assert _normalize_path(nfc) == nfc


def test_normalize_path_handles_empty():
    assert _normalize_path("") == ""


def test_normalize_path_ascii_passthrough():
    assert _normalize_path("/src/main.go") == "/src/main.go"


def test_fetch_watcher_queue_stats_returns_none_on_unreachable(monkeypatch):
    """Watcher offline → graceful degrade, no exception, no 5xx."""
    monkeypatch.setattr("api_server.main.WATCHER_QUEUE_STATS_URL", "http://localhost:1/queue/stats")

    class FailingClient:
        async def get(self, *args, **kwargs):
            raise ConnectionError("simulated unreachable")

    result = asyncio.run(_fetch_watcher_queue_stats(FailingClient()))
    assert result is None


def test_fetch_watcher_queue_stats_returns_none_on_empty_url(monkeypatch):
    monkeypatch.setattr("api_server.main.WATCHER_QUEUE_STATS_URL", "")
    result = asyncio.run(_fetch_watcher_queue_stats(AsyncMock()))
    assert result is None


def test_fetch_watcher_queue_stats_passes_through_on_success(monkeypatch):
    monkeypatch.setattr("api_server.main.WATCHER_QUEUE_STATS_URL", "http://stub")

    class OkClient:
        async def get(self, *args, **kwargs):
            class R:
                def raise_for_status(self): pass
                def json(self): return {"state_counts": {"pending": 3}, "pending_events": 0}
            return R()

    result = asyncio.run(_fetch_watcher_queue_stats(OkClient()))
    assert result == {"state_counts": {"pending": 3}, "pending_events": 0}


def test_no_production_print_calls_in_hub():
    """Cleanup-1 sanity: zero print() in hub production code (not scripts/)."""
    pattern = re.compile(r"\bprint\(")
    offenders = []
    for root in [REPO / "hub" / "api_server", REPO / "hub" / "embed_service", REPO / "hub" / "mcp_server"]:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text()
            for n, line in enumerate(text.splitlines(), 1):
                # ignore docstrings/comments containing 'print('
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                    continue
                if pattern.search(line):
                    offenders.append(f"{path}:{n}: {line.strip()}")
    assert not offenders, f"production print() calls remain:\n" + "\n".join(offenders)


def test_logging_config_module_exists():
    cfg = REPO / "hub" / "logging_config.py"
    assert cfg.exists(), "hub/logging_config.py missing"
    src = cfg.read_text()
    assert "configure_logging" in src
    assert "structlog" in src


def test_flake8_config_blocks_print_in_hub():
    cfg = REPO / ".flake8"
    assert cfg.exists()
    text = cfg.read_text()
    assert "T20" in text, "flake8-print rule (T20) not configured"


def test_watcher_queue_stats_env_constants_exist():
    src = (REPO / "hub" / "api_server" / "main.py").read_text()
    assert "WATCHER_QUEUE_STATS_URL" in src
    assert "WATCHER_QUEUE_STATS_TIMEOUT" in src
    assert "9100" in src, "default watcher port 9100 not referenced"


def test_tunnel_script_uses_real_hostname_and_port():
    src = (REPO / "scripts" / "setup_tunnel.sh").read_text()
    assert "trycloudflare" not in src, "setup_tunnel.sh still references trycloudflare.com"
    assert "TUNNEL_HOSTNAME" in src
    assert "HUB_API_PORT" in src
    assert "9000" in src, "default port 9000 (hub-api) missing"
    assert "knowledge-db.example.com" in src, "real domain default missing"
