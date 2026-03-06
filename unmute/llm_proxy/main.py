from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_fastapi_instrumentator import Instrumentator

from unmute.llm_proxy.config import load_mcp_config, load_mcp_excluded_tools
from unmute.llm_proxy.mcp_client import MCPManager
from unmute.tools import LOCAL_TOOL_HANDLERS, LOCAL_TOOLS, get_runtime_settings

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

UPSTREAM_LLM_URL = os.environ.get("UPSTREAM_LLM_URL", "http://llm:8000").rstrip("/")
if UPSTREAM_LLM_URL.endswith("/v1"):
    UPSTREAM_LLM_URL = UPSTREAM_LLM_URL[: -len("/v1")]

UPSTREAM_LLM_API_KEY = os.environ.get("UPSTREAM_LLM_API_KEY", "ollama")
UPSTREAM_API_STYLE = os.environ.get("UPSTREAM_API_STYLE", "openai").strip().lower()
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
    headers = {"Content-Type": "application/json"}
    if UPSTREAM_LLM_API_KEY.strip() != "":
        headers["Authorization"] = f"Bearer {UPSTREAM_LLM_API_KEY}"
    return headers


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


def _current_thinking_mode() -> str:
    settings = get_runtime_settings()
    mode = str(settings.get("thinking_mode", "off")).strip().lower()
    if mode in {"true", "on"}:
        return "on"
    if mode in {"false", "off"}:
        return "off"
    if mode in {"low", "medium", "high"}:
        return mode
    return "off"


def _apply_openai_thinking(payload: dict[str, Any]) -> dict[str, Any]:
    mode = _current_thinking_mode()
    out = dict(payload)

    if mode == "off":
        out["reasoning"] = {"enabled": False}
        out["thinking"] = False
        out.pop("reasoning_effort", None)
        return out

    if mode == "on":
        out["reasoning"] = {"enabled": True}
        out["thinking"] = True
        out.pop("reasoning_effort", None)
        return out

    # gpt-oss style levels
    out["reasoning"] = {"effort": mode}
    out["reasoning_effort"] = mode
    out["thinking"] = mode
    return out


def _ollama_think_value() -> bool | str:
    mode = _current_thinking_mode()
    if mode == "off":
        return False
    if mode == "on":
        return True
    return mode


def _openai_to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if not isinstance(role, str):
            continue

        mapped: dict[str, Any] = {
            "role": role,
            "content": _extract_message_text(message),
        }

        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                converted_calls = []
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    tool_name = function.get("name")
                    if not isinstance(tool_name, str):
                        continue
                    arguments_raw = function.get("arguments", "{}")
                    if isinstance(arguments_raw, str):
                        try:
                            arguments = json.loads(arguments_raw)
                        except json.JSONDecodeError:
                            arguments = {}
                    elif isinstance(arguments_raw, dict):
                        arguments = arguments_raw
                    else:
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}

                    converted_calls.append(
                        {
                            "function": {
                                "name": tool_name,
                                "arguments": arguments,
                            }
                        }
                    )
                if converted_calls:
                    mapped["tool_calls"] = converted_calls

        if role == "tool":
            name = message.get("name")
            if isinstance(name, str) and name != "":
                mapped["name"] = name

        out.append(mapped)
    return out


def _openai_to_ollama_request(
    payload: dict[str, Any],
    *,
    force_stream: bool | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    messages_raw = payload.get("messages", [])
    if not isinstance(messages_raw, list):
        messages_raw = []
    messages = [m for m in messages_raw if isinstance(m, dict)]

    req: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": _openai_to_ollama_messages(messages),
        "stream": payload.get("stream", False) if force_stream is None else force_stream,
        "think": _ollama_think_value(),
    }

    if tools is None:
        tools = _get_tools_from_request(payload)
    if tools:
        req["tools"] = tools

    if "temperature" in payload:
        req["options"] = {"temperature": payload["temperature"]}

    return req


