from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_fastapi_instrumentator import Instrumentator

from unmute.llm_proxy.config import load_mcp_config, load_mcp_excluded_tools
from unmute.llm_proxy.mcp_client import MCPManager
from unmute.tools import LOCAL_TOOL_HANDLERS, LOCAL_TOOLS

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

UPSTREAM_LLM_URL = os.environ.get("UPSTREAM_LLM_URL", "http://llm:8000").rstrip("/")
if UPSTREAM_LLM_URL.endswith("/v1"):
    UPSTREAM_LLM_URL = UPSTREAM_LLM_URL[: -len("/v1")]

UPSTREAM_LLM_API_KEY = os.environ.get("UPSTREAM_LLM_API_KEY", "ollama")
TOOL_CALLING_ENABLED = os.environ.get("TOOL_CALLING_ENABLED", "1") == "1"
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "6"))

app = FastAPI(title="Unmute LLM Proxy")
Instrumentator().instrument(app).expose(app)

_http_client: httpx.AsyncClient | None = None
_mcp_manager: MCPManager | None = None


async def _http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=120)
    return _http_client


def _upstream_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {UPSTREAM_LLM_API_KEY}",
        "Content-Type": "application/json",
    }


def _extract_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content

    # Some providers can return structured content.
    if isinstance(content, list):
        text_chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    text_chunks.append(text)
        return "".join(text_chunks)

    return ""


def _iter_text_chunks(text: str) -> list[str]:
    if not text:
        return []

    # Preserve whitespace exactly. Splitting on spaces introduces regressions in TTS
    # formatting (missing spaces/newlines) when we synthesize streaming chunks.
    chunk_size = 24
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _sse_line(payload: dict[str, Any] | str) -> bytes:
    if isinstance(payload, str):
        return f"data: {payload}\n\n".encode("utf-8")
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _get_tools_from_request(body: dict[str, Any]) -> list[dict[str, Any]]:
    incoming = body.get("tools", [])
    if isinstance(incoming, list):
        return incoming
    return []


def _build_tools(
    excluded_tools: set[str], incoming_tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    tools = [tool for tool in incoming_tools if isinstance(tool, dict)]

    for tool in LOCAL_TOOLS:
        function = tool.get("function", {})
        name = function.get("name")
        if isinstance(name, str) and name not in excluded_tools:
            tools.append(tool)

    if _mcp_manager is not None:
        tools.extend(
            tool
            for tool in _mcp_manager.openai_tools
            if tool.get("function", {}).get("name") not in excluded_tools
        )

    dedup: dict[str, dict[str, Any]] = {}
    for tool in tools:
        function = tool.get("function", {})
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str):
            dedup[name] = tool

    return list(dedup.values())


async def _call_upstream(payload: dict[str, Any]) -> dict[str, Any]:
    client = await _http()
    response = await client.post(
        f"{UPSTREAM_LLM_URL}/v1/chat/completions",
        headers=_upstream_headers(),
        json=payload,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    parsed = response.json()
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="Invalid upstream response format")

    return parsed


