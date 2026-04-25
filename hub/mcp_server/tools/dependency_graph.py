"""Tool 2: get_dependency_graph — Query Memgraph for call flow."""

from models.schema import DependencyGraphInput, DependencyGraphResult


def get_dependency_graph(input_data: DependencyGraphInput) -> DependencyGraphResult:
    """Analyze call flow and dependencies."""
    from db.memgraph_client import get_memgraph

    graph = get_memgraph()
    data = graph.get_dependencies(
        entity_name=input_data.entity_name,
        direction=input_data.direction,
        machine_id=input_data.machine_id,
    )
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    groups = _group_edges(edges)
    return DependencyGraphResult(
        nodes=nodes,
        edges=edges,
        summary=_build_summary(input_data.entity_name, input_data.direction, nodes, edges, groups),
        groups=groups,
    )


def _build_summary(entity_name: str, direction: str, nodes: list[dict], edges: list[dict], groups: dict[str, list[dict]]) -> dict:
    return {
        "entity": entity_name,
        "direction": direction,
        "node_count": len(nodes),
        "edge_count": len(edges),
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
