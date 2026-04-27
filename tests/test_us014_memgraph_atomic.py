"""US-014 acceptance: per-file atomic delete + upsert in Memgraph.

Hub `_handle_upserts` previously called `delete_file()`, `upsert_entities()`,
and `upsert_relations()` on three separate auto-commit sessions. If
`upsert_entities` raised mid-batch, the delete had already committed —
leaving torn graph state until the next ingest. This suite verifies:

1. `atomic_replace_file` wraps delete + entity upsert + relation upsert in
   one explicit transaction; a mid-tx exception rolls back so existing
   state is preserved.
2. `_with_retry` catches `neo4j.exceptions.TransientError` and retries
   with exponential backoff up to `max_attempts`, then re-raises.
3. `_ensure_schema` adds the composite uniqueness constraint and is
   idempotent across re-opens.
4. `_handle_upserts` regroups entities/relations by file_path and calls
   `atomic_replace_file` once per content-bearing file.
"""

import asyncio
import os
import sys
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "mcp_server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub"))

# Suppress neo4j warnings about deprecated APIs in tests.
import warnings

from db.memgraph_client import MemgraphCodeGraph

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _make_client_with_fake_driver(driver: MagicMock) -> MemgraphCodeGraph:
    """Build a MemgraphCodeGraph that uses ``driver`` instead of a live Bolt.

    Bypasses ``__init__`` so we don't open a real connection or run schema
    setup against a live Memgraph.
    """
    client = object.__new__(MemgraphCodeGraph)
    client.uri = "bolt://localhost:7687"
    client.user = ""
    client.password = ""
    client._driver = driver
    return client


def _stub_session_with_tx(tx: MagicMock) -> MagicMock:
    """Build a session context manager whose begin_transaction() returns tx."""
    session = MagicMock()
    session.begin_transaction.return_value = tx
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_atomic_replace_file_rolls_back_on_error():
    """If the entity-upsert run raises, the transaction is rolled back and
    we never reach commit. Caller sees the original exception."""
    tx = MagicMock()
    # First two tx.run calls (delete + delete-file) succeed; entity upsert
    # raises. _run_upsert_relations should not be called after the raise.
    delete_entities_call = MagicMock()
    delete_file_call = MagicMock()
    prune_call = MagicMock()
    boom = RuntimeError("entity merge failed mid-tx")

    def tx_run(query: str, **kwargs: Any) -> MagicMock:
        if "DETACH DELETE e" in query:
            return delete_entities_call
        if "DETACH DELETE f" in query:
            return delete_file_call
        if "WHERE (n:Module OR n:UnresolvedSymbol" in query:
            return prune_call
        if "MERGE (e:Entity" in query and "ent.name" in query:
            raise boom
        return MagicMock()

    tx.run.side_effect = tx_run

    driver = MagicMock()
    driver.session.return_value = _stub_session_with_tx(tx)
    client = _make_client_with_fake_driver(driver)

    with pytest.raises(RuntimeError, match="entity merge failed mid-tx"):
        client.atomic_replace_file(
            "/src/main.go",
            "m1",
            entities=[
                {
                    "machine_id": "m1",
                    "project": "demo",
                    "file_path": "/src/main.go",
                    "name": "main",
                    "type": "function",
                    "content_hash": "abc",
                    "start_line": 1,
                    "end_line": 5,
                }
            ],
            relations=[],
        )

    tx.commit.assert_not_called()
    tx.rollback.assert_called_once()


def test_atomic_replace_file_retries_on_transient_error():
    """First attempt raises TransientError, second succeeds. Final state
    is committed; rollback is called for the failed attempt only."""
    from neo4j.exceptions import TransientError

    attempt_state = {"calls": 0}

    def session_factory():
        attempt_state["calls"] += 1
        tx = MagicMock()
        if attempt_state["calls"] == 1:
            tx.run.side_effect = TransientError("memgraph conflict")
        else:
            tx.run.return_value = MagicMock()
        # Stash on the parent so the test can inspect.
        attempt_state.setdefault("txs", []).append(tx)
        return _stub_session_with_tx(tx)

    driver = MagicMock()
    driver.session.side_effect = session_factory
    client = _make_client_with_fake_driver(driver)

    client.atomic_replace_file("/src/main.go", "m1", entities=[], relations=[])

    assert attempt_state["calls"] == 2, "expected one retry after TransientError"
    txs = attempt_state["txs"]
    txs[0].commit.assert_not_called()
    txs[0].rollback.assert_called_once()
    txs[1].commit.assert_called_once()


