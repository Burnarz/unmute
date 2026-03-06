from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

from unmute.llm_proxy.config import MCPServerConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MCPTool:
    server_name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def openai_name(self) -> str:
        return f"mcp__{self.server_name}__{self.tool_name}"

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.openai_name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class MCPProtocolError(RuntimeError):
    pass


class MCPWireProtocol(str, Enum):
    LSP = "lsp"  # Content-Length framed JSON-RPC over stdio
    JSON_LINE = "json-line"  # One JSON-RPC object per line


class MCPServerProcess:
    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._id_counter = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._wire_protocol = MCPWireProtocol.LSP

    async def _auto_install(self) -> None:
        if not self.config.install:
            return

        logger.info("Installing MCP server dependencies for %s", self.config.name)
        proc = await asyncio.create_subprocess_exec(
            *self.config.install,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **self.config.env},
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            stderr_str = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Failed to install MCP server '{self.config.name}': {stderr_str}"
            )

    async def start(self) -> None:
        await self._auto_install()
        errors: list[str] = []

        for protocol in [MCPWireProtocol.LSP, MCPWireProtocol.JSON_LINE]:
            self._wire_protocol = protocol
            await self._spawn_process()
            try:
                await self._initialize()
                return
            except Exception as exc:
                stderr_tail = "\n".join(self._stderr_tail).strip()
                returncode = self._process.returncode if self._process else None
                errors.append(
                    f"{protocol.value}: {type(exc).__name__}: {exc}; "
                    f"returncode={returncode}; stderr_tail={stderr_tail!r}"
                )
                await self.stop()

        raise RuntimeError(
            f"Failed to initialize MCP server '{self.config.name}' with supported "
            f"protocols. Details: {' | '.join(errors)}"
        )

    async def _spawn_process(self) -> None:
        self._stderr_tail.clear()
        self._process = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **self.config.env},
        )

        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._stdout_task = asyncio.create_task(self._read_loop(self._process.stdout))
        self._stderr_task = asyncio.create_task(self._read_stderr(self._process.stderr))

    async def stop(self) -> None:
        if self._stdout_task is not None:
            self._stdout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stdout_task

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task

        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            with contextlib.suppress(ProcessLookupError):
                await asyncio.wait_for(self._process.wait(), timeout=2)

    async def _initialize(self) -> None:
        response = await asyncio.wait_for(
            self.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "unmute-llm-proxy", "version": "0.1.0"},
                },
            ),
            timeout=self.config.startup_timeout_sec,
        )

        if "result" not in response:
            raise MCPProtocolError(
                f"Initialize failed for {self.config.name}: missing result"
            )

        await self.notify("notifications/initialized", {})

    async def list_tools(self) -> list[MCPTool]:
        response = await asyncio.wait_for(
            self.request("tools/list", {}), timeout=self.config.tool_timeout_sec
        )
        result = response.get("result", {})
        tools_raw = result.get("tools", [])

        tools: list[MCPTool] = []
        for tool in tools_raw:
            if not isinstance(tool, dict):
                continue

            name = tool.get("name")
            if not isinstance(name, str) or name.strip() == "":
                continue

            description = tool.get("description")
            if not isinstance(description, str):
                description = f"MCP tool {name} from {self.config.name}"

            input_schema = tool.get("inputSchema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}

            tools.append(
                MCPTool(
                    server_name=self.config.name,
                    tool_name=name,
                    description=description,
                    input_schema=input_schema,
                )
            )

        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await asyncio.wait_for(
            self.request(
                "tools/call",
                {
                    "name": tool_name,
                    "arguments": arguments,
                },
            ),
            timeout=self.config.tool_timeout_sec,
        )

        if "error" in response:
            return {
                "error": response["error"],
            }

        result = response.get("result", {})
        return result if isinstance(result, dict) else {"result": result}

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id_counter += 1
        request_id = str(self._id_counter)

        fut: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = fut

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        await self._write_message(payload)
        return await fut

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._write_message(payload)

    async def _write_message(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError(f"MCP server {self.config.name} is not running")

        body = json.dumps(message).encode("utf-8")

        async with self._write_lock:
            if self._wire_protocol == MCPWireProtocol.LSP:
                header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                self._process.stdin.write(header + body)
            else:
                self._process.stdin.write(body + b"\n")
            await self._process.stdin.drain()

    async def _read_loop(self, stdout: asyncio.StreamReader) -> None:
        try:
            message_iterator = (
                self._iter_messages_lsp(stdout)
                if self._wire_protocol == MCPWireProtocol.LSP
                else self._iter_messages_json_line(stdout)
            )
            async for msg in message_iterator:
                if "id" not in msg:
                    continue

                request_id = msg.get("id")
                if not isinstance(request_id, int | str):
                    continue

                fut = self._pending.pop(str(request_id), None)
                if fut is None or fut.done():
                    continue
                fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP read loop failed for %s", self.config.name)
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(
                        MCPProtocolError(f"MCP server {self.config.name} read loop failed")
                    )

    async def _read_stderr(self, stderr: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                if decoded:
                    self._stderr_tail.append(decoded)
        except asyncio.CancelledError:
            raise

    async def _iter_messages_lsp(
        self, stdout: asyncio.StreamReader
    ) -> AsyncIterator[dict[str, Any]]:
        while True:
            headers: dict[str, str] = {}
            while True:
                line = await stdout.readline()
                if not line:
                    return

                if line in (b"\r\n", b"\n"):
                    break

                decoded = line.decode("ascii", errors="replace").strip()
                key, _, value = decoded.partition(":")
                headers[key.lower()] = value.strip()

            content_length = headers.get("content-length")
            if content_length is None:
                continue

            try:
                length = int(content_length)
            except ValueError:
                continue

            body = await stdout.readexactly(length)
            decoded_body = body.decode("utf-8", errors="replace")
            parsed = json.loads(decoded_body)
            if isinstance(parsed, dict):
                yield parsed

    async def _iter_messages_json_line(
        self, stdout: asyncio.StreamReader
    ) -> AsyncIterator[dict[str, Any]]:
        while True:
            line = await stdout.readline()
            if not line:
                return

            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded == "":
                continue

            try:
                parsed = json.loads(decoded)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                yield parsed


class MCPManager:
    def __init__(self, server_configs: list[MCPServerConfig], excluded_tools: set[str]):
        self._server_configs = [cfg for cfg in server_configs if cfg.enabled]
        self._excluded_tools = excluded_tools
        self._servers: dict[str, MCPServerProcess] = {}
        self._tool_mapping: dict[str, tuple[MCPServerProcess, str]] = {}
        self._openai_tools: list[dict[str, Any]] = []

    @property
    def openai_tools(self) -> list[dict[str, Any]]:
        return self._openai_tools

    async def start(self) -> None:
        for cfg in self._server_configs:
            try:
                server = MCPServerProcess(cfg)
                await server.start()
                self._servers[cfg.name] = server

                tools = await server.list_tools()
                for tool in tools:
                    # Allow exclusion by short name (tool_name) or full name (openai_name)
                    if tool.openai_name in self._excluded_tools or tool.tool_name in self._excluded_tools:
                        logger.info("Excluding MCP tool: %s (short name match: %s)", 
                                    tool.openai_name, tool.tool_name in self._excluded_tools)
                        continue
                    self._tool_mapping[tool.openai_name] = (server, tool.tool_name)
                    self._openai_tools.append(tool.to_openai_tool())
            except Exception:
                logger.exception("Failed to start MCP server '%s'", cfg.name)

    async def stop(self) -> None:
        for server in self._servers.values():
            await server.stop()

    async def call(self, openai_tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        target = self._tool_mapping.get(openai_tool_name)
        if target is None:
            return {"error": f"Unknown MCP tool: {openai_tool_name}"}

        server, original_tool_name = target
        return await server.call_tool(original_tool_name, arguments)
