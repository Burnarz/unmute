"""Local tool registry for the LLM proxy.

This file is intentionally standalone and easy to edit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast
from urllib.parse import quote_plus

import httpx

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_HTTP_TIMEOUT_SEC = float(os.environ.get("TOOL_HTTP_TIMEOUT_SEC", "4.0"))
_BLAGUES_API_KEY = os.environ.get("BLAGUES_API_KEY", "")
_HOME_ASSISTANT_URL = os.environ.get("HOME_ASSISTANT_URL", "").rstrip("/")
_HOME_ASSISTANT_TOKEN = os.environ.get("HOME_ASSISTANT_TOKEN", "")

_thinking_mode: Literal["off", "on", "low", "medium", "high"] = "off"
_http_client: httpx.AsyncClient | None = None


async def _http_client_instance() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SEC)
    return _http_client


async def _fetch_json(url: str, *, method: str = "GET", json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    client = await _http_client_instance()
    response = await client.request(method=method, url=url, json=json_body)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object response")
    return cast(dict[str, Any], data)


async def _get_weather(args: dict[str, Any]) -> dict[str, Any]:
    latitude_raw = args.get("latitude")
    longitude_raw = args.get("longitude")

    try:
        latitude = float(latitude_raw)
        longitude = float(longitude_raw)
    except (TypeError, ValueError):
        return {"error": "'latitude' and 'longitude' must be numbers"}

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&current=temperature_2m,wind_speed_10m"
        "&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m"
    )

    try:
        data = await _fetch_json(url)
    except Exception as exc:
        return {"error": f"Weather API request failed: {exc}"}

    current = data.get("current")
    if not isinstance(current, dict):
        return {"error": "Weather API response missing 'current'"}

    return cast(dict[str, Any], current)


async def _get_coordinates(args: dict[str, Any]) -> dict[str, Any]:
    city = str(args.get("city", "")).strip()
    if not city:
        return {"error": "Missing 'city'"}

    encoded_city = quote_plus(city)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_city}"

    try:
        data = await _fetch_json(url)
    except Exception as exc:
        return {"error": f"Geocoding API request failed: {exc}"}

    results = data.get("results")
    if not isinstance(results, list) or len(results) == 0:
        return {"error": f"No coordinates found for city '{city}'"}

    first = results[0]
    if not isinstance(first, dict):
        return {"error": "Invalid geocoding API response"}

    return cast(dict[str, Any], first)


async def _get_jokes(_args: dict[str, Any]) -> dict[str, Any]:
    if _BLAGUES_API_KEY.strip() == "":
        return {"error": "BLAGUES_API_KEY is not set"}

    url = "https://www.blagues-api.fr/api/random"
    headers = {
        "Authorization": f"Bearer {_BLAGUES_API_KEY}"
    }
    try:
        client = await _http_client_instance()
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return {"error": "Blagues API returned invalid JSON format"}
    except httpx.HTTPStatusError as exc:
        return {
            "error": (
                "Blagues API request failed with status "
                f"{exc.response.status_code}: {exc.response.text}"
            )
        }
    except Exception as exc:
        return {"error": f"Blagues API request failed: {exc}"}

    joke = data.get("joke")
    answer = data.get("answer")
    if isinstance(joke, str) and isinstance(answer, str):
        return {"joke": joke, "answer": answer}

    return data


async def _home_assistant(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text", "")).strip()
    if not text:
        return {"error": "Missing 'text'"}

    if _HOME_ASSISTANT_URL == "":
        return {"error": "HOME_ASSISTANT_URL is not set"}
    if _HOME_ASSISTANT_TOKEN.strip() == "":
        return {"error": "HOME_ASSISTANT_TOKEN is not set"}

    headers = {
        "Authorization": f"Bearer {_HOME_ASSISTANT_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        client = await _http_client_instance()
        response = await client.post(
            f"{_HOME_ASSISTANT_URL}/api/conversation/process",
            headers=headers,
            json={"text": text, "language": "fr"},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return {"error": "Home Assistant returned invalid JSON format"}
        return data
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            err_json = exc.response.json()
            if isinstance(err_json, dict):
                detail = str(err_json.get("detail", ""))
        except Exception:
            pass

        if detail:
            return {"error": detail}
        return {
            "error": (
                "Home Assistant request failed with status "
                f"{exc.response.status_code}"
            )
        }
    except Exception as exc:
        return {"error": str(exc)}


async def _reset_recent_interactions(args: dict[str, Any]) -> dict[str, Any]:
    interactions = args.get("interactions")
    try:
        count = int(interactions)
    except (TypeError, ValueError):
        return {"error": "'interactions' must be an integer"}

    if count <= 0:
        return {"error": "'interactions' must be >= 1"}

    # The proxy receives full chat history from Unmute on each request, so it cannot
    # mutate persistent memory by itself.
    return {
        "status": "not_supported",
        "message": "Cannot delete persistent history from this proxy layer.",
        "requested_interactions": count,
    }


async def _set_thinking_mode(args: dict[str, Any]) -> dict[str, Any]:
    global _thinking_mode

    mode = str(args.get("mode", "")).strip().lower()
    allowed = {"off", "on", "low", "medium", "high"}
    if mode not in allowed:
        return {"error": "'mode' must be one of: off, on, low, medium, high"}

    _thinking_mode = cast(Literal["off", "on", "low", "medium", "high"], mode)
    return {
        "mode": _thinking_mode,
        "message": "Thinking mode saved in proxy runtime.",
    }


# OpenAI-compatible tool schema.
LOCAL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for provided coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                },
                "required": ["latitude", "longitude"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_coordinates",
            "description": "Get coordinates for a given city name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                },
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_jokes",
            "description": "Fetch a random joke from blagues-api.fr.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        },
    {
        "type": "function",
        "function": {
            "name": "home_assistant",
            "description": "Send a French natural language command directly to Home Assistant.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Commande naturelle en francais pour Home Assistant.",
                    }
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_recent_interactions",
            "description": "Request deletion of recent interactions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "interactions": {
                        "type": "integer",
                        "description": "Number of interactions to remove.",
                    }
                },
                "required": ["interactions"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_thinking_mode",
            "description": "Set runtime thinking mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["off", "on", "low", "medium", "high"],
                    }
                },
                "required": ["mode"],
                "additionalProperties": False,
            },
        },
    },
]

LOCAL_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "get_weather": _get_weather,
    "get_coordinates": _get_coordinates,
    "get_jokes": _get_jokes,
    "home_assistant": _home_assistant,
    "reset_recent_interactions": _reset_recent_interactions,
    "set_thinking_mode": _set_thinking_mode,
}


def get_runtime_settings() -> dict[str, str]:
    """Expose tool-level runtime settings used by the proxy if needed."""
    return {"thinking_mode": _thinking_mode}


def serialize_runtime_settings() -> str:
    return json.dumps(get_runtime_settings(), ensure_ascii=False)