async def _tool_result(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = LOCAL_TOOL_HANDLERS.get(tool_name)
    if handler is not None:
        try:
            return await handler(arguments)
        except Exception as exc:
            return {"error": f"Local tool execution failed: {exc}"}

    if _mcp_manager is not None:
        try:
            return await _mcp_manager.call(tool_name, arguments)
        except Exception as exc:
            return {"error": f"MCP tool execution failed: {exc}"}

    return {"error": f"Unknown tool: {tool_name}"}


async def _resolve_tool_calls(
    body: dict[str, Any],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    messages_raw = body.get("messages")
    if not isinstance(messages_raw, list):
        raise HTTPException(status_code=400, detail="Expected list in 'messages'")

    messages: list[dict[str, Any]] = [m for m in messages_raw if isinstance(m, dict)]

    if not tools:
        passthrough_body = {**body, "stream": False}
        return await _call_upstream(passthrough_body)

    for _ in range(MAX_TOOL_ROUNDS):
        request_payload = {
            **body,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
        }

        result = await _call_upstream(request_payload)
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) == 0:
            return result

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return result

        assistant_message = first_choice.get("message")
        if not isinstance(assistant_message, dict):
            return result

        messages.append(assistant_message)
        tool_calls = assistant_message.get("tool_calls")
        if not isinstance(tool_calls, list) or len(tool_calls) == 0:
            return result

        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue

            tool_call_id = tool_call.get("id")
            function = tool_call.get("function")
            if not isinstance(tool_call_id, str) or not isinstance(function, dict):
                continue

            tool_name = function.get("name")
            args_raw = function.get("arguments", "{}")
            if not isinstance(tool_name, str):
                continue

            try:
                parsed_args = json.loads(args_raw) if isinstance(args_raw, str) else {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {}
            except json.JSONDecodeError:
                parsed_args = {}

            tool_output = await _tool_result(tool_name, parsed_args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(tool_output, ensure_ascii=False),
                }
            )

    raise HTTPException(status_code=400, detail="Maximum tool rounds reached")


async def _stream_final_response(final_response: dict[str, Any]) -> AsyncIterator[bytes]:
    model = str(final_response.get("model", "unknown"))
    created = int(time.time())
    response_id = str(final_response.get("id", f"chatcmpl-{uuid.uuid4().hex}"))

    role_chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield _sse_line(role_chunk)

    choices = final_response.get("choices", [])
    message: dict[str, Any] = {}
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            maybe_message = first_choice.get("message", {})
            if isinstance(maybe_message, dict):
                message = maybe_message
    text = _extract_message_text(message)
    for chunk in _iter_text_chunks(text):
        payload = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": chunk},
                    "finish_reason": None,
                }
            ],
        }
        yield _sse_line(payload)

    end_payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield _sse_line(end_payload)
    yield _sse_line("[DONE]")


@app.on_event("startup")
async def startup_event() -> None:
    global _mcp_manager
    excluded = load_mcp_excluded_tools().excluded_tools
    mcp_config = load_mcp_config()
    _mcp_manager = MCPManager(mcp_config.servers, excluded)
    await _mcp_manager.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if _mcp_manager is not None:
        await _mcp_manager.stop()

    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Unmute LLM proxy running"}


@app.get("/v1/models")
async def models() -> JSONResponse:
    client = await _http()
    response = await client.get(
        f"{UPSTREAM_LLM_URL}/v1/models",
        headers=_upstream_headers(),
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return JSONResponse(content=response.json())


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")

    requested_stream = bool(body.get("stream", False))

    excluded = load_mcp_excluded_tools().excluded_tools
    incoming_tools = _get_tools_from_request(body)
    tools = _build_tools(excluded, incoming_tools)

    # Lowest-latency path: pure passthrough when there is no tool-calling work to do.
    if (not TOOL_CALLING_ENABLED) or not tools:
        client = await _http()
        if requested_stream:

            async def stream_passthrough() -> AsyncIterator[bytes]:
                async with client.stream(
                    "POST",
                    f"{UPSTREAM_LLM_URL}/v1/chat/completions",
                    headers=_upstream_headers(),
                    json=body,
                ) as response:
                    if response.status_code >= 400:
                        error_payload = (await response.aread()).decode(
                            "utf-8", errors="replace"
                        )
                        raise HTTPException(
                            status_code=response.status_code,
                            detail=error_payload,
                        )
                    async for chunk in response.aiter_bytes():
                        yield chunk

            return StreamingResponse(
                stream_passthrough(),
                media_type="text/event-stream",
            )

        result = await _call_upstream(body)
        return JSONResponse(content=result)

    final_response = await _resolve_tool_calls(body, tools)

    if requested_stream:
        return StreamingResponse(
            _stream_final_response(final_response),
            media_type="text/event-stream",
        )

    return JSONResponse(content=final_response)
