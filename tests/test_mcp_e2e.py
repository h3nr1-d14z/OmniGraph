"""End-to-end test for Phase 3: MCP Server (4 tools)."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "hub" / "mcp_server"))

from models.schema import (
    DependencyGraphInput,
    ProjectTreeInput,
    ReadFileInput,
    SemanticSearchInput,
)
from tools.read_file import read_full_file
from tools.project_tree import get_project_tree


def test_read_full_file_local():
    print("[test] read_full_file local fallback ...")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".go", delete=False) as f:
        f.write("package main\nfunc main() {}\n")
        path = f.name

    try:
        inp = ReadFileInput(machine_id="local", file_path=path)
        content = read_full_file(inp)
        assert "package main" in content, content
        print("[test] read_full_file OK")
    finally:
        os.unlink(path)


def _assert_project_tree_local(base_dir: Path) -> None:
    print("[test] get_project_tree local fallback ...")
    proj_dir = base_dir / "testproj"
    src_dir = proj_dir / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "main.go").write_text("package main\n")

    inp = ProjectTreeInput(machine_id="local", project_name="testproj")
    tree = get_project_tree(inp)
    assert "src/" in tree, tree
    assert "main.go" in tree, tree
    print("[test] get_project_tree OK")


def test_get_project_tree_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _assert_project_tree_local(tmp_path)


def test_project_tree_cap():
    print("[test] project tree cap ...")
    from tools.project_tree import _cap_tree_text

    tree = "\n".join(f"line-{i}" for i in range(400))
    capped = _cap_tree_text(tree)
    assert "[truncated]" in capped
    assert len(capped.splitlines()) <= 301
    print("[test] project tree cap OK")


def test_tool_schemas():
    print("[test] tool schema validation ...")
    # Verify inputs construct correctly
    s = SemanticSearchInput(query="hash password", project_scope="OmniGraph", machine_id="m1")
    assert s.query == "hash password"
    assert s.project_scope == "OmniGraph"
    assert s.machine_id == "m1"

    d = DependencyGraphInput(entity_name="main", direction="downstream")
    assert d.direction in ("upstream", "downstream", "both")

    r = ReadFileInput(machine_id="m1", file_path="/src/main.go")
    assert r.machine_id == "m1"

    p = ProjectTreeInput(machine_id="m1", project_name="OmniGraph")
    assert p.project_name == "OmniGraph"

    print("[test] schemas OK")


async def _stdio_transport_smoke(content_db_path: Path):
    env = os.environ.copy()
    env["CONTENT_DB_PATH"] = str(content_db_path)
    transport = PythonStdioTransport(
        PROJECT_ROOT / "hub" / "mcp_server" / "server.py",
        env=env,
        cwd=str(PROJECT_ROOT / "hub" / "mcp_server"),
        python_cmd=sys.executable,
    )

    async with Client(transport, init_timeout=15) as client:
        await client.ping()
        tools = await client.list_tools()
        tool_names = {tool.name for tool in tools}
        assert tool_names == {
            "semantic_search_code_tool",
            "get_dependency_graph_tool",
            "read_full_file_tool",
            "get_project_tree_tool",
        }

        result = await client.call_tool(
            "read_full_file_tool",
            {"machine_id": "local", "file_path": str(PROJECT_ROOT / "README.md")},
        )
        assert "# OmniGraph" in result.data

        graph_result = await client.call_tool(
            "get_dependency_graph_tool",
            {"entity_name": "missing", "direction": "downstream", "machine_id": "local"},
        )
        graph = json.loads(graph_result.data)
        assert graph["summary"]["entity"] == "missing"
        assert graph["summary"]["edge_count"] == 0
        assert set(graph["groups"]) == {"imports", "calls", "contains", "dependencies", "other"}
        assert graph["nodes"] == []
        assert graph["edges"] == []


def test_mcp_server_stdio_transport(tmp_path):
    asyncio.run(_stdio_transport_smoke(tmp_path / "mcp-content.db"))


def main():
    print("=== OmniGraph Phase 3 E2E Test ===\n")
    test_tool_schemas()
    test_read_full_file_local()
    with tempfile.TemporaryDirectory() as tmp_dir:
        previous_cwd = os.getcwd()
        try:
            os.chdir(tmp_dir)
            _assert_project_tree_local(Path(tmp_dir))
            asyncio.run(_stdio_transport_smoke(Path(tmp_dir) / "mcp-content.db"))
        finally:
            os.chdir(previous_cwd)
    print("\n=== All Phase 3 tests passed ===")


if __name__ == "__main__":
    main()
