"""MCP forward-compat contract for SemanticSearchResult.

Rules:
- V1 required fields must remain required.
- Existing field types must not change.
- New fields (added in C.1+) must be Optional (no new required).
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hub" / "mcp_server"))

from models.schema import SemanticSearchResult  # noqa: E402

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
SNAPSHOT_FILE = SNAPSHOT_DIR / "semantic_search_result_v1.json"

# V1 required fields (locked at Phase D commit time).
V1_REQUIRED = {"file_path", "machine_id", "project", "snippet", "score"}


def _live_schema():
    return SemanticSearchResult.model_json_schema()


def test_snapshot_exists():
    """First-run: write snapshot. Subsequent runs: must exist."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    if not SNAPSHOT_FILE.exists():
        SNAPSHOT_FILE.write_text(json.dumps(_live_schema(), indent=2, sort_keys=True) + "\n")
        pytest.skip("Snapshot created; re-run to validate.")
    assert SNAPSHOT_FILE.exists()


def test_v1_required_fields_unchanged():
    """V1 required fields must remain required."""
    live = _live_schema()
    live_required = set(live.get("required", []))
    missing = V1_REQUIRED - live_required
    assert not missing, f"MCP contract broken — required fields removed/optional: {missing}"


def test_v1_field_types_unchanged():
    """V1 field types must not change."""
    snapshot = json.loads(SNAPSHOT_FILE.read_text())
    live = _live_schema()
    snap_props = snapshot.get("properties", {})
    live_props = live.get("properties", {})
    for field in V1_REQUIRED:
        assert field in live_props, f"Field '{field}' missing"
        # Compare 'type' key only — ignore 'title' / 'description' churn
        snap_type = snap_props[field].get("type") if field in snap_props else None
        live_type = live_props[field].get("type")
        assert snap_type == live_type, f"Field '{field}' type changed: was {snap_type}, now {live_type}"


def test_new_fields_are_optional():
    """Any field added after v1 must NOT be required."""
    snapshot = json.loads(SNAPSHOT_FILE.read_text())
    live = _live_schema()
    v1_props = set(snapshot.get("properties", {}).keys())
    live_props = set(live.get("properties", {}).keys())
    new_fields = live_props - v1_props
    live_required = set(live.get("required", []))
    required_new = new_fields & live_required
    assert not required_new, f"New fields added as required (breaks old MCP clients): {required_new}"
