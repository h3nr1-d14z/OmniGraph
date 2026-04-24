"""Tool 4: get_project_tree — Return directory tree overview."""

import os

from models.schema import ProjectTreeInput

MAX_TREE_DEPTH = int(os.getenv("PROJECT_TREE_MAX_DEPTH", "4"))
MAX_TREE_ENTRIES = int(os.getenv("PROJECT_TREE_MAX_ENTRIES", "200"))
MAX_TREE_LINES = int(os.getenv("PROJECT_TREE_MAX_LINES", "300"))


def get_project_tree(input_data: ProjectTreeInput) -> str:
    """Get directory tree overview."""
    from db.content_store import get_store

    store = get_store()
    cached = store.get_project_tree(input_data.machine_id, input_data.project_name)
    if cached is not None:
        return _cap_tree_text(cached)

    roots = [".", "~/Projects", "~/projects", "~/dev"]
    for r in roots:
        path = os.path.expanduser(r)
        if not os.path.isdir(path):
            continue
        for entry in os.listdir(path):
            if entry == input_data.project_name:
                full = os.path.join(path, entry)
                return _build_tree(full)

    return f"[project not found: {input_data.project_name}]"


def _cap_tree_text(tree_text: str) -> str:
    lines = tree_text.splitlines()
    if len(lines) <= MAX_TREE_LINES:
        return tree_text
    truncated = lines[:MAX_TREE_LINES]
    truncated.append("[truncated]")
    return "\n".join(truncated)


def _build_tree(root: str) -> str:
    lines = [os.path.basename(root) + "/"]
    state = {"entries": 0, "truncated": False}
    _walk_tree(root, lines, "", 0, state)
    if state["truncated"]:
        lines.append("[truncated]")
    return _cap_tree_text("\n".join(lines))


def _walk_tree(root: str, lines: list[str], prefix: str, depth: int, state: dict[str, int | bool]) -> None:
    if state["truncated"]:
        return
    if depth >= MAX_TREE_DEPTH:
        lines.append(prefix + "└── ...")
        state["truncated"] = True
        return

    try:
        entries = sorted(os.listdir(root))
    except PermissionError:
        lines.append(prefix + os.path.basename(root) + "/ [permission denied]")
        return

    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "target", "dist", "build"}
    entries = [e for e in entries if e not in skip]

    for i, entry in enumerate(entries):
        if state["truncated"]:
            return
        if state["entries"] >= MAX_TREE_ENTRIES or len(lines) >= MAX_TREE_LINES:
            state["truncated"] = True
            return

        path = os.path.join(root, entry)
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        state["entries"] += 1
        if os.path.isdir(path):
            lines.append(prefix + connector + entry + "/")
            extension = "    " if is_last else "│   "
            _walk_tree(path, lines, prefix + extension, depth + 1, state)
        else:
            lines.append(prefix + connector + entry)
