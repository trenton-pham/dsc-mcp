from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import catalog


def make_server(db: Path) -> MCPServer:
    server = MCPServer("dsc-mcp", instructions=(
        "Search the local curated documentation snapshot before making library claims. "
        "Use read_doc for full evidence and cite its official URL. "
        "If results do not support a claim, say the catalog has insufficient evidence."
    ))
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=read_only)
    def list_libraries() -> list[dict]:
        """List supported libraries, curated coverage, local availability, and source versions."""
        try:
            return catalog.list_libraries(db)
        except catalog.CatalogError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=read_only)
    def search_docs(query: str, library: str | None = None,
                    limit: Annotated[int, Field(strict=True, ge=1, le=10)] = 5) -> list[dict]:
        """Search literal terms in local official docs; library filters to one catalog name."""
        try:
            return catalog.search_docs(query, library, limit, db)
        except (catalog.CatalogError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=read_only)
    def read_doc(doc_id: str) -> dict:
        """Read a full section and its exact official source URL and download metadata."""
        try:
            return catalog.read_doc(doc_id, db)
        except catalog.CatalogError as exc:
            raise ToolError(str(exc)) from exc

    return server
