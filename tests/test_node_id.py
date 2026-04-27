"""Unit tests for _node_id composite identity (US-001 acceptance)."""

import os
import sys

# Make hub/mcp_server importable for tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "mcp_server"))

from db.memgraph_client import _node_id  # noqa: E402


def test_two_entities_same_file_distinct_ids():
    """Phase 1.1 bug: two Entity nodes in same file MUST produce distinct ids.

    Previously _node_id returned just file_path, collapsing distinct entities
    in same file → graph tool returned self-loop edges and dropped nodes.
    """
    a = {"file_path": "/proj/foo.go", "name": "FuncA", "machine_id": "m1"}
    b = {"file_path": "/proj/foo.go", "name": "FuncB", "machine_id": "m1"}
    assert _node_id(a) != _node_id(b)
    assert _node_id(a) == "m1:/proj/foo.go#FuncA"
    assert _node_id(b) == "m1:/proj/foo.go#FuncB"


def test_two_entities_same_name_different_machine_distinct():
    """Cross-machine isolation: same path+name on different machines distinct."""
    a = {"file_path": "/p/foo.go", "name": "F", "machine_id": "m1"}
    b = {"file_path": "/p/foo.go", "name": "F", "machine_id": "m2"}
    assert _node_id(a) != _node_id(b)


def test_file_node_uses_path_only():
    """File nodes have path but no name → id is path (machine-prefixed)."""
    f = {"path": "/proj/foo.go", "machine_id": "m1"}
    assert _node_id(f) == "m1:/proj/foo.go"


def test_module_node_uses_name_only():
    """Module/Symbol nodes have name but no file_path."""
    m = {"name": "fmt", "machine_id": "m1"}
    assert _node_id(m) == "m1:fmt"


def test_no_machine_id_falls_back_to_local_components():
    """Robust to missing machine_id (e.g., legacy nodes)."""
    e = {"file_path": "/p/x.go", "name": "X"}
    assert _node_id(e) == "/p/x.go#X"


def test_empty_node_returns_empty_string():
    """Defensive: completely empty node → empty id."""
    assert _node_id({}) == ""
