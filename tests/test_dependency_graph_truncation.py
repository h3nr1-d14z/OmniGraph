"""Truncation contract for hub.mcp_server.tools.dependency_graph.

Codex review (2026-04): when MAX_NODES caps node list, edges that reference
the dropped nodes must NOT survive — otherwise the graph response has
dangling endpoints.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "mcp_server"))


def _reload_dg(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]):
    """Set env via monkeypatch (auto-reverted at test teardown) then reload
    tools.dependency_graph so its module-level MAX_NODES / MAX_EDGES
    constants pick up the new values for this test only."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import importlib

    import tools.dependency_graph as dg

    importlib.reload(dg)
    return dg


def _make_node(idx: int) -> dict:
    return {"id": f"n{idx}", "name": f"sym{idx}", "kind": "Entity"}


def _make_edge(src_idx: int, dst_idx: int) -> dict:
    return {
        "from": f"n{src_idx}",
        "to": f"n{dst_idx}",
        "relation": "CALLS",
        "line": None,
        "confidence": 1.0,
    }


def test_edges_referencing_dropped_nodes_are_filtered_out(monkeypatch):
    dg = _reload_dg(monkeypatch, {"DEP_GRAPH_MAX_NODES": "3", "DEP_GRAPH_MAX_EDGES": "100"})

    nodes = [_make_node(i) for i in range(10)]  # n0..n9
    edges = [
        _make_edge(0, 1),  # both retained (n0,n1 in first 3)
        _make_edge(1, 2),  # both retained
        _make_edge(0, 5),  # n5 dropped → must be filtered
        _make_edge(7, 8),  # both dropped → must be filtered
        _make_edge(2, 0),  # both retained
    ]

    class FakeGraph:
        def get_dependencies(self, **_kw):
            return {"nodes": nodes, "edges": edges}

    with patch("db.memgraph_client.get_memgraph", return_value=FakeGraph()):
        from models.schema import DependencyGraphInput

        out = dg.get_dependency_graph(DependencyGraphInput(entity_name="x", direction="downstream"))

    retained_ids = {n["id"] for n in out.nodes}
    assert retained_ids == {"n0", "n1", "n2"}, f"node truncation off: {retained_ids}"

    for edge in out.edges:
        assert edge["from"] in retained_ids, f"dangling from-endpoint: {edge}"
        assert edge["to"] in retained_ids, f"dangling to-endpoint: {edge}"

    # 3 in-scope edges survive, 2 dangling dropped.
    assert len(out.edges) == 3
    # Summary still reports the original full sizes for observability.
    assert out.summary["full_node_count"] == 10
    assert out.summary["full_edge_count"] == 5
    assert out.summary["truncated"] is True


def test_no_truncation_preserves_legacy_behavior(monkeypatch):
    dg = _reload_dg(monkeypatch, {"DEP_GRAPH_MAX_NODES": "100", "DEP_GRAPH_MAX_EDGES": "100"})

    nodes = [_make_node(i) for i in range(3)]
    edges = [_make_edge(0, 1), _make_edge(1, 2)]

    class FakeGraph:
        def get_dependencies(self, **_kw):
            return {"nodes": nodes, "edges": edges}

    with patch("db.memgraph_client.get_memgraph", return_value=FakeGraph()):
        from models.schema import DependencyGraphInput

        out = dg.get_dependency_graph(DependencyGraphInput(entity_name="x", direction="downstream"))

    assert len(out.nodes) == 3
    assert len(out.edges) == 2
    assert out.summary["truncated"] is False
    assert out.summary["full_node_count"] == 3
    assert out.summary["full_edge_count"] == 2


def test_edge_cap_applies_after_node_filter(monkeypatch):
    """Even when all edges are in-scope, MAX_EDGES still caps the list."""
    dg = _reload_dg(monkeypatch, {"DEP_GRAPH_MAX_NODES": "50", "DEP_GRAPH_MAX_EDGES": "3"})

    nodes = [_make_node(i) for i in range(5)]
    edges = [_make_edge(0, 1), _make_edge(1, 2), _make_edge(2, 3), _make_edge(3, 4), _make_edge(4, 0)]

    class FakeGraph:
        def get_dependencies(self, **_kw):
            return {"nodes": nodes, "edges": edges}

    with patch("db.memgraph_client.get_memgraph", return_value=FakeGraph()):
        from models.schema import DependencyGraphInput

        out = dg.get_dependency_graph(DependencyGraphInput(entity_name="x", direction="downstream"))

    assert len(out.edges) == 3  # capped
    assert out.summary["truncated"] is True
    assert out.summary["full_edge_count"] == 5
