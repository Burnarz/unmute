import asyncio

import pytest

from unmute.llm_proxy.config import MCPServerConfig
from unmute.llm_proxy.mcp_client import MCPManager


def test_mcp_manager_start_raises_when_enabled_server_fails():
    class ExplodingServer:
        def __init__(self, config: MCPServerConfig):
            self.config = config

        async def start(self) -> None:
            raise RuntimeError("boom")

    config = MCPServerConfig(
        name="google-workspace",
        command=["python", "server.py"],
        install=[],
        env={},
    )

    from unittest.mock import patch

    with patch("unmute.llm_proxy.mcp_client.MCPServerProcess", new=ExplodingServer):
        manager = MCPManager([config], excluded_tools=set())
        with pytest.raises(RuntimeError, match="google-workspace: RuntimeError: boom"):
            asyncio.run(manager.start())
