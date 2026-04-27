"""US-008 acceptance: sliding-window whole-file fallback + chunk caps."""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hub" / "api_server"))
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))

# Importing main.py triggers DB clients on import-side-effects (lifespan).
# We only need _sliding_windows and _build_embedding_chunks; those are pure.
# Patch settings via env BEFORE import.
os.environ.setdefault("EMBED_MAX_CHARS", "32000")

# Use a minimal FileEvent stand-in (avoid Pydantic + DB imports).
from dataclasses import dataclass, field


@dataclass
class FakeEntity:
    name: str
    type: str
    start_line: int = 0
    end_line: int = 0


@dataclass
class FakeFileEvent:
    path: str
    content: str
    content_hash: str = "h"
    entities: list = field(default_factory=list)


# Direct import works because main.py is import-safe (lifespan only runs on
# app startup, not module import). But it does try to import db.* which needs
# sys.path. Already added above.
import main  # noqa: E402


def test_sliding_windows_basic():
    """Window=2400, stride=600 → starts at 0,600,1200,1800,2400,3000 for 5000-char text."""
    text = "x" * 5000
    out = main._sliding_windows(text, 2400, 600)
    assert len(out) == 6, [(s, e) for s, e, _ in out]
    starts = [s for s, _, _ in out]
    assert starts == [0, 600, 1200, 1800, 2400, 3000]
    # Last window's end is clamped to content length (covers tail).
    assert out[-1][1] == 5000


def test_sliding_windows_empty_input():
    assert main._sliding_windows("", 2400, 600) == []


def test_sliding_windows_short_input_single_window():
    out = main._sliding_windows("hello", 2400, 600)
    assert len(out) == 1
    assert out[0] == (0, 5, "hello")


def test_whole_file_fallback_uses_sliding_window():
    """File with no entities → sliding-window fallback (multiple chunks)."""
    content = "x" * 6000
    ev = FakeFileEvent(path="/p/x.go", content=content)
    chunks = main._build_embedding_chunks(ev, "m1", "p1")
    assert len(chunks) > 1, f"expected multi-window, got {len(chunks)} chunks"
    for c in chunks:
        assert c["chunk_id"].startswith("win"), c["chunk_id"]
        assert c["entity"] == "" or c["entity_type"] == "file"


def test_whole_file_fallback_covers_full_content_within_cap():
    """For files within MAX_CHUNKS_PER_FILE, sliding window covers tail.
    Previous bug: single-chunk fallback truncated at 2000 chars, dropping
    everything after first ~40 lines."""
    # 64 chunks × 600 stride + 2400 first window = ~41KB max coverage.
    # Use 30KB content (well under cap) to verify tail coverage.
    content = "AAA" + ("x" * 29_994) + "ZZZ"  # 30000 chars total
    ev = FakeFileEvent(path="/p/medium.go", content=content)
    chunks = main._build_embedding_chunks(ev, "m1", "p1")
    last = chunks[-1]
    assert "ZZZ" in str(last["text"]), "tail of file dropped (sliding window did not reach end)"


def test_whole_file_fallback_caps_oversized_file_with_warning(capsys):
    """200KB file > MAX_CHUNKS_PER_FILE * stride → cap, log warning, drop tail."""
    content = "y" * 200_000
    ev = FakeFileEvent(path="/p/huge.js", content=content)
    chunks = main._build_embedding_chunks(ev, "m1", "p1")
    assert len(chunks) == main.MAX_CHUNKS_PER_FILE
    captured = capsys.readouterr()
    assert "/p/huge.js" in captured.out and "capped" in captured.out


def test_max_chunks_per_file_caps_pathological_input():
    """200KB file produces ≤ MAX_CHUNKS_PER_FILE chunks (default 64)."""
    content = "y" * 200_000
    ev = FakeFileEvent(path="/p/huge.js", content=content)
    chunks = main._build_embedding_chunks(ev, "m1", "p1")
    assert len(chunks) <= main.MAX_CHUNKS_PER_FILE
    assert len(chunks) == main.MAX_CHUNKS_PER_FILE  # exactly at cap


def test_per_entity_cap_at_max_embed_chars():
    """Per-entity slice respects MAX_EMBED_CHARS (32000 default in Phase 4)."""
    body = "z" * 50_000
    content = body + "\n"
    ev = FakeFileEvent(
        path="/p/big_func.go",
        content=content,
        entities=[FakeEntity(name="Big", type="function", start_line=1, end_line=2)],
    )
    chunks = main._build_embedding_chunks(ev, "m1", "p1")
    assert len(chunks) == 1
    text = str(chunks[0]["text"])
    assert len(text) == main.MAX_EMBED_CHARS, f"expected len={main.MAX_EMBED_CHARS}, got {len(text)}"
