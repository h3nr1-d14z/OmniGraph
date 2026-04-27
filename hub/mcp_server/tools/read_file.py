"""Tool 3: read_full_file — Read entire file content into context."""

from models.schema import ReadFileInput


def read_full_file(input_data: ReadFileInput) -> str:
    """Read entire file content."""
    # 1. Try content store first (Hub synced content)
    from db.content_store import get_store

    store = get_store()
    content = store.get_file(input_data.machine_id, input_data.file_path)
    if content is not None:
        return content

    # 2. Fallback: read local filesystem (MCP Server runs locally on dev machine)
    import os

    if os.path.exists(input_data.file_path):
        try:
            with open(input_data.file_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as exc:
            return f"[error reading local file: {exc}]"

    return f"[file not found: {input_data.file_path}]"
