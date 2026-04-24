"""Authentication helpers for MCP Server and Hub endpoints."""

import os
from functools import wraps


def get_allowed_machines() -> dict[str, str]:
    """Return dict of machine_id -> auth_token from env."""
    # Format: MACHINE_TOKENS="id1:token1,id2:token2"
    raw = os.getenv("MACHINE_TOKENS", "")
    machines = {}
    for pair in raw.split(","):
        if ":" in pair:
            mid, tok = pair.split(":", 1)
            machines[mid.strip()] = tok.strip()
    return machines


def verify_token(machine_id: str, token: str) -> bool:
    allowed = get_allowed_machines()
    return allowed.get(machine_id) == token
