import asyncio
from unittest.mock import patch

import pytest

import unmute.openai_realtime_api_events as ora
from unmute.unmute_handler import UnmuteHandler


class _FakeTTS:
    async def send(self, _message):
        return None


class _FakeQuest:
    async def get(self):
        return _FakeTTS()


class _FakeVLLMStream:
    def __init__(self, *args, **kwargs):
        pass

    async def chat_completion(self, _messages):
        yield {
            "object": "unmute.tool_started",
            "tool": "mcp__brave-search__brave_web_search",
            "ack_text": "Je fais une recherche sur le web.",
        }
        yield "Bonjour"


@pytest.mark.asyncio
async def test_tool_acknowledgement_does_not_block_llm_stream():
    ack_started = asyncio.Event()
    release_ack = asyncio.Event()

    async def fake_start_up_tts(_generating_message_i: int):
        return _FakeQuest()

    async def fake_speak_tool_acknowledgement(_ack_text: str, _tool_name: str) -> None:
        ack_started.set()
        await release_ack.wait()

    with (
        patch("unmute.llm.system_prompt.autoselect_model", return_value="test-model"),
        patch("unmute.unmute_handler.VLLMStream", _FakeVLLMStream),
        patch("unmute.llm.llm_utils.autoselect_model", return_value="test-model"),
    ):
        handler = UnmuteHandler()
        handler.chatbot.chat_history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Salut"},
            {"role": "assistant", "content": ""},
        ]

        with (
            patch.object(handler, "start_up_tts", side_effect=fake_start_up_tts),
            patch.object(
                handler,
                "_speak_tool_acknowledgement",
                side_effect=fake_speak_tool_acknowledgement,
            ),
        ):
            task = asyncio.create_task(handler._generate_response_task())

            await asyncio.wait_for(ack_started.wait(), timeout=0.2)

            seen_delta = None
            for _ in range(4):
                event = await asyncio.wait_for(handler.output_queue.get(), timeout=0.2)
                if isinstance(event, ora.UnmuteResponseTextDeltaReady):
                    seen_delta = event
                    break

            assert seen_delta is not None
            assert seen_delta.delta == "Bonjour"
            assert handler.tool_ack_task is not None
            assert not handler.tool_ack_task.done()

            release_ack.set()
            await asyncio.wait_for(task, timeout=0.2)
            await handler.cleanup()
