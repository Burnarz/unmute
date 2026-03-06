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
    if isinstance(content, list):
        text_chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    text_chunks.append(text)
        return "".join(text_chunks)
    return ""


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
        name = tool.get("function", {}).get("name")
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
        name = tool.get("function", {}).get("name")
        if isinstance(name, str):
            dedup[name] = tool
    return list(dedup.values())


def _current_thinking_mode() -> str:
    settings = get_runtime_settings()
    mode = str(settings.get("thinking_mode", "off")).strip().lower()
    if mode in {"true", "on"}: return "on"
    if mode in {"false", "off"}: return "off"
    if mode in {"low", "medium", "high"}: return mode
    return "off"


def _apply_openai_thinking(payload: dict[str, Any]) -> dict[str, Any]:
    mode = _current_thinking_mode()
    out = dict(payload)
    if mode == "off":
        out["reasoning"] = {"enabled": False}
        out["thinking"] = False
        out.pop("reasoning_effort", None)
    elif mode == "on":
        out["reasoning"] = {"enabled": True}
        out["thinking"] = True
        out.pop("reasoning_effort", None)
    else:
        out["reasoning"] = {"effort": mode}
        out["reasoning_effort"] = mode
        out["thinking"] = mode
    return out


def _openai_to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if not isinstance(role, str): continue
        
        mapped: dict[str, Any] = {
            "role": role,
            "content": _extract_message_text(message),
        }

        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                converted_calls = []
                for tc in tool_calls:
                    if not isinstance(tc, dict): continue
                    fn = tc.get("function", {})
                    name = fn.get("name")
                    if not isinstance(name, str): continue
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except:
                        args = {}
                    converted_calls.append({"function": {"name": name, "arguments": args}})
                if converted_calls:
                    mapped["tool_calls"] = converted_calls

        if role == "tool":
            # Ollama expects 'tool' role for results.
            # Some versions of Ollama don't use 'tool_call_id' but depend on order.
            pass

        out.append(mapped)
    return out


def _openai_to_ollama_request(
    payload: dict[str, Any],
    *,
    force_stream: bool | None = None,
    tools: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if messages is None:
        messages = [m for m in payload.get("messages", []) if isinstance(m, dict)]

    req: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": _openai_to_ollama_messages(messages),
        "stream": payload.get("stream", False) if force_stream is None else force_stream,
    }
    if tools is None:
        tools = _get_tools_from_request(payload)
    if tools:
        req["tools"] = tools
    if "temperature" in payload:
        req["options"] = {"temperature": payload["temperature"]}
    return req


def _ollama_message_to_openai(ollama_message: dict[str, Any], *, response_id: str) -> dict[str, Any]:
    content = ollama_message.get("content", "")
    message: dict[str, Any] = {"role": "assistant", "content": content}
    tool_calls = ollama_message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        openai_tool_calls = []
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            openai_tool_calls.append({
                "id": f"{response_id}-tool-{i}",
                "type": "function",
                "function": {
                    "name": fn.get("name"),
                    "arguments": json.dumps(fn.get("arguments", {}), ensure_ascii=False),
                },
            })
        if openai_tool_calls:
            message["tool_calls"] = openai_tool_calls
    return message


