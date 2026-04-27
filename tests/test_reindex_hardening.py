"""Acceptance tests for scripts/reindex.py hardening (US-014).

Covers:
  - --clean drops target collection via Qdrant DELETE
  - --skip-machine-ids filters rows out of the re-emit set
  - validate_active_collection aborts on Hub /stats mismatch
  - --no-validate bypasses the check
  - Backward compat: no new flags → original behavior
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "reindex.py"

spec = importlib.util.spec_from_file_location("reindex", SCRIPT)
assert spec and spec.loader
reindex = importlib.util.module_from_spec(spec)
sys.modules["reindex"] = reindex
spec.loader.exec_module(reindex)


# ---------- arg parsing ----------------------------------------------------


def test_argparse_supports_new_flags():
    args = reindex.parse_args([
        "--collection", "code_v2_jina",
        "--clean",
        "--skip-machine-ids", "test-machine-01,m1",
        "--no-validate",
    ])
    assert args.clean is True
    assert args.skip_machine_ids == "test-machine-01,m1"
    assert args.validate is False


def test_argparse_defaults_validate_on():
    args = reindex.parse_args(["--collection", "code_v2_jina"])
    assert args.clean is False
    assert args.skip_machine_ids == ""
    assert args.validate is True


# ---------- skip-machine-ids filter ---------------------------------------


def test_skip_machine_ids_excludes_matching_rows(tmp_path):
    db = tmp_path / "content.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE files (machine_id TEXT, project TEXT, file_path TEXT, content TEXT)"
    )
    conn.executemany(
        "INSERT INTO files VALUES (?,?,?,?)",
        [
            ("dev-01", "p", "a.go", "x"),
            ("test-machine-01", "p", "b.go", "y"),
            ("m1", "p", "c.go", "z"),
            ("dev-01", "p", "d.go", "q"),
        ],
    )
    conn.commit()
    conn.close()

    rows = reindex.fetch_files_from_content_store(str(db),
                                                  skip_machine_ids={"test-machine-01", "m1"})
    machine_ids = {r[0] for r in rows}
    assert machine_ids == {"dev-01"}
    assert len(rows) == 2


def test_skip_machine_ids_empty_set_returns_all(tmp_path):
    db = tmp_path / "content.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE files (machine_id TEXT, project TEXT, file_path TEXT, content TEXT)"
    )
    conn.executemany(
        "INSERT INTO files VALUES (?,?,?,?)",
        [("dev-01", "p", "a.go", "x"), ("test-machine-01", "p", "b.go", "y")],
    )
    conn.commit()
    conn.close()
    assert len(reindex.fetch_files_from_content_store(str(db), skip_machine_ids=None)) == 2
    assert len(reindex.fetch_files_from_content_store(str(db), skip_machine_ids=set())) == 2


# ---------- collection validation -----------------------------------------


def test_validate_active_collection_pass():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/stats"
        return httpx.Response(200, json={"qdrant": {"collection": "code_v2_jina"}})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        ok, actual = reindex.validate_active_collection(
            http, "http://hub", "tok", "code_v2_jina",
        )
    assert ok is True
    assert actual == "code_v2_jina"


def test_validate_active_collection_mismatch():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"qdrant": {"collection": "code_v1_nomic"}})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        ok, actual = reindex.validate_active_collection(
            http, "http://hub", "tok", "code_v2_jina",
        )
    assert ok is False
    assert actual == "code_v1_nomic"


def test_validate_active_collection_missing_field():
    """Hub /stats returns degraded shape (qdrant block missing)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "degraded", "qdrant": None})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        ok, err = reindex.validate_active_collection(
            http, "http://hub", "tok", "code_v2_jina",
        )
    assert ok is False
    assert "missing" in (err or "")


def test_validate_active_collection_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated unreachable")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        ok, err = reindex.validate_active_collection(
            http, "http://hub", "tok", "code_v2_jina",
        )
    assert ok is False
    assert "hub_stats_unreachable" in (err or "")


# ---------- drop_collection ----------------------------------------------


def test_drop_collection_success(monkeypatch):
    captured = {}

    class _Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = ""

    class _Client:
        def __init__(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def delete(self, url, timeout=None):
            captured["url"] = url
            return _Resp(200)

    monkeypatch.setattr(reindex.httpx, "Client", lambda: _Client())
    ok, err = reindex.drop_collection("http://qdrant:6333", "code_v2_jina")
    assert ok is True
    assert err is None
    assert captured["url"] == "http://qdrant:6333/collections/code_v2_jina"


def test_drop_collection_404_treated_as_success(monkeypatch):
    """Already-absent collection is fine (idempotent --clean)."""
    class _Resp:
        status_code = 404
        text = "not found"

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def delete(self, *args, **kwargs): return _Resp()

    monkeypatch.setattr(reindex.httpx, "Client", lambda: _Client())
    ok, _ = reindex.drop_collection("http://qdrant:6333", "absent")
    assert ok is True


def test_drop_collection_5xx_returns_error(monkeypatch):
    class _Resp:
        status_code = 503
        text = "service unavailable"

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def delete(self, *args, **kwargs): return _Resp()

    monkeypatch.setattr(reindex.httpx, "Client", lambda: _Client())
    ok, err = reindex.drop_collection("http://qdrant:6333", "code_v2_jina")
    assert ok is False
    assert "503" in (err or "")
