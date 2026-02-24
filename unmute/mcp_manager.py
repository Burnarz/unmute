"""MCP (Model Context Protocol) Manager for connecting to MCP servers.

This module provides a manager for connecting to multiple MCP servers,
discovering their tools, and invoking them on behalf of the LLM.

Configuration is done via environment variables:
- MCP_GMAIL_COMMAND: Command to run Gmail MCP server (e.g., "npx")
- MCP_GMAIL_ARGS: JSON array of arguments for Gmail MCP server
- MCP_CALENDAR_COMMAND: Command to run Calendar MCP server
- MCP_CALENDAR_ARGS: JSON array of arguments for Calendar MCP server

Example .env:
    MCP_GMAIL_COMMAND=npx
    MCP_GMAIL_ARGS=["@gongrzhe/server-gmail-autoauth-mcp"]
    MCP_CALENDAR_COMMAND=npx
    MCP_CALENDAR_ARGS=["@modelcontextprotocol/server-google-calendar"]
"""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from logging import getLogger
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel

logger = getLogger(__name__)


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str] | None = None


class MCPTool(BaseModel):
    """Represents a tool from an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str


class MCPServerConnection:
    """Manages a connection to a single MCP server."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.session: ClientSession | None = None
        self.tools: list[MCPTool] = []
        self._exit_stack: AsyncExitStack | None = None

    async def connect(self) -> bool:
        """Connect to the MCP server and discover tools."""
        try:
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env or None,
            )

            self._exit_stack = AsyncExitStack()
            
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            
            # Initialize the session before using it
            await self.session.initialize()

            # Discover tools
            tools_result = await self.session.list_tools()
            self.tools = [
                MCPTool(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema,
                    server_name=self.config.name,
                )
                for tool in tools_result.tools
            ]

            logger.info(
                "Connected to MCP server '%s' with %d tools: %s",
                self.config.name,
                len(self.tools),
                [t.name for t in self.tools],
            )
            return True

        except Exception as e:
            logger.error("Failed to connect to MCP server '%s': %s", self.config.name, e)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning("Error disconnecting from MCP server '%s': %s", self.config.name, e)
            finally:
                self._exit_stack = None
                self.session = None
                self.tools = []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool on this MCP server."""
        if not self.session:
            raise RuntimeError(f"Not connected to MCP server '{self.config.name}'")

        try:
            result = await self.session.call_tool(tool_name, arguments)
            
            # Extract content from result
            if result.content:
                # Handle different content types
                text_contents = [
                    c.text for c in result.content if hasattr(c, "text")
                ]
                if text_contents:
                    if len(text_contents) == 1:
                        return text_contents[0]
                    return text_contents
            
            return result.model_dump()

        except Exception as e:
            logger.error(
                "Error calling tool '%s' on MCP server '%s': %s",
                tool_name,
                self.config.name,
                e,
            )
            raise


class MCPManager:
    """Manager for multiple MCP server connections.

    This is a singleton that manages connections to multiple MCP servers
    and provides a unified interface for tool discovery and invocation.
    """

    _instance: "MCPManager | None" = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self.servers: dict[str, MCPServerConnection] = {}
        self._initialized = False

    @classmethod
    async def get_instance(cls) -> "MCPManager":
        """Get the singleton instance, initializing if needed."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = MCPManager()
            if not cls._instance._initialized:
                await cls._instance.initialize()
                cls._instance._initialized = True
            return cls._instance

    def _load_server_configs(self) -> list[MCPServerConfig]:
        """Load MCP server configurations from environment variables.
        
        Supports dynamic discovery of MCP servers via pattern:
        - MCP_<NAME>_COMMAND: Command to run the server
        - MCP_<NAME>_ARGS: JSON array of arguments (optional)
        
        Example:
            MCP_FILE_SERVER_COMMAND=npx
            MCP_FILE_SERVER_ARGS=["@modelcontextprotocol/server-filesystem","/path"]
        """
        configs = []
        discovered_servers: set[str] = set()
        
        # Scan environment variables for MCP_<name>_COMMAND pattern
        for key, value in os.environ.items():
            if key.startswith("MCP_") and key.endswith("_COMMAND"):
                # Extract server name from MCP_<NAME>_COMMAND
                name = key[4:-8].lower()  # Remove "MCP_" prefix and "_COMMAND" suffix
                if name and value:
                    discovered_servers.add(name)
        
        # Also check for MCP_SERVERS JSON config
        servers_json = os.environ.get("MCP_SERVERS")
        if servers_json:
            try:
                servers_config = json.loads(servers_json)
                for name, config in servers_config.items():
                    configs.append(MCPServerConfig(
                        name=name.lower(),
                        command=config.get("command", ""),
                        args=config.get("args", []),
                        env=config.get("env"),
                    ))
            except json.JSONDecodeError:
                logger.warning("Failed to parse MCP_SERVERS JSON")
        
        # Create configs for discovered servers
        for name in discovered_servers:
            command = os.environ.get(f"MCP_{name.upper()}_COMMAND")
            if not command:
                continue
            
            args_str = os.environ.get(f"MCP_{name.upper()}_ARGS", "[]")
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = []
                logger.warning("Failed to parse MCP_%s_ARGS, using empty list", name.upper())
            
            configs.append(MCPServerConfig(
                name=name,
                command=command,
                args=args,
            ))
        
        logger.info("Discovered %d MCP server config(s): %s", len(configs), [c.name for c in configs])
        return configs

    async def initialize(self) -> None:
        """Initialize connections to all configured MCP servers."""
        configs = self._load_server_configs()
        
        if not configs:
            logger.info("No MCP servers configured")
            return

        logger.info("Initializing MCP manager with %d server(s)", len(configs))

        for config in configs:
            connection = MCPServerConnection(config)
            success = await connection.connect()
            if success:
                self.servers[config.name] = connection

        logger.info(
            "MCP manager initialized with %d/%d servers, %d total tools",
            len(self.servers),
            len(configs),
            sum(len(s.tools) for s in self.servers.values()),
        )

    async def shutdown(self) -> None:
        """Disconnect from all MCP servers."""
        for name, connection in list(self.servers.items()):
            await connection.disconnect()
        self.servers.clear()
        self._initialized = False

    def get_all_tools(self) -> list[MCPTool]:
        """Get all tools from all connected MCP servers."""
        tools = []
        for connection in self.servers.values():
            tools.extend(connection.tools)
        return tools

    def get_tools_for_llm(self) -> list[dict[str, Any]]:
        """Get tools in OpenAI function calling format."""
        tools = []
        for mcp_tool in self.get_all_tools():
            tools.append({
                "type": "function",
                "function": {
                    "name": mcp_tool.name,
                    "description": mcp_tool.description,
                    "parameters": mcp_tool.input_schema,
                },
            })
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool by name, finding the appropriate server."""
        for server_name, connection in self.servers.items():
            for tool in connection.tools:
                if tool.name == tool_name:
                    logger.info(
                        "Calling MCP tool '%s' on server '%s' with args: %s",
                        tool_name,
                        server_name,
                        arguments,
                    )
                    return await connection.call_tool(tool_name, arguments)
        
        raise ValueError(f"Tool '{tool_name}' not found in any connected MCP server")

    def is_tool_from_mcp(self, tool_name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        for connection in self.servers.values():
            for tool in connection.tools:
                if tool.name == tool_name:
                    return True
        return False


# Global function for easy access
async def get_mcp_manager() -> MCPManager:
    """Get the initialized MCP manager singleton."""
    return await MCPManager.get_instance()
