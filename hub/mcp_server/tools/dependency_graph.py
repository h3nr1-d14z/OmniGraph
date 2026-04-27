"""Tool 2: get_dependency_graph — Query Memgraph for call flow."""

import logging
import os

from models.schema import DependencyGraphInput, DependencyGraphResult

MAX_NODES = int(os.getenv("DEP_GRAPH_MAX_NODES", "50"))
MAX_EDGES = int(os.getenv("DEP_GRAPH_MAX_EDGES", "100"))

_log = logging.getLogger(__name__)


def get_dependency_graph(input_data: DependencyGraphInput) -> DependencyGraphResult:
    """Analyze call flow and dependencies."""
    from db.memgraph_client import get_memgraph

    graph = get_memgraph()
    data = graph.get_dependencies(
        entity_name=input_data.entity_name,
        direction=input_data.direction,
        machine_id=input_data.machine_id,
    )
    nodes_full = data.get("nodes", [])
    edges_full = data.get("edges", [])

    nodes_truncated = len(nodes_full) > MAX_NODES
    nodes = nodes_full[:MAX_NODES]

    # Edges must reference nodes the caller can see. After capping nodes we
    # drop edges whose endpoints fell out of the retained set so renderers
    # never get dangling edge endpoints; THEN apply the edge cap.
    retained_keys = _node_key_set(nodes)
    edges_in_scope = [e for e in edges_full if _edge_in_scope(e, retained_keys)]
    # Only the post-filter check signals "the user lost edges to the cap".
    # Dangling edges dropped by node truncation are surfaced via
    # nodes_truncated; conflating them produced a misleading flag.
    edges_truncated = len(edges_in_scope) > MAX_EDGES
    edges = edges_in_scope[:MAX_EDGES]
    if nodes_truncated or edges_truncated:
        _log.warning(
            "dep_graph_truncated entity=%s nodes=%d/%d edges=%d/%d",
            input_data.entity_name, len(nodes), len(nodes_full), len(edges), len(edges_full),
        )

    groups = _group_edges(edges)
    return DependencyGraphResult(
        nodes=nodes,
        edges=edges,
        summary=_build_summary(
            input_data.entity_name, input_data.direction, nodes, edges, groups,
            full_node_count=len(nodes_full), full_edge_count=len(edges_full),
            truncated=nodes_truncated or edges_truncated,
        ),
        groups=groups,
    )


def _node_key_set(nodes: list[dict]) -> set[str]:
    """Collect the node IDs that survived truncation. Empty/missing IDs are
    skipped — Memgraph always assigns one via _node_payload."""
    return {n["id"] for n in nodes if n.get("id")}


def _edge_in_scope(edge: dict, retained: set[str]) -> bool:
    """An edge is in scope only if BOTH endpoints survived node truncation."""
    return edge.get("from") in retained and edge.get("to") in retained


def _build_summary(
    entity_name: str,
    direction: str,
    nodes: list[dict],
    edges: list[dict],
    groups: dict[str, list[dict]],
    full_node_count: int,
    full_edge_count: int,
    truncated: bool,
) -> dict:
    return {
        "entity": entity_name,
        "direction": direction,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "full_node_count": full_node_count,
        "full_edge_count": full_edge_count,
        "truncated": truncated,
        "imports": len(groups["imports"]),
        "imports_resolved": len(groups["imports_resolved"]),
        "calls": len(groups["calls"]),
        "calls_syntax": len(groups["calls_syntax"]),
        "calls_resolved": len(groups["calls_resolved"]),
        "references": len(groups["references"]),
        "contains": len(groups["contains"]),
        "dependencies": len(groups["dependencies"]),
    }


def _group_edges(edges: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {
        "imports": [],
        "imports_resolved": [],
        "calls": [],
        "calls_syntax": [],
        "calls_resolved": [],
        "references": [],
        "contains": [],
        "dependencies": [],
        "other": [],
    }
    for edge in edges:
        relation = edge.get("relation")
        if relation == "IMPORTS":
            groups["imports"].append(edge)
        elif relation == "IMPORTS_RESOLVED":
            groups["imports"].append(edge)
            groups["imports_resolved"].append(edge)
        elif relation == "CALLS_SYNTAX":
            groups["calls"].append(edge)
            groups["calls_syntax"].append(edge)
        elif relation in ("CALLS", "CALLS_RESOLVED"):
            groups["calls"].append(edge)
            if relation == "CALLS_RESOLVED":
                groups["calls_resolved"].append(edge)
        elif relation == "REFERENCES":
            groups["references"].append(edge)
        elif relation == "CONTAINS":
            groups["contains"].append(edge)
        elif relation == "DEPENDS_ON":
            groups["dependencies"].append(edge)
        else:
            groups["other"].append(edge)
    return groups
