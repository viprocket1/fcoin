"""
MCP Server — protocol-compliant, transport-agnostic.
Exposes the agent's tools, resources, and prompts via the MCP spec
(JSON-RPC 2.0 over stdio or SSE).

Import this and call:
    server = MCPServer(agent=my_session)
    server.run(transport="stdio")   # or "sse"
"""
from __future__ import annotations

import json, logging, sys
from dataclasses import dataclass, field
from typing import Any, Callable
from mcp.server import Server as MCPServerCore
from mcp.server.stdio import stdio_server
from mcp.types import Tool, Resource, Prompt
from mcp.server import Server

from .agent import Session, ToolDef, ResourceDef

log = logging.getLogger("fcoin.mcp")


# ---------------------------------------------------------------------------
# MCP Server wrapper
# ---------------------------------------------------------------------------

@dataclass
class MCPServer:
    """
    Wires a Session (agent core) into the MCP protocol.
    Works with any MCP-compatible client over stdio or SSE.
    """
    session: Session
    name: str = "fcoin-agent"
    version: str = "0.1.0"

    _server: Server = field(init=False)

    def __post_init__(self):
        self._server = MCPServerCore(name=self.name)
        self._register_handlers()

    # ------------------------------------------------------------------
    # Internal MCP handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        s = self._server

        # -- tools/list --------------------------------------------------------
        @s.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name=t.name,
                    description=t.description,
                    inputSchema=t.input_schema,
                )
                for t in self.session.tool_registry.values()
            ]

        # -- tools/call -------------------------------------------------------
        @s.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
            tool = self.session.tool_registry.get(name)
            if not tool:
                raise ValueError(f"Unknown tool: {name}")
            return tool.handler(**arguments)

        # -- resources/list ---------------------------------------------------
        @s.list_resources()
        async def list_resources() -> list[Resource]:
            return [
                Resource(
                    uri=res.uri,
                    name=res.name,
                    description=res.description,
                    mimeType=res.mime_type,
                )
                for res in self.session.resource_registry.values()
            ]

        # -- resources/{uri} --------------------------------------------------
        @s.read_resource()
        async def read_resource(uri: Any) -> str:
            uri_str = str(uri)
            res = self.session.resource_registry.get(uri_str)
            if not res:
                raise ValueError(f"Unknown resource: {uri_str}")
            # Call handler if provided, otherwise return URI as text
            handler = getattr(res, "handler", None)
            if handler:
                return await handler()
            return f"Resource: {uri_str}"

        # -- prompts/list -----------------------------------------------------
        @s.list_prompts()
        async def list_prompts() -> list[Prompt]:
            return []  # Populate via session.prompt_registry if needed

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run_stdio(self) -> None:
        """Blocking stdio transport — the standard MCP launch mechanism."""
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream,
                write_stream,
                self._server.create_initialization_options(),
            )

    def run(self, transport: str = "stdio") -> None:
        """
        Entry point.  Set transport="sse" to use HTTP/SSE instead.
        SSE requires:  pip install fcoin-mcp-agent[sse]
        """
        import asyncio
        if transport == "stdio":
            asyncio.run(self.run_stdio())
        elif transport == "sse":
            try:
                from .transport.sse import run_sse
            except ImportError:
                log.error("SSE transport not available. Install with: pip install fcoin-mcp-agent[sse]")
                sys.exit(1)
            asyncio.run(run_sse(self))
        else:
            raise ValueError(f"Unknown transport: {transport!r}")
