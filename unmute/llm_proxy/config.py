from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MCP_SERVERS_PATH = Path(
    os.environ.get("MCP_SERVERS_CONFIG", "/app/mcp-servers.json")
)
DEFAULT_MCP_EXCLUDED_TOOLS_PATH = Path(
    os.environ.get("MCP_TOOLS_EXCLUDED_CONFIG", "/app/mcp-tools-excluded.json")
)


@dataclass(slots=True)
class MCPServerConfig:
    name: str
    command: list[str]
    install: list[str] | None
    env: dict[str, str]
    enabled: bool = True
    startup_timeout_sec: float = 10.0
    tool_timeout_sec: float = 15.0
    cwd: str | None = None


@dataclass(slots=True)
class MCPConfig:
    servers: list[MCPServerConfig]


@dataclass(slots=True)
class MCPExcludedTools:
    excluded_tools: set[str]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        loaded = json.load(f)

    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid config format for {path}: expected JSON object")
    return loaded


def load_mcp_config(path: Path = DEFAULT_MCP_SERVERS_PATH) -> MCPConfig:
    raw = _load_json(path)
    servers_raw = raw.get("servers", [])
    if not isinstance(servers_raw, list):
        raise ValueError(f"Invalid 'servers' in {path}: expected list")

    servers: list[MCPServerConfig] = []
    for server in servers_raw:
        if not isinstance(server, dict):
            raise ValueError(f"Invalid server entry in {path}: expected object")

        command = server.get("command")
        if not isinstance(command, list) or not all(
            isinstance(item, str) for item in command
        ):
            raise ValueError(
                f"Invalid command for server {server.get('name', '<unknown>')}"
            )

        env_raw = server.get("env", {})
        if not isinstance(env_raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()
        ):
            raise ValueError(
                f"Invalid env for server {server.get('name', '<unknown>')}"
            )

        install_raw = server.get("install")
        if install_raw is not None and (
            not isinstance(install_raw, list)
            or not all(isinstance(item, str) for item in install_raw)
        ):
            raise ValueError(
                f"Invalid install command for server {server.get('name', '<unknown>')}"
            )

        name = server.get("name")
        if not isinstance(name, str) or name.strip() == "":
            raise ValueError("Each MCP server must have a non-empty string 'name'")

        servers.append(
            MCPServerConfig(
                name=name,
                command=command,
                install=install_raw,
                env=env_raw,
                enabled=bool(server.get("enabled", True)),
                startup_timeout_sec=float(server.get("startup_timeout_sec", 10.0)),
                tool_timeout_sec=float(server.get("tool_timeout_sec", 15.0)),
                cwd=server.get("cwd"),
            )
        )

    return MCPConfig(servers=servers)


def load_mcp_excluded_tools(
    path: Path = DEFAULT_MCP_EXCLUDED_TOOLS_PATH,
) -> MCPExcludedTools:
    raw = _load_json(path)
    excluded_tools_raw = raw.get("excluded_tools", [])
    if not isinstance(excluded_tools_raw, list) or not all(
        isinstance(item, str) for item in excluded_tools_raw
    ):
        raise ValueError(f"Invalid 'excluded_tools' in {path}: expected list[str]")

    return MCPExcludedTools(excluded_tools=set(excluded_tools_raw))
