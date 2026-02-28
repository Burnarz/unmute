"""MCP (Model Context Protocol) Manager for connecting to MCP servers.

This module provides a manager for connecting to the MCP sidecar service,
discovering their tools, and invoking them on behalf of the LLM.

Configuration is done via environment variable:
- MCP_HTTP_URL: URL of the MCP sidecar service (default: http://127.0.0.1:3001)
"""

from logging import getLogger
from os import path
from typing import Any

import json

from pydantic import BaseModel

from unmute.mcp_client import MCPHTTPClient

logger = getLogger(__name__)


class MCPTool(BaseModel):
    """Represents a tool from an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str


class MCPManager:
    """Manager for MCP server connections via HTTP.

    This singleton manages connections to the MCP sidecar service
    and provides a unified interface for tool discovery and invocation.
    """

    _instance: "MCPManager | None" = None
    _lock = None

    def __init__(self) -> None:
        self._http_client = MCPHTTPClient()
        self.tools: list[MCPTool] = []
        self._initialized = False
        self._memory_cache: str | None = None
        self._excluded_tools: set[str] = self._load_excluded_tools()

    def _load_excluded_tools(self) -> set[str]:
        """Load excluded tools from config file."""
        config_path = path.join(
            path.dirname(__file__), "..", "mcp", "mcp-excluded-tools.json"
        )
        try:
            with open(config_path) as f:
                config = json.load(f)
            excluded = config.get("excluded_tools", [])
            logger.info("Loaded excluded MCP tools: %s", excluded)
            return set(excluded)
        except FileNotFoundError:
            logger.debug("No MCP tools config found at %s", config_path)
            return set()
        except Exception as e:
            logger.warning("Failed to load MCP tools config: %s", e)
            return set()

    @classmethod
    async def get_instance(cls) -> "MCPManager":
        """Get the singleton instance, initializing if needed."""
        if cls._instance is None:
            cls._instance = MCPManager()
        if not cls._instance._initialized:
            await cls._instance.initialize()
            cls._instance._initialized = True
        return cls._instance

    async def initialize(self) -> None:
        """Initialize connections to MCP servers."""
        try:
            await self._http_client.health_check()
        except Exception as e:
            logger.warning(f"MCP service not available: {e}")
            return

        try:
            tools_data = await self._http_client.get_tools()
            all_tools = [
                MCPTool(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                    server_name=tool.get("server_name", "unknown"),
                )
                for tool in tools_data
            ]
            self.tools = [t for t in all_tools if t.name not in self._excluded_tools]
            logger.info(
                "MCP manager initialized with %d tools (%d excluded)",
                len(self.tools),
                len(self._excluded_tools),
            )
        except Exception as e:
            logger.error(f"Failed to initialize MCP: {e}")

    async def shutdown(self) -> None:
        """Close the HTTP client."""
        await self._http_client.close()
        self.tools = []
        self._initialized = False
        self._memory_cache = None

    async def load_memory(self) -> str | None:
        """Load memory from MCP memory server and cache it."""
        try:
            result = await self._http_client.call_tool("read_graph", {})
            self._memory_cache = result
            logger.info("Memory loaded and cached")
            return result
        except Exception as e:
            logger.warning(f"Failed to load memory: {e}")
            return self._memory_cache

    def get_cached_memory(self) -> str | None:
        """Get cached memory without calling the server."""
        return self._memory_cache

    def get_all_tools(self) -> list[MCPTool]:
        """Get all tools from all connected MCP servers."""
        return self.tools

    def get_tools_for_llm(self) -> list[dict[str, Any]]:
        """Get tools in OpenAI function calling format."""
        tools = []
        for mcp_tool in self.tools:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": mcp_tool.name,
                        "description": mcp_tool.description,
                        "parameters": mcp_tool.input_schema,
                    },
                }
            )
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool by name."""
        logger.info(
            "Calling MCP tool '%s' with args: %s",
            tool_name,
            arguments,
        )
        return await self._http_client.call_tool(tool_name, arguments)

    def is_tool_from_mcp(self, tool_name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        return any(tool.name == tool_name for tool in self.tools)


async def get_mcp_manager() -> MCPManager:
    """Get the initialized MCP manager singleton."""
    return await MCPManager.get_instance()