def _ollama_message_to_openai(
    ollama_message: dict[str, Any], *, response_id: str
) -> dict[str, Any]:
    content = ollama_message.get("content")
    if not isinstance(content, str):
        content = ""

    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }

    tool_calls = ollama_message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        openai_tool_calls = []
        for i, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            tool_name = function.get("name")
            if not isinstance(tool_name, str):
                continue
            arguments = function.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            openai_tool_calls.append(
                {
                    "id": f"{response_id}-tool-{i}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )

        if openai_tool_calls:
            message["tool_calls"] = openai_tool_calls

    return message


def _normalize_openai_response_from_ollama(ollama_resp: dict[str, Any]) -> dict[str, Any]:
    model = str(ollama_resp.get("model", "unknown"))
    created_iso = ollama_resp.get("created_at")
    if isinstance(created_iso, str):
        try:
            created = int(datetime.fromisoformat(created_iso.replace("Z", "+00:00")).timestamp())
        except ValueError:
            created = int(time.time())
    else:
        created = int(time.time())

    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    ollama_message = ollama_resp.get("message")
    if not isinstance(ollama_message, dict):
        ollama_message = {"role": "assistant", "content": ""}
    openai_message = _ollama_message_to_openai(ollama_message, response_id=response_id)

    finish_reason = "stop"
    if openai_message.get("tool_calls"):
        finish_reason = "tool_calls"

    usage: dict[str, int] = {}
    prompt_tokens = ollama_resp.get("prompt_eval_count")
    completion_tokens = ollama_resp.get("eval_count")
    if isinstance(prompt_tokens, int):
        usage["prompt_tokens"] = prompt_tokens
    if isinstance(completion_tokens, int):
        usage["completion_tokens"] = completion_tokens
    if usage:
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get(
            "completion_tokens", 0
        )

    out: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": openai_message,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage:
        out["usage"] = usage
    return out


async def _call_upstream(payload: dict[str, Any]) -> dict[str, Any]:
    client = await _http()
    if UPSTREAM_API_STYLE == "openai":
        openai_payload = _apply_openai_thinking(payload)
        response = await client.post(
            f"{UPSTREAM_LLM_URL}/v1/chat/completions",
            headers=_upstream_headers(),
            json=openai_payload,
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        parsed = response.json()
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=502, detail="Invalid upstream response format")
        return parsed

    if UPSTREAM_API_STYLE == "ollama":
        ollama_payload = _openai_to_ollama_request(payload, force_stream=False)
        response = await client.post(
            f"{UPSTREAM_LLM_URL}/api/chat",
            headers=_upstream_headers(),
            json=ollama_payload,
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=502, detail="Invalid upstream response format")
        return _normalize_openai_response_from_ollama(parsed)

    raise HTTPException(
        status_code=500,
        detail=f"Unsupported UPSTREAM_API_STYLE={UPSTREAM_API_STYLE!r}",
    )


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
                    "name": tool_name,
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(tool_output, ensure_ascii=False),
                }
            )

    raise HTTPException(status_code=400, detail="Maximum tool rounds reached")


