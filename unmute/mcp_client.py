import os
import httpx
from typing import Any
from logging import getLogger

logger = getLogger(__name__)


class MCPHTTPClient:
    """HTTP client for communicating with the MCP sidecar service."""

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or os.environ.get(
            "MCP_HTTP_URL", "http://127.0.0.1:3001"
        )
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_tools(self) -> list[dict[str, Any]]:
        """Fetch all available tools from MCP servers."""
        client = await self._get_client()
        response = await client.get("/v1/mcp/tools")
        response.raise_for_status()
        return response.json()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on the MCP server."""
        client = await self._get_client()
        response = await client.post(
            "/v1/mcp/call", json={"tool_name": tool_name, "arguments": arguments}
        )
        response.raise_for_status()
        return response.json()

    async def get_status(self) -> dict[str, Any]:
        """Get status of all MCP servers."""
        client = await self._get_client()
        response = await client.get("/v1/mcp/status")
        response.raise_for_status()
        return response.json()

    async def health_check(self) -> bool:
        """Check if MCP service is healthy."""
        try:
            client = await self._get_client()
            response = await client.get("/health")
            return response.status_code == 200
        except Exception:
            return False
