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
    return DependencyGraphResult(
        nodes=data.get("nodes", []),
        edges=data.get("edges", []),
    )