def test_atomic_replace_file_gives_up_after_max_retries():
    """Three TransientErrors in a row → re-raise after max_attempts=3."""
    from neo4j.exceptions import TransientError

    attempt_state = {"calls": 0, "txs": []}

    def session_factory():
        attempt_state["calls"] += 1
        tx = MagicMock()
        tx.run.side_effect = TransientError("memgraph conflict")
        attempt_state["txs"].append(tx)
        return _stub_session_with_tx(tx)

    driver = MagicMock()
    driver.session.side_effect = session_factory
    client = _make_client_with_fake_driver(driver)

    with pytest.raises(TransientError):
        client.atomic_replace_file("/src/main.go", "m1", entities=[], relations=[])

    assert attempt_state["calls"] == 3, "should attempt exactly max_attempts=3 times"
    for tx in attempt_state["txs"]:
        tx.commit.assert_not_called()
        tx.rollback.assert_called_once()


@pytest.mark.skipif(
    os.getenv("OMNIGRAPH_SKIP_MEMGRAPH_TESTS") == "1",
    reason="requires running Memgraph (set OMNIGRAPH_SKIP_MEMGRAPH_TESTS=1 to skip)",
)
def test_constraint_added_in_ensure_schema():
    """Open MemgraphCodeGraph, query SHOW CONSTRAINT INFO, verify the
    composite uniqueness constraint exists.

    Also verifies idempotency: re-opening the graph doesn't crash even
    though the constraint already exists.
    """
    try:
        graph = MemgraphCodeGraph()
    except Exception as exc:
        pytest.skip(f"Memgraph unavailable: {exc}")

    try:
        with graph._driver.session() as session:
            result = list(session.run("SHOW CONSTRAINT INFO"))
        # Memgraph returns rows like:
        #   constraint type | label | properties
        # Look for a unique constraint on Entity that covers all four keys.
        found = False
        for record in result:
            row = dict(record)
            ctype = (row.get("constraint type") or row.get("constraint_type") or "").lower()
            label = row.get("label") or ""
            props = row.get("properties") or []
            if isinstance(props, str):
                props = [props]
            if (
                "unique" in ctype
                and label == "Entity"
                and set(props) == {"machine_id", "project", "file_path", "name"}
            ):
                found = True
                break
        assert found, f"composite uniqueness constraint not present: {result}"
    finally:
        graph.close()

    # Re-open: must not crash on the already-existing constraint.
    graph2 = MemgraphCodeGraph()
    graph2.close()


@pytest.mark.skipif(
    os.getenv("OMNIGRAPH_SKIP_MEMGRAPH_TESTS") == "1",
    reason="requires running Memgraph",
)
def test_atomic_replace_file_end_to_end_against_memgraph():
    """Exercise the real Memgraph: insert via atomic_replace_file, verify
    entities + relations exist, then atomically replace and verify the
    old set is gone."""
    try:
        graph = MemgraphCodeGraph()
    except Exception as exc:
        pytest.skip(f"Memgraph unavailable: {exc}")

    machine_id = f"machine-us014-{uuid.uuid4().hex}"
    project = "demo"
    file_path = "/src/atomic.go"

    try:
        graph.atomic_replace_file(
            file_path,
            machine_id,
            entities=[
                {
                    "machine_id": machine_id,
                    "project": project,
                    "file_path": file_path,
                    "name": "Alpha",
                    "type": "function",
                    "content_hash": "h1",
                    "start_line": 1,
                    "end_line": 5,
                }
            ],
            relations=[
                {
                    "machine_id": machine_id,
                    "project": project,
                    "file_path": file_path,
                    "type": "IMPORTS",
                    "source": "",
                    "target": "fmt",
                    "target_type": "module",
                    "line": 1,
                    "confidence": "syntax",
                }
            ],
        )

        with graph._driver.session() as session:
            row = session.run(
                "MATCH (e:Entity {machine_id: $m, file_path: $p}) "
                "RETURN e.name AS name",
                m=machine_id,
                p=file_path,
            ).single()
            assert row is not None and row["name"] == "Alpha"

        # Replace: new entity name should appear, old should be gone.
        graph.atomic_replace_file(
            file_path,
            machine_id,
            entities=[
                {
                    "machine_id": machine_id,
                    "project": project,
                    "file_path": file_path,
                    "name": "Beta",
                    "type": "function",
                    "content_hash": "h2",
                    "start_line": 1,
                    "end_line": 5,
                }
            ],
            relations=[],
        )

        with graph._driver.session() as session:
            names = [
                rec["name"]
                for rec in session.run(
                    "MATCH (e:Entity {machine_id: $m, file_path: $p}) RETURN e.name AS name",
                    m=machine_id,
                    p=file_path,
                )
            ]
            assert names == ["Beta"], f"expected only Beta after replace, got {names}"
    finally:
        graph.delete_file(file_path, machine_id)
        graph.delete_machine(machine_id)
        graph.close()