async def _resolve_tool_calls_streaming(
    body: dict[str, Any],
    tools: list[dict[str, Any]],
) -> AsyncIterator[bytes]:
    messages_raw = body.get("messages", [])
    if not isinstance(messages_raw, list):
        raise HTTPException(status_code=400, detail="Expected list in 'messages'")

    messages: list[dict[str, Any]] = [m for m in messages_raw if isinstance(m, dict)]

    client = await _http()
    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    model = str(body.get("model", "unknown"))

    for _ in range(MAX_TOOL_ROUNDS):
        # Determine upstream request based on style
        if UPSTREAM_API_STYLE == "openai":
            upstream_url = f"{UPSTREAM_LLM_URL}/v1/chat/completions"
            payload = _apply_openai_thinking({**body, "messages": messages, "tools": tools, "stream": True})
        elif UPSTREAM_API_STYLE == "ollama":
            upstream_url = f"{UPSTREAM_LLM_URL}/api/chat"
            payload = _openai_to_ollama_request(body, force_stream=True, tools=tools)
            # Update messages in the payload to include current history
            payload["messages"] = _openai_to_ollama_messages(messages)
        else:
            raise HTTPException(status_code=500, detail=f"Unsupported UPSTREAM_API_STYLE={UPSTREAM_API_STYLE}")

        async with client.stream(
            "POST",
            upstream_url,
            headers=_upstream_headers(),
            json=payload,
            timeout=120,
        ) as response:
            if response.status_code >= 400:
                error_payload = (await response.aread()).decode("utf-8", errors="replace")
                raise HTTPException(status_code=response.status_code, detail=error_payload)

            determined_type = False
            is_tool_call = False
            
            # Accumulators for tool calls
            current_tool_calls: dict[int, dict[str, Any]] = {}
            full_content_acc = []

            # First chunk role emission (only on the very last turn that yields to client)
            role_emitted = False

            async for line in response.aiter_lines():
                if not line or line.strip() == "":
                    continue
                
                # Handle SSE prefix if OpenAI
                chunk_data = line.strip()
                if chunk_data.startswith("data: "):
                    chunk_data = chunk_data[len("data: "):]
                
                if chunk_data == "[DONE]":
                    break
                
                try:
                    parsed = json.loads(chunk_data)
                except json.JSONDecodeError:
                    continue

                # Normalize chunk based on source
                delta: dict[str, Any] = {}
                finish_reason = None
                
                if UPSTREAM_API_STYLE == "openai":
                    choices = parsed.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        finish_reason = choices[0].get("finish_reason")
                else:  # ollama
                    msg = parsed.get("message", {})
                    delta = {"content": msg.get("content", ""), "tool_calls": msg.get("tool_calls")}
                    if parsed.get("done"):
                        finish_reason = "stop" if not msg.get("tool_calls") else "tool_calls"

                # Check if we are starting a tool call or content
                if not determined_type:
                    if delta.get("tool_calls") or (UPSTREAM_API_STYLE == "openai" and "tool_calls" in delta):
                        is_tool_call = True
                    elif delta.get("content") or "content" in delta:
                        is_tool_call = False
                    
                    if delta.get("tool_calls") or delta.get("content") or finish_reason:
                        determined_type = True

                if is_tool_call:
                    # Accumulate tool calls for later execution
                    tcs = delta.get("tool_calls", [])
                    if tcs:
                        for tc in tcs:
                            idx = tc.get("index", 0)
                            if idx not in current_tool_calls:
                                current_tool_calls[idx] = {"id": tc.get("id"), "type": "function", "function": {"name": "", "arguments": ""}}
                            
                            if tc.get("id"):
                                current_tool_calls[idx]["id"] = tc["id"]
                            
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                current_tool_calls[idx]["function"]["name"] += fn["name"]
                            
                            args_delta = fn.get("arguments")
                            if args_delta:
                                if isinstance(args_delta, dict):
                                    # If it's a dict, it's likely the full arguments object
                                    args_delta = json.dumps(args_delta, ensure_ascii=False)
                                
                                current_tool_calls[idx]["function"]["arguments"] += args_delta
                else:
                    # This is final content, stream it to client!
                    if not role_emitted:
                        yield _sse_line({
                            "id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
                        })
                        role_emitted = True
                    
                    content_piece = delta.get("content", "")
                    if content_piece:
                        full_content_acc.append(content_piece)
                        yield _sse_line({
                            "id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {"content": content_piece}, "finish_reason": None}]
                        })
                    
                    if finish_reason:
                        yield _sse_line({
                            "id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
                        })
                        yield _sse_line("[DONE]")
                        return # Exit the entire generator, we are done!

            # If we finished the stream and it was a tool call, process and loop
            if is_tool_call:
                tool_calls_list = [v for k, v in sorted(current_tool_calls.items())]
                messages.append({"role": "assistant", "tool_calls": tool_calls_list})
                
                for tc in tool_calls_list:
                    tool_name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except:
                        args = {}
                    
                    tool_output = await _tool_result(tool_name, args)
                    messages.append({
                        "role": "tool",
                        "name": tool_name,
                        "tool_call_id": tc["id"],
                        "content": json.dumps(tool_output, ensure_ascii=False)
                    })
                # Loop continues to next round
            else:
                # Fallback if stream ended without finish_reason
                yield _sse_line("[DONE]")
                return

    raise HTTPException(status_code=400, detail="Maximum tool rounds reached")


