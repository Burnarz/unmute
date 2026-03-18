import asyncio
import json
from unittest.mock import AsyncMock, patch

from unmute.llm_proxy.main import _resolve_tool_calls_streaming


class FakeStreamingResponse:
    def __init__(self, lines: list[str], *, status_code: int = 200):
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class FakeStreamingClient:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.last_payload: dict[str, object] | None = None

    def stream(self, method: str, url: str, *, headers: dict[str, str], json: dict[str, object], timeout: int):
        self.last_payload = json
        return FakeStreamingResponse(self.lines)


async def _collect_stream(body: dict[str, object], tools: list[dict[str, object]], lines: list[str]) -> list[str]:
    client = FakeStreamingClient(lines)

    async def fake_http():
        return client

    with patch("unmute.llm_proxy.main._http", new=fake_http):
        return [
            chunk.decode("utf-8")
            async for chunk in _resolve_tool_calls_streaming(body, tools)
        ]


def test_resolve_tool_calls_streaming_forwards_content_until_stop():
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Salut"}],
        "stream": True,
    }
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]
    lines = [
        'data: {"choices":[{"delta":{"content":"Bonjour"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    ]

    chunks = asyncio.run(_collect_stream(body, tools, lines))

    payload = "".join(chunks)
    assert '"role": "assistant"' in payload
    assert '"content": "Bonjour"' in payload
    assert "data: [DONE]" in payload


def test_resolve_tool_calls_streaming_stops_after_denied_approval():
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Supprime le rendez-vous"}],
        "stream": True,
        "unmute_session_id": "session_123",
    }
    tools = [{"type": "function", "function": {"name": "delete_calendar_event", "parameters": {"type": "object", "properties": {}}}}]
    lines = [
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "delete_calendar_event",
                                        "arguments": '{"event_id":"abc123"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
    ]

    with (
        patch("unmute.llm_proxy.main._request_tool_approval", new=AsyncMock(return_value=False)),
        patch("unmute.llm_proxy.main._tool_result", new=AsyncMock()),
    ):
        chunks = asyncio.run(_collect_stream(body, tools, lines))

    payload = "".join(chunks)
    assert "D'accord, j'annule." in payload
    assert "data: [DONE]" in payload
