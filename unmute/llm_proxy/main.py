from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
HUMAN_APPROVAL_BACKEND_URL = os.environ.get(
    "HUMAN_APPROVAL_BACKEND_URL", "http://backend:80"
).rstrip("/")
APPROVAL_REQUIRED_TOOL_NAMES = {
    "create_event",
    "create_calendar_event",
    "delete_event",
    "delete_calendar_event",
}

# Acknowledgement phrases based on tool names
# These are spoken to the user while the tool is executing
TOOL_ACK_PHRASES = {
    "get_weather": "",
    "get_coordinates": "Laisse-moi regarder la météo.",
    "get_jokes": "",
    "mcp__brave-search__brave_web_search": "Je fais une recherche sur le web.",
    "mcp__brave-search__brave_image_search": "Je cherche une image sur le web.",
    "mcp__memory__read_graph": "Je consulte ma mémoire.",
    "mcp__memory__search_nodes": "Je cherche dans mes souvenirs.",
    "mcp__filesystem__list_directory": "Je regarde le contenu du dossier.",
    "home_assistant": "Je m'occupe de ta domotique.",
}
DEFAULT_ACK_PHRASE = ""

app = FastAPI(title="Unmute LLM Proxy")
Instrumentator().instrument(app).expose(app)

_http_client: httpx.AsyncClient | None = None
_mcp_manager: MCPManager | None = None
DEFAULT_CALENDAR_TIMEZONE = os.environ.get(
    "CALENDAR_LOCAL_TIMEZONE",
    os.environ.get("TZ", "Europe/Paris"),
)
_FRENCH_WEEKDAYS = {
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
}


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


def _strip_internal_fields(body: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in body.items()
        if key not in {"unmute_session_id"}
    }


def _tool_leaf_name(tool_name: str) -> str:
    if tool_name.startswith("mcp__"):
        return tool_name.split("__")[-1]
    return tool_name


def _tool_requires_approval(tool_name: str) -> bool:
    return _tool_leaf_name(tool_name) in APPROVAL_REQUIRED_TOOL_NAMES


def _tool_approval_key(tool_name: str, arguments: dict[str, Any]) -> str:
    normalized_args = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return f"{tool_name}:{normalized_args}"


def _calendar_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(DEFAULT_CALENDAR_TIMEZONE)
    except Exception:
        logger.warning("Invalid calendar timezone '%s', falling back to UTC", DEFAULT_CALENDAR_TIMEZONE)
        return ZoneInfo("UTC")


def _now_in_calendar_timezone() -> datetime:
    return datetime.now(_calendar_timezone())


def _normalize_french_text(text: str) -> str:
    replacements = {
        "à": "a",
        "â": "a",
        "é": "e",
        "è": "e",
        "ê": "e",
        "ë": "e",
        "î": "i",
        "ï": "i",
        "ô": "o",
        "ö": "o",
        "ù": "u",
        "û": "u",
        "ü": "u",
        "ç": "c",
        "’": "'",
    }
    normalized = text.lower()
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized


def _latest_user_message_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _extract_message_text(message)
    return ""