async def _stream_ollama_passthrough(body: dict[str, Any]) -> AsyncIterator[bytes]:
    client = await _http()
    payload = _openai_to_ollama_request(body, force_stream=True)

    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    model = str(body.get("model", "unknown"))

    role_chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield _sse_line(role_chunk)

    async with client.stream(
        "POST",
        f"{UPSTREAM_LLM_URL}/api/chat",
        headers=_upstream_headers(),
        json=payload,
        timeout=120,
    ) as response:
        if response.status_code >= 400:
            error_payload = (await response.aread()).decode("utf-8", errors="replace")
            raise HTTPException(
                status_code=response.status_code,
                detail=error_payload,
            )

        async for line in response.aiter_lines():
            if line is None:
                continue
            stripped = line.strip()
            if stripped == "":
                continue

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue

            if "error" in parsed:
                raise HTTPException(status_code=502, detail=str(parsed["error"]))

            message = parsed.get("message")
            if isinstance(message, dict):
                piece = message.get("content")
                if isinstance(piece, str) and piece != "":
                    payload_chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": str(parsed.get("model", model)),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": piece},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield _sse_line(payload_chunk)

            done = parsed.get("done")
            if isinstance(done, bool) and done:
                end_payload = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": str(parsed.get("model", model)),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield _sse_line(end_payload)
                yield _sse_line("[DONE]")
                return

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
    if UPSTREAM_API_STYLE == "openai":
        response = await client.get(
            f"{UPSTREAM_LLM_URL}/v1/models",
            headers=_upstream_headers(),
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return JSONResponse(content=response.json())

    if UPSTREAM_API_STYLE == "ollama":
        response = await client.get(
            f"{UPSTREAM_LLM_URL}/api/tags",
            headers=_upstream_headers(),
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        parsed = response.json()
        models_raw = parsed.get("models", []) if isinstance(parsed, dict) else []
        data = []
        if isinstance(models_raw, list):
            for model in models_raw:
                if not isinstance(model, dict):
                    continue
                name = model.get("name")
                if isinstance(name, str):
                    data.append({"id": name, "object": "model", "owned_by": "ollama"})
        return JSONResponse(content={"object": "list", "data": data})

    raise HTTPException(
        status_code=500,
        detail=f"Unsupported UPSTREAM_API_STYLE={UPSTREAM_API_STYLE!r}",
    )


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
        if requested_stream:
            if UPSTREAM_API_STYLE == "openai":
                client = await _http()
                openai_body = _apply_openai_thinking(body)

                async def stream_passthrough() -> AsyncIterator[bytes]:
                    async with client.stream(
                        "POST",
                        f"{UPSTREAM_LLM_URL}/v1/chat/completions",
                        headers=_upstream_headers(),
                        json=openai_body,
                        timeout=120,
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

            if UPSTREAM_API_STYLE == "ollama":
                return StreamingResponse(
                    _stream_ollama_passthrough(body),
                    media_type="text/event-stream",
                )

            raise HTTPException(
                status_code=500,
                detail=f"Unsupported UPSTREAM_API_STYLE={UPSTREAM_API_STYLE!r}",
            )

        result = await _call_upstream(body)
        return JSONResponse(content=result)

    if requested_stream:
        return StreamingResponse(
            _resolve_tool_calls_streaming(body, tools),
            media_type="text/event-stream",
        )

    final_response = await _resolve_tool_calls(body, tools)
    return JSONResponse(content=final_response)
