"""Optional Model Context Protocol adapter for Entryplug."""

from entryplug_mcp.server import (
    CANCEL_TOOL,
    CATALOG_TOOL,
    INSPECT_TOOL,
    create_mcp_server,
)

__all__ = ["CANCEL_TOOL", "CATALOG_TOOL", "INSPECT_TOOL", "create_mcp_server"]
