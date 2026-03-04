import pytest

from unmute.llm.llm_utils import (
    _convert_ollama_chunk_to_delta,
    _to_ollama_tools,
    _to_ollama_messages,
    rechunk_to_words,
)


async def make_iterator(s: str):
    parts = s.split("|")
    for part in parts:
        yield part


@pytest.mark.asyncio
async def test_rechunk_to_words():
    test_strings = [
        "hel|lo| |w|orld",
        "hello world",
        "hello \nworld",
        "hello| |world",
        "hello| |world|.",
        "h|e|l|l|o| |\tw|o|r|l|d|.",
        "h|e|l|l|o\n| |w|o|r|l|d|.",
    ]

    for s in test_strings:
        parts = [x async for x in rechunk_to_words(make_iterator(s))]
        assert parts[0] == "hello"
        assert parts[1] == " world" or parts[1] == " world."

    async def f(s: str):
        x = [x async for x in rechunk_to_words(make_iterator(s))]
        print(x)
        return x

    assert await f("i am ok") == ["i", " am", " ok"]
    assert await f(" i am ok") == [" i", " am", " ok"]
    assert await f(" they are ok") == [" they", " are", " ok"]
    assert await f("  foo bar") == [" foo", " bar"]
    assert await f(" \t foo  bar") == [" foo", " bar"]


def test_convert_ollama_chunk_to_delta_tool_call():
    chunk = {
        "message": {
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "get_weather",
                        "arguments": {"city": "Paris"},
                    }
                }
            ],
        }
    }

    delta = _convert_ollama_chunk_to_delta(chunk)
    assert delta.content == ""
    assert delta.tool_calls is not None
    assert len(delta.tool_calls) == 1
    assert delta.tool_calls[0].function.name == "get_weather"
    assert delta.tool_calls[0].function.arguments == '{"city": "Paris"}'


def test_to_ollama_messages_parses_json_tool_args():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"query":"kyutai"}',
                    },
                }
            ],
        }
    ]

    converted = _to_ollama_messages(messages)
    assert converted[0]["tool_calls"][0]["function"]["arguments"] == {
        "query": "kyutai"
    }


def test_to_ollama_tools_accepts_model_dump_objects():
    class ToolLike:
        def model_dump(self):
            return {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }

    converted = _to_ollama_tools([ToolLike()])
    assert len(converted) == 1
    assert converted[0]["function"]["name"] == "weather"
