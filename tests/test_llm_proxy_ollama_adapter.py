import json

from unmute.llm_proxy.main import (
    _normalize_openai_response_from_ollama,
    _openai_to_ollama_messages,
)


def test_openai_to_ollama_messages_keeps_tool_result_name():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": json.dumps({"city": "Paris"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "get_weather",
            "tool_call_id": "call_1",
            "content": '{"temp": 20}',
        },
    ]

    converted = _openai_to_ollama_messages(messages)
    assert converted[0]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert converted[1]["role"] == "tool"
    assert converted[1]["name"] == "get_weather"


def test_normalize_openai_response_from_ollama_tool_calls():
    ollama_resp = {
        "model": "llama3.2",
        "created_at": "2026-03-06T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "get_weather",
                        "arguments": {"city": "Paris"},
                    }
                }
            ],
        },
        "done": True,
    }

    out = _normalize_openai_response_from_ollama(ollama_resp)
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