def test_handle_upserts_atomic_replace_per_file_grouping():
    """A batch with two distinct file_paths should yield two
    atomic_replace_file calls, each carrying only that file's entities and
    relations (no cross-file leakage)."""
    from api_server.main import _handle_upserts
    from models.event import Entity, FileEvent

    class _FakeMemgraph:
        def __init__(self):
            self.replaced: list[tuple[str, str, list[dict], list[dict]]] = []
            self.legacy_relations: list[dict] = []

        def atomic_replace_file(self, file_path, machine_id, entities, relations):
            self.replaced.append((file_path, machine_id, list(entities), list(relations)))

        def upsert_relations(self, relations):
            self.legacy_relations.extend(relations)

        def delete_file(self, file_path, machine_id):  # not expected on this path
            raise AssertionError("delete_file should not be called when content is present")

        def upsert_entities(self, entities):  # not expected on this path
            raise AssertionError("upsert_entities should not be called when content is present")

    class _FakeContent:
        def __init__(self):
            self.files: list = []

        def upsert_files(self, files, content_hashes=None):
            self.files = list(files)

        def refresh_project_tree(self, machine_id, project):
            pass

        def update_content_hash(self, *args, **kwargs):
            pass

    class _FakeQdrant:
        def __init__(self):
            self.deleted: list = []

        def delete_by_file(self, file_path, machine_id):
            self.deleted.append((file_path, machine_id))

    class _FakeHTTP:
        async def post(self, *a, **kw):
            raise RuntimeError("skip embeddings for this test")

    ev_a = FileEvent(
        type="CREATE",
        path="/src/a.go",
        project="demo",
        machine_id="m1",
        timestamp=1,
        content="package a\nfunc Alpha(){}\n",
        content_hash="ha",
        entities=[Entity(name="Alpha", type="function", line=2, start_line=2, end_line=2)],
        relations=[
            {"type": "CONTAINS", "target": "Alpha", "target_type": "function", "line": 2, "confidence": "syntax"}
        ],
    )
    ev_b = FileEvent(
        type="CREATE",
        path="/src/b.go",
        project="demo",
        machine_id="m1",
        timestamp=2,
        content="package b\nfunc Beta(){}\n",
        content_hash="hb",
        entities=[Entity(name="Beta", type="function", line=2, start_line=2, end_line=2)],
        relations=[
            {"type": "IMPORTS", "target": "fmt", "target_type": "module", "line": 1, "confidence": "syntax"}
        ],
    )

    memgraph = _FakeMemgraph()
    content = _FakeContent()
    qdrant = _FakeQdrant()

    # _FakeHTTP.post raises, so embedding raises HTTPException 502 — that's
    # past the graph upsert path, so we just catch and ignore for this test.
    from fastapi import HTTPException

    try:
        asyncio.run(
            _handle_upserts([ev_a, ev_b], "m1", "demo", qdrant, memgraph, content, _FakeHTTP())
        )
    except HTTPException:
        pass

    assert len(memgraph.replaced) == 2, memgraph.replaced
    by_path = {call[0]: call for call in memgraph.replaced}
    assert set(by_path) == {"/src/a.go", "/src/b.go"}

    a_entities = by_path["/src/a.go"][2]
    a_relations = by_path["/src/a.go"][3]
    assert [e["name"] for e in a_entities] == ["Alpha"]
    assert all(e["file_path"] == "/src/a.go" for e in a_entities)
    assert all(r["file_path"] == "/src/a.go" for r in a_relations)
    assert {r["type"] for r in a_relations} == {"CONTAINS"}

    b_entities = by_path["/src/b.go"][2]
    b_relations = by_path["/src/b.go"][3]
    assert [e["name"] for e in b_entities] == ["Beta"]
    assert all(e["file_path"] == "/src/b.go" for e in b_entities)
    assert all(r["file_path"] == "/src/b.go" for r in b_relations)
    assert {r["type"] for r in b_relations} == {"IMPORTS"}

    # qdrant deletion still runs per-file after the graph commits.
    assert sorted(qdrant.deleted) == [("/src/a.go", "m1"), ("/src/b.go", "m1")]
    # No semantic-only relations in this batch.
    assert memgraph.legacy_relations == []
