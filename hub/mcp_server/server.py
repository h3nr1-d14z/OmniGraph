"""OmniGraph MCP Server — 4-tool local bridge to Claude Code."""

import json
import os
import sys

from fastmcp import FastMCP
from models.schema import (
    DependencyGraphInput,
    ProjectTreeInput,
    ReadFileInput,
    SemanticSearchInput,
)
from tools.dependency_graph import get_dependency_graph
from tools.project_tree import get_project_tree
from tools.read_file import read_full_file
from tools.semantic_search import semantic_search_code

mcp = FastMCP("omnigraph")


@mcp.tool()
def semantic_search_code_tool(query: str, project_scope: str | None = None, machine_id: str | None = None) -> str:
    """Find code snippets/files by semantic meaning.

    Args:
        query: Natural language description (e.g. "password hashing function").
        project_scope: Optional project name to filter results.
        machine_id: Optional machine identifier to filter results.

    Returns JSON array of results. Each result includes a ``matched_entities``
    field listing all symbol names (e.g. function/class names) that contributed
    to the RRF score for that file. Use this to decide whether to call
    ``read_full_file`` for additional context.
    """
    inp = SemanticSearchInput(query=query, project_scope=project_scope, machine_id=machine_id)
    results = semantic_search_code(inp)
    return json.dumps([r.model_dump() for r in results], indent=2, ensure_ascii=False)


@mcp.tool()
def get_dependency_graph_tool(entity_name: str, direction: str, machine_id: str | None = None) -> str:
    """Analyze call flow and dependencies.

    Args:
        entity_name: Name of the function, class, or component.
        direction: upstream, downstream, or both.
        machine_id: Optional machine identifier; None searches all machines.
    """
    inp = DependencyGraphInput(entity_name=entity_name, direction=direction, machine_id=machine_id)
    result = get_dependency_graph(inp)
    return json.dumps(result.model_dump(), indent=2, ensure_ascii=False)


@mcp.tool()
def read_full_file_tool(machine_id: str, file_path: str) -> str:
    """Read entire file content into context.

    Args:
        machine_id: Identifier of the machine where the file resides.
        file_path: Absolute or relative path to the file.
    """
    inp = ReadFileInput(machine_id=machine_id, file_path=file_path)
    return read_full_file(inp)


@mcp.tool()
def get_project_tree_tool(machine_id: str, project_name: str) -> str:
    """Get directory tree overview.

    Args:
        machine_id: Identifier of the machine.
        project_name: Name of the project to list.
    """
    inp = ProjectTreeInput(machine_id=machine_id, project_name=project_name)
    return get_project_tree(inp)


if __name__ == "__main__":
    # stdio is the default transport for local MCP servers
    mcp.run()