async def _tool_result(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = LOCAL_TOOL_HANDLERS.get(tool_name)
    if handler:
        try: return await handler(arguments)
        except Exception as e: return {"error": f"Local tool failed: {e}"}
    if _mcp_manager:
        try: return await _mcp_manager.call(tool_name, arguments)
        except Exception as e: return {"error": f"MCP tool failed: {e}"}
    return {"error": f"Unknown tool: {tool_name}"}


async def _resolve_tool_calls_streaming(
    body: dict[str, Any],
    tools: list[dict[str, Any]],
) -> AsyncIterator[bytes]:
    messages: list[dict[str, Any]] = [m for m in body.get("messages", []) if isinstance(m, dict)]
    client = await _http()
    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    created, model = int(time.time()), str(body.get("model", "unknown"))
    role_emitted = False

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info("Starting tool round %d", round_idx)
        if UPSTREAM_API_STYLE == "openai":
            url = f"{UPSTREAM_LLM_URL}/v1/chat/completions"
            payload = _apply_openai_thinking({**body, "messages": messages, "tools": tools, "stream": True})
        else:
            url = f"{UPSTREAM_LLM_URL}/api/chat"
            payload = _openai_to_ollama_request(body, force_stream=True, tools=tools, messages=messages)

        async with client.stream("POST", url, headers=_upstream_headers(), json=payload, timeout=120) as response:
            if response.status_code >= 400:
                err = (await response.aread()).decode("utf-8", errors="replace")
                logger.error("Upstream error: %s", err)
                raise HTTPException(status_code=response.status_code, detail=err)

            current_tcs: dict[int, dict[str, Any]] = {}
            full_content: list[str] = []
            loop_finish_reason = None
            
            async for line in response.aiter_lines():
                line = line.strip()
                if not line: continue
                chunk_data = line[6:] if line.startswith("data: ") else line
                if not chunk_data or chunk_data == "[DONE]": break
                
                try: parsed = json.loads(chunk_data)
                except: continue

                delta, fr = {}, None
                if UPSTREAM_API_STYLE == "openai":
                    choices = parsed.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        fr = choices[0].get("finish_reason")
                else:
                    msg = parsed.get("message", {})
                    delta = {"content": msg.get("content", ""), "tool_calls": msg.get("tool_calls")}
                    if parsed.get("done"):
                        fr = "tool_calls" if (msg.get("tool_calls") or current_tcs) else "stop"

                content_piece = delta.get("content", "")
                if content_piece:
                    if not role_emitted:
                        yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
                        role_emitted = True
                    full_content.append(content_piece)
                    yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                     "choices": [{"index": 0, "delta": {"content": content_piece}, "finish_reason": None}]})

                tcs = delta.get("tool_calls", [])
                if tcs:
                    for tc in tcs:
                        idx = tc.get("index", 0)
                        if idx not in current_tcs:
                            current_tcs[idx] = {"id": tc.get("id"), "type": "function", "function": {"name": "", "arguments": ""}}
                        if tc.get("id"): current_tcs[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"): current_tcs[idx]["function"]["name"] += fn["name"]
                        args = fn.get("arguments", "")
                        if args:
                            current_tcs[idx]["function"]["arguments"] += (json.dumps(args) if isinstance(args, dict) else args)

                if fr:
                    loop_finish_reason = fr
                    # Do not return yet, let the loop finish to ensure we saw everything
                    if fr == "tool_calls": break

            if current_tcs:
                logger.info("Round %d found %d tool calls", round_idx, len(current_tcs))
                tc_list = [v for k, v in sorted(current_tcs.items())]
                messages.append({"role": "assistant", "content": "".join(full_content), "tool_calls": tc_list})
                for tc in tc_list:
                    name = tc["function"]["name"]
                    try: args = json.loads(tc["function"]["arguments"])
                    except: args = {}
                    logger.info("Executing tool: %s", name)
                    res = await _tool_result(name, args)
                    messages.append({"role": "tool", "name": name, "tool_call_id": tc.get("id") or f"tc-{uuid.uuid4().hex}", 
                                     "content": json.dumps(res, ensure_ascii=False)})
            elif loop_finish_reason == "stop" or not full_content:
                logger.info("Round %d finished with stop", round_idx)
                yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield _sse_line("[DONE]")
                return
            else:
                # Content was streamed but no more tools, we're likely done
                logger.info("Round %d finished after streaming content", round_idx)
                yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield _sse_line("[DONE]")
                return

    raise HTTPException(status_code=400, detail="Max tool rounds reached")


@app.on_event("startup")
async def startup_event() -> None:
    global _mcp_manager
    cfg = load_mcp_config()
    excl = load_mcp_excluded_tools().excluded_tools
    _mcp_manager = MCPManager(cfg.servers, excl)
    await _mcp_manager.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if _mcp_manager: await _mcp_manager.stop()
    if _http_client: await _http_client.aclose()


@app.get("/")
async def root(): return {"message": "Unmute LLM proxy running"}


@app.get("/v1/models")
async def models():
    c = await _http()
    if UPSTREAM_API_STYLE == "openai":
        r = await c.get(f"{UPSTREAM_LLM_URL}/v1/models", headers=_upstream_headers())
        return JSONResponse(content=r.json())
    r = await c.get(f"{UPSTREAM_LLM_URL}/api/tags", headers=_upstream_headers())
    m = [{"id": x["name"], "object": "model", "owned_by": "ollama"} for x in r.json().get("models", [])]
    return JSONResponse(content={"object": "list", "data": m})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream", False))
    tools = _build_tools(load_mcp_excluded_tools().excluded_tools, _get_tools_from_request(body))

    if not TOOL_CALLING_ENABLED or not tools:
        if stream:
            c = await _http()
            if UPSTREAM_API_STYLE == "openai":
                async def ps():
                    async with c.stream("POST", f"{UPSTREAM_LLM_URL}/v1/chat/completions", headers=_upstream_headers(), 
                                        json=_apply_openai_thinking(body), timeout=120) as r:
                        async for chunk in r.aiter_bytes(): yield chunk
                return StreamingResponse(ps(), media_type="text/event-stream")
            
            async def ops():
                payload = _openai_to_ollama_request(body, force_stream=True)
                async with c.stream("POST", f"{UPSTREAM_LLM_URL}/api/chat", headers=_upstream_headers(), json=payload, timeout=120) as r:
                    rid, created, model = f"chatcmpl-{uuid.uuid4().hex}", int(time.time()), str(body.get("model", "unknown"))
                    yield _sse_line({"id": rid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
                    async for line in r.aiter_lines():
                        if not line.strip(): continue
                        p = json.loads(line)
                        if "message" in p and p["message"].get("content"):
                            yield _sse_line({"id": rid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": p["message"]["content"]}, "finish_reason": None}]})
                        if p.get("done"):
                            yield _sse_line({"id": rid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                            yield _sse_line("[DONE]")
            return StreamingResponse(ops(), media_type="text/event-stream")
        
        # Non-streaming passthrough
        if UPSTREAM_API_STYLE == "openai":
            c = await _http()
            r = await c.post(f"{UPSTREAM_LLM_URL}/v1/chat/completions", headers=_upstream_headers(), json=_apply_openai_thinking(body))
            return JSONResponse(content=r.json())
        else:
            c = await _http()
            r = await c.post(f"{UPSTREAM_LLM_URL}/api/chat", headers=_upstream_headers(), json=_openai_to_ollama_request(body, force_stream=False))
            return JSONResponse(content=_normalize_openai_response_from_ollama(r.json()))

    if stream:
        return StreamingResponse(_resolve_tool_calls_streaming(body, tools), media_type="text/event-stream")
    
    # Non-streaming with tools
    messages = [m for m in body.get("messages", []) if isinstance(m, dict)]
    for _ in range(MAX_TOOL_ROUNDS):
        res = await (await _http()).post(f"{UPSTREAM_LLM_URL}/v1/chat/completions" if UPSTREAM_API_STYLE == "openai" else f"{UPSTREAM_LLM_URL}/api/chat",
                                         headers=_upstream_headers(),
                                         json=_apply_openai_thinking({**body, "messages": messages, "tools": tools, "stream": False}) if UPSTREAM_API_STYLE == "openai" else _openai_to_ollama_request(body, force_stream=False, tools=tools, messages=messages))
        
        parsed = res.json()
        if UPSTREAM_API_STYLE == "ollama": parsed = _normalize_openai_response_from_ollama(parsed)
        
        msg = parsed["choices"][0]["message"]
        messages.append(msg)
        if not msg.get("tool_calls"): return JSONResponse(content=parsed)
        
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            try: args = json.loads(tc["function"]["arguments"])
            except: args = {}
            out = await _tool_result(name, args)
            messages.append({"role": "tool", "name": name, "tool_call_id": tc["id"], "content": json.dumps(out, ensure_ascii=False)})
    
    raise HTTPException(status_code=400, detail="Max rounds reached")


def _normalize_openai_response_from_ollama(o: dict[str, Any]) -> dict[str, Any]:
    rid = f"chatcmpl-{uuid.uuid4().hex}"
    m = _ollama_message_to_openai(o.get("message", {}), response_id=rid)
    return {
        "id": rid, "object": "chat.completion", "created": int(time.time()), "model": o.get("model", "unknown"),
        "choices": [{"index": 0, "message": m, "finish_reason": "tool_calls" if m.get("tool_calls") else "stop"}],
        "usage": {"prompt_tokens": o.get("prompt_eval_count", 0), "completion_tokens": o.get("eval_count", 0), "total_tokens": o.get("prompt_eval_count", 0) + o.get("eval_count", 0)}
    }
