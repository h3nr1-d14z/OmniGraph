"""End-to-end test for Phase 3: MCP Server (4 tools)."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub", "mcp_server"))

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


def test_get_project_tree_local():
    print("[test] get_project_tree local fallback ...")
    proj_dir = os.path.join(os.getcwd(), "testproj")
    src_dir = os.path.join(proj_dir, "src")
    os.makedirs(src_dir, exist_ok=True)
    with open(os.path.join(src_dir, "main.go"), "w") as f:
        f.write("package main\n")
    try:
        inp = ProjectTreeInput(machine_id="local", project_name="testproj")
        tree = get_project_tree(inp)
        assert "src/" in tree, tree
        assert "main.go" in tree, tree
        print("[test] get_project_tree OK")
    finally:
        import shutil
        shutil.rmtree(proj_dir, ignore_errors=True)


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


def main():
    print("=== OmniGraph Phase 3 E2E Test ===\n")
    test_tool_schemas()
    test_read_full_file_local()
    test_get_project_tree_local()
    print("\n=== All Phase 3 tests passed ===")


if __name__ == "__main__":
    main()