def _parse_relative_french_datetime(text: str, *, now: datetime | None = None) -> str | None:
    normalized = _normalize_french_text(text)
    reference = now or _now_in_calendar_timezone()

    time_match = re.search(r"(?:\ba\s*)?(\d{1,2})(?:[:h](\d{2}))?\s*h?\b", normalized)
    hour = 9
    minute = 0
    if time_match is not None:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or "0")

    day_offset: int | None = None
    if "apres-demain" in normalized or "apres demain" in normalized:
        day_offset = 2
    elif "demain" in normalized:
        day_offset = 1
    elif "aujourd'hui" in normalized or "aujourdhui" in normalized:
        day_offset = 0
    else:
        for weekday_name, weekday_idx in _FRENCH_WEEKDAYS.items():
            if weekday_name not in normalized:
                continue
            days_ahead = (weekday_idx - reference.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            if f"{weekday_name} prochain" in normalized or f"prochain {weekday_name}" in normalized:
                days_ahead += 7 if days_ahead < 7 else 0
            day_offset = days_ahead
            break

    if day_offset is None:
        return None

    target = reference + timedelta(days=day_offset)
    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target.isoformat()


def _normalize_create_event_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    if _tool_leaf_name(tool_name) not in {"create_event", "create_calendar_event"}:
        return arguments

    normalized_args = dict(arguments)
    latest_user_text = _latest_user_message_text(messages)
    parsed_start_time = _parse_relative_french_datetime(latest_user_text)

    if parsed_start_time is None:
        description = str(arguments.get("description") or "")
        parsed_start_time = _parse_relative_french_datetime(description)

    if parsed_start_time is not None:
        normalized_args["start_time"] = parsed_start_time

    return normalized_args


def _format_datetime_for_humans(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip() == "":
        return None

    normalized = value.strip().replace("Z", "+00:00")
    if "T" not in normalized and " " in normalized:
        normalized = normalized.replace(" ", "T", 1)

    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return value

    return dt.strftime("%d/%m/%Y a %H:%M")


def _get_calendar_event_summary(event_id: str) -> str | None:
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_path = Path(os.environ.get("GOOGLE_TOKEN_FILE", ""))
        if not token_path.exists():
            return None

        data = json.loads(token_path.read_text())
        creds = Credentials(
            token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            scopes=data.get("scopes", []),
        )
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:
        logger.warning("Failed to resolve calendar event '%s' for approval summary: %s", event_id, exc)
        return None

    calendar_ids = ["primary"]
    shared_calendar_id = os.environ.get("SHARED_CALENDAR_ID", "").strip()
    if shared_calendar_id:
        calendar_ids.append(shared_calendar_id)

    event = None
    for calendar_id in calendar_ids:
        try:
            event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
            break
        except Exception as exc:
            logger.info(
                "Could not resolve event '%s' in calendar '%s': %s",
                event_id,
                calendar_id,
                exc,
            )

    if event is None:
        return None

    title = str(event.get("summary") or "Sans titre")
    start_raw = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
    end_raw = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")

    start = _format_datetime_for_humans(start_raw) or "horaire inconnu"
    end = _format_datetime_for_humans(end_raw)
    if end is not None and end != start:
        return f"'{title}' le {start} jusqu'a {end} (ID: {event_id})"
    return f"'{title}' le {start} (ID: {event_id})"


def _tool_approval_summary(tool_name: str, arguments: dict[str, Any]) -> str:
    leaf_name = _tool_leaf_name(tool_name)

    if leaf_name in {"create_event", "create_calendar_event"}:
        title = str(arguments.get("summary") or arguments.get("title") or "Sans titre")
        start = (
            _format_datetime_for_humans(arguments.get("start_time"))
            or _format_datetime_for_humans(arguments.get("start"))
            or "horaire inconnu"
        )
        duration = arguments.get("duration_minutes")
        duration_suffix = (
            f" pour {duration} min" if isinstance(duration, int | float) else ""
        )
        description = str(arguments.get("description") or "").strip()
        description_suffix = f". Notes: {description}" if description else ""
        return (
            f"Ajouter l'evenement '{title}' le {start}{duration_suffix}"
            f"{description_suffix}."
        )

    if leaf_name in {"delete_event", "delete_calendar_event"}:
        event_id = str(arguments.get("event_id") or arguments.get("id") or "inconnu")
        summary = str(arguments.get("summary") or "").strip()
        if summary:
            return f"Supprimer l'evenement '{summary}' (ID: {event_id})."

        resolved_summary = _get_calendar_event_summary(event_id)
        if resolved_summary is not None:
            return f"Supprimer l'evenement {resolved_summary}."

        return f"Supprimer l'evenement avec l'identifiant {event_id}."

    return f"Autoriser l'execution du tool {tool_name}."


async def _request_tool_approval(
    session_id: str | None, tool_name: str, arguments: dict[str, Any]
) -> bool:
    if not session_id or HUMAN_APPROVAL_BACKEND_URL == "":
        raise RuntimeError(
            f"Missing session id or approval backend URL for guarded tool {tool_name}"
        )

    payload = {
        "session_id": session_id,
        "tool": tool_name,
        "summary": _tool_approval_summary(tool_name, arguments),
        "arguments": arguments,
    }
    response = await (await _http()).post(
        f"{HUMAN_APPROVAL_BACKEND_URL}/v1/tool-approvals/request",
        json=payload,
        timeout=300,
    )
    response.raise_for_status()
    data = response.json()
    return bool(data.get("approved", False))


def _approval_cancelled_result() -> dict[str, Any]:
    return {
        "status": "cancelled",
        "message": (
            "Tool execution cancelled by the user. Do not retry the same action "
            "unless the user explicitly asks again or changes the request."
        ),
        "retry_allowed_without_new_user_confirmation": False,
    }


def _is_approval_cancelled_result(result: dict[str, Any]) -> bool:
    return (
        result.get("status") == "cancelled"
        and result.get("retry_allowed_without_new_user_confirmation") is False
    )


def _build_tools(
    excluded_tools: set[str], incoming_tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    # 1. Collect all candidates
    all_candidates: list[dict[str, Any]] = []
    
    # Add incoming tools from the request
    all_candidates.extend(t for t in incoming_tools if isinstance(t, dict))
    
    # Add local tools
    all_candidates.extend(LOCAL_TOOLS)
    
    # Add MCP tools
    if _mcp_manager is not None:
        all_candidates.extend(_mcp_manager.openai_tools)

    # 2. Filter and deduplicate by name
    dedup: dict[str, dict[str, Any]] = {}
    excluded_count = 0
    
    # Helper to check if a name is excluded (robust check)
    def is_excluded(name: str) -> bool:
        if name in excluded_tools: return True
        # Check if any excluded tool name is a substring or match without underscores
        norm_name = name.replace("_", "").lower()
        for ex in excluded_tools:
            if ex.replace("_", "").lower() == norm_name: return True
        return False

    for tool in all_candidates:
        fn = tool.get("function", {})
        name = fn.get("name")
        if isinstance(name, str):
            if is_excluded(name):
                excluded_count += 1
                continue
            dedup[name] = tool
            
    final_tools = list(dedup.values())
    logger.info("Tools built: %d total, %d excluded, %d remaining. Tools: %s", 
                len(all_candidates), excluded_count, len(final_tools), 
                ", ".join(dedup.keys()))
    return final_tools


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
            pass

        out.append(mapped)
    return out


def _ollama_think_value() -> bool | str:
    mode = _current_thinking_mode()
    if mode == "off":
        return False
    if mode == "on":
        return True
    return mode


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
        "think": _ollama_think_value(),
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
    upstream_body = _strip_internal_fields(body)
    session_id = body.get("unmute_session_id")
    messages: list[dict[str, Any]] = [m for m in body.get("messages", []) if isinstance(m, dict)]
    client = await _http()
    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    created, model = int(time.time()), str(body.get("model", "unknown"))
    role_emitted = False
    global_content_acc: list[str] = []
    
    # Track tools acknowledged in the CURRENT session to avoid repeated "Je cherche..."
    # for the exact same tool in multiple rounds.
    acknowledged_tools: set[str] = set()
    denied_approval_keys: set[str] = set()

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info("Starting tool round %d (sending %d tools to upstream)", round_idx, len(tools))
        if UPSTREAM_API_STYLE == "openai":
            url = f"{UPSTREAM_LLM_URL}/v1/chat/completions"
            payload = _apply_openai_thinking(
                {**upstream_body, "messages": messages, "tools": tools, "stream": True}
            )
        else:
            url = f"{UPSTREAM_LLM_URL}/api/chat"
            payload = _openai_to_ollama_request(
                upstream_body, force_stream=True, tools=tools, messages=messages
            )

        async with client.stream("POST", url, headers=_upstream_headers(), json=payload, timeout=120) as response:
            if response.status_code >= 400:
                err = (await response.aread()).decode("utf-8", errors="replace")
                logger.error("Upstream error: %s", err)
                raise HTTPException(status_code=response.status_code, detail=err)

            current_tcs: dict[int, dict[str, Any]] = {}
            round_content_acc: list[str] = []
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
                    round_content_acc.append(content_piece)
                    global_content_acc.append(content_piece)
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
                        if fn.get("name"): 
                            fragment = fn["name"]
                            current_tcs[idx]["function"]["name"] += fragment
                            full_name = current_tcs[idx]["function"]["name"]
                            
                            # EAGER ACKNOWLEDGEMENT:
                            # We check if we have already acknowledged THIS specific tool CALL (by index)
                            # to avoid repeating the phrase for every chunk of the name.
                            tc_ack_key = f"round_{round_idx}_idx_{idx}"
                            if tc_ack_key not in acknowledged_tools and len(round_content_acc) < 5:
                                # Look for a match in our phrases
                                ack = TOOL_ACK_PHRASES.get(full_name)
                                
                                # If we have a full match or we're starting a tool call
                                # (we wait for at least 3 chars to avoid false positives)
                                if ack or len(full_name) > 3:
                                    final_ack = ack or DEFAULT_ACK_PHRASE
                                    logger.info("Eagerly acknowledging tool: %s (as %s)", full_name, final_ack)
                                    
                                    if not role_emitted:
                                        yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                                         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
                                        role_emitted = True
                                    
                                    # Inject the phrase
                                    yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                                     "choices": [{"index": 0, "delta": {"content": final_ack + " "}, "finish_reason": None}]})
                                    
                                    # NEW: Send a specific event for the UI to display the tool name
                                    if not _tool_requires_approval(full_name):
                                        yield _sse_line(
                                            {
                                                "id": response_id,
                                                "object": "unmute.tool_started",
                                                "tool": full_name,
                                            }
                                        )
                                    
                                    global_content_acc.append(final_ack + " ")
                                    round_content_acc.append(final_ack + " ")
                                    acknowledged_tools.add(tc_ack_key)

                        args = fn.get("arguments", "")
                        if args:
                            current_tcs[idx]["function"]["arguments"] += (json.dumps(args) if isinstance(args, dict) else args)

                if fr:
                    loop_finish_reason = fr
                    if fr == "tool_calls": break

            if current_tcs:
                logger.info("Round %d found %d tool calls. Executing in parallel...", round_idx, len(current_tcs))
                tc_list = [v for k, v in sorted(current_tcs.items())]
                messages.append({"role": "assistant", "content": "".join(round_content_acc), "tool_calls": tc_list})
                
                # Parallel tool execution
                async def run_one_tool(tc):
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        args = {}
                    args = _normalize_create_event_arguments(name, args, messages)

                    if _tool_requires_approval(name):
                        approval_key = _tool_approval_key(name, args)
                        if approval_key in denied_approval_keys:
                            logger.info(
                                "Skipping repeated approval prompt for cancelled tool '%s'",
                                name,
                            )
                            return name, tc.get("id"), _approval_cancelled_result()
                        try:
                            approved = await _request_tool_approval(session_id, name, args)
                        except Exception as exc:
                            logger.exception("Approval flow failed for tool '%s'", name)
                            return name, tc.get("id"), {
                                "error": f"Approval flow failed: {exc}",
                            }
                        if not approved:
                            denied_approval_keys.add(approval_key)
                            logger.info("Tool '%s' cancelled by human approval workflow", name)
                            return name, tc.get("id"), _approval_cancelled_result()

                    logger.info("Executing tool: %s", name)
                    res = await _tool_result(name, args)
                    logger.info("Tool '%s' finished", name)
                    return name, tc.get("id"), res

                parallel_results = await asyncio.gather(*(run_one_tool(tc) for tc in tc_list))
                
                # Signal that tools are done for this round
                yield _sse_line({"id": response_id, "object": "unmute.tool_finished"})
                
                for name, tc_id, res in parallel_results:
                    messages.append({"role": "tool", "name": name, "tool_call_id": tc_id or f"tc-{uuid.uuid4().hex}", 
                                     "content": json.dumps(res, ensure_ascii=False)})
                    
                    if name == "reset_recent_interactions" and res.get("status") == "success":
                        count = res.get("count", 0)
                        to_remove = (count + 1) * 2
                        if len(messages) > to_remove + 1:
                            logger.info("Truncating %d messages from history", to_remove)
                            idx_to_keep = max(1, len(messages) - 2 - to_remove)
                            messages = [messages[0]] + messages[idx_to_keep:]
                        yield _sse_line({"id": response_id, "object": "unmute.reset_history", "count": count + 1})

                if any(_is_approval_cancelled_result(res) for _name, _tc_id, res in parallel_results):
                    if not role_emitted:
                        yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
                    yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                     "choices": [{"index": 0, "delta": {"content": "D'accord, j'annule. "}, "finish_reason": None}]})
                    yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                    yield _sse_line("[DONE]")
                    return
            elif loop_finish_reason == "stop" or not round_content_acc:
                logger.info("Round %d finished with stop", round_idx)
                yield _sse_line({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield _sse_line("[DONE]")
                return
            else:
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
    upstream_body = _strip_internal_fields(body)
    stream = bool(body.get("stream", False))
    excl_config = load_mcp_excluded_tools()
    tools = _build_tools(excl_config.excluded_tools, _get_tools_from_request(body))
    denied_approval_keys: set[str] = set()

    if not TOOL_CALLING_ENABLED or not tools:
        if stream:
            c = await _http()
            if UPSTREAM_API_STYLE == "openai":
                async def ps():
                    async with c.stream("POST", f"{UPSTREAM_LLM_URL}/v1/chat/completions", headers=_upstream_headers(), 
                                        json=_apply_openai_thinking(upstream_body), timeout=120) as r:
                        async for chunk in r.aiter_bytes(): yield chunk
                return StreamingResponse(ps(), media_type="text/event-stream")
            
            async def ops():
                payload = _openai_to_ollama_request(upstream_body, force_stream=True)
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
        
        if UPSTREAM_API_STYLE == "openai":
            c = await _http()
            r = await c.post(f"{UPSTREAM_LLM_URL}/v1/chat/completions", headers=_upstream_headers(), json=_apply_openai_thinking(upstream_body))
            return JSONResponse(content=r.json())
        else:
            c = await _http()
            r = await c.post(f"{UPSTREAM_LLM_URL}/api/chat", headers=_upstream_headers(), json=_openai_to_ollama_request(upstream_body, force_stream=False))
            return JSONResponse(content=_normalize_openai_response_from_ollama(r.json()))

    if stream:
        return StreamingResponse(_resolve_tool_calls_streaming(body, tools), media_type="text/event-stream")
    
    messages = [m for m in body.get("messages", []) if isinstance(m, dict)]
    for _ in range(MAX_TOOL_ROUNDS):
        res = await (await _http()).post(f"{UPSTREAM_LLM_URL}/v1/chat/completions" if UPSTREAM_API_STYLE == "openai" else f"{UPSTREAM_LLM_URL}/api/chat",
                                         headers=_upstream_headers(),
                                         json=_apply_openai_thinking({**upstream_body, "messages": messages, "tools": tools, "stream": False}) if UPSTREAM_API_STYLE == "openai" else _openai_to_ollama_request(upstream_body, force_stream=False, tools=tools, messages=messages))
        
        parsed = res.json()
        if UPSTREAM_API_STYLE == "ollama": parsed = _normalize_openai_response_from_ollama(parsed)
        msg = parsed["choices"][0]["message"]
        messages.append(msg)
        if not msg.get("tool_calls"): return JSONResponse(content=parsed)
        
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            args = _normalize_create_event_arguments(name, args, messages)
            if _tool_requires_approval(name):
                approval_key = _tool_approval_key(name, args)
                if approval_key in denied_approval_keys:
                    out = _approval_cancelled_result()
                    messages.append({"role": "tool", "name": name, "tool_call_id": tc["id"], "content": json.dumps(out, ensure_ascii=False)})
                    final_message = {
                        "role": "assistant",
                        "content": "D'accord, j'annule.",
                    }
                    return JSONResponse(
                        content={
                            "id": f"chatcmpl-{uuid.uuid4().hex}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": body.get("model", "unknown"),
                            "choices": [
                                {
                                    "index": 0,
                                    "message": final_message,
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": parsed.get("usage", {}),
                        }
                    )
                try:
                    approved = await _request_tool_approval(
                        body.get("unmute_session_id"), name, args
                    )
                except Exception as exc:
                    out = {"error": f"Approval flow failed: {exc}"}
                    messages.append({"role": "tool", "name": name, "tool_call_id": tc["id"], "content": json.dumps(out, ensure_ascii=False)})
                    continue
                if not approved:
                    denied_approval_keys.add(approval_key)
                    out = _approval_cancelled_result()
                    messages.append({"role": "tool", "name": name, "tool_call_id": tc["id"], "content": json.dumps(out, ensure_ascii=False)})
                    final_message = {
                        "role": "assistant",
                        "content": "D'accord, j'annule.",
                    }
                    return JSONResponse(
                        content={
                            "id": f"chatcmpl-{uuid.uuid4().hex}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": body.get("model", "unknown"),
                            "choices": [
                                {
                                    "index": 0,
                                    "message": final_message,
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": parsed.get("usage", {}),
                        }
                    )
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
