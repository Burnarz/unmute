import os
import re
from copy import deepcopy
from functools import cache
from typing import Any, AsyncIterator, Protocol, cast

from mistralai import Mistral
from openai import AsyncOpenAI, OpenAI
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

from unmute.kyutai_constants import LLM_SERVER

from ..kyutai_constants import KYUTAI_LLM_API_KEY, KYUTAI_LLM_MODEL

INTERRUPTION_CHAR = "—"  # em-dash
USER_SILENCE_MARKER = "..."


def preprocess_messages_for_llm(
    chat_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []

    for message in chat_history:
        message = deepcopy(message)

        # Sometimes, an interruption happens before the LLM can say anything at all.
        # In that case, we're left with a message with only INTERRUPTION_CHAR.
        # Simplify by removing.
        if (
            isinstance(message.get("content"), str)
            and message["content"].replace(INTERRUPTION_CHAR, "") == ""
        ):
            continue

        # If the llm was interrupted we don't want to insert the INTERRUPTION_CHAR
        # into the context, otherwise the LLM might want to repeat it.
        if isinstance(message.get("content"), str):
            message["content"] = message["content"].strip().removesuffix(INTERRUPTION_CHAR)

        if (
            output
            and message["role"] == output[-1]["role"]
            and isinstance(message.get("content"), str)
            and isinstance(output[-1].get("content"), str)
            and message.get("tool_calls") is None
            and output[-1].get("tool_calls") is None
        ):
            output[-1]["content"] += " " + message["content"]
        else:
            output.append(message)

    def role_at(index: int) -> str | None:
        if index >= len(output):
            return None
        return output[index]["role"]

    if role_at(0) == "system" and role_at(1) in [None, "assistant"]:
        # Some LLMs, like Gemma, get confused if the assistant message goes before user
        # messages, so add a dummy user message.
        output = [output[0]] + [{"role": "user", "content": "Hello."}] + output[1:]

    for message in chat_history:
        if (
            message["role"] == "user"
            and isinstance(message.get("content"), str)
            and message["content"].startswith(USER_SILENCE_MARKER)
            and message["content"] != USER_SILENCE_MARKER
        ):
            # This happens when the user is silent but then starts talking again after
            # the silence marker was inserted but before the LLM could respond.
            # There are special instructions in the system prompt about how to handle
            # the silence marker, so remove the marker from the message to not confuse
            # the LLM
            message["content"] = message["content"][len(USER_SILENCE_MARKER) :]

    return output

async def rechunk_to_words(iterator: AsyncIterator[str]) -> AsyncIterator[str]:
    """Rechunk the stream of text to whole words.

    Otherwise the TTS doesn't know where word boundaries are and will mispronounce
    split words.

    The spaces will be included with the next word, so "foo bar baz" will be split into
    "foo", " bar", " baz".
    Multiple space-like characters will be merged to a single space.
    """
    buffer = ""
    space_re = re.compile(r"\s+")
    prefix = ""
    async for delta in iterator:
        buffer = buffer + delta
        while True:
            match = space_re.search(buffer)
            if match is None:
                break
            chunk = buffer[: match.start()]
            buffer = buffer[match.end() :]
            if chunk != "":
                yield prefix + chunk
            prefix = " "

    if buffer != "":
        yield prefix + buffer


async def rechunk_to_words_and_functions(
    iterator: AsyncIterator[Any],
) -> AsyncIterator[dict[str, Any]]:
    """Rechunk the stream of LLM deltas to words or tool calls.

    See [rechunk_to_words] for more details on how words are handled.

    Words are yielded as {"word": "word"}.
    Tool calls are yielded as {"function": {"id": "...", "name": "...", "arguments": "..."}}.
    """
    buffer = ""
    space_re = re.compile(r"\s+")
    prefix = ""
    tools: dict[int, ChatCompletionMessageToolCall] = {}

    async for delta in iterator:
        if not delta:
            continue
        if delta.tool_calls:
            for tool_call_chunk in delta.tool_calls:
                index = tool_call_chunk.index
                if index not in tools:
                    tools[index] = ChatCompletionMessageToolCall(
                        id=tool_call_chunk.id or "",
                        function=Function(
                            name=tool_call_chunk.function.name or "",
                            arguments=tool_call_chunk.function.arguments or "",
                        ),
                        type="function",
                    )
                else:
                    tool = tools[index]
                    if tool_call_chunk.id:
                        tool.id = tool_call_chunk.id
                    if tool_call_chunk.function:
                        if tool_call_chunk.function.name:
                            tool.function.name = tool_call_chunk.function.name
                        if tool_call_chunk.function.arguments:
                            tool.function.arguments += (
                                tool_call_chunk.function.arguments
                            )
                yield {"function": tools[index]}
        if delta.content:
            buffer = buffer + delta.content
            while True:
                match = space_re.search(buffer)
                if match is None:
                    break
                chunk = buffer[: match.start()]
                buffer = buffer[match.end() :]
                if chunk != "":
                    yield {"word": prefix + chunk}
                prefix = " "

    if buffer != "":
        yield {"word": prefix + buffer}


class LLMStream(Protocol):
    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[Any]:
        """Get a chat completion from the LLM."""
        ...


class MistralStream:
    def __init__(self):
        self.current_message_index = 0
        self.mistral = Mistral(api_key=os.environ["MISTRAL_API_KEY"])

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[Any]:
        if tools:
            raise NotImplementedError("MistralStream does not support tool calling yet.")
        event_stream = await self.mistral.chat.stream_async(
            model="mistral-large-latest",
            messages=cast(Any, messages),  # It's too annoying to type this properly
            temperature=1.0,
        )

        async for event in event_stream:
            yield event.data.choices[0].delta


def get_openai_client(
    server_url: str = LLM_SERVER, api_key: str | None = KYUTAI_LLM_API_KEY
) -> AsyncOpenAI:
    # AsyncOpenAI() will complain if the API key is not set, so set a dummy string if it's None.
    # This still makes sense when using vLLM because it doesn't care about the API key.
    return AsyncOpenAI(api_key=api_key or "EMPTY", base_url=server_url + "/v1")


@cache
def autoselect_model() -> str:
    if KYUTAI_LLM_MODEL is not None:
        return KYUTAI_LLM_MODEL
    openai_client = get_openai_client()
    # OpenAI() will complain if the API key is not set, so set a dummy string if it's None.
    # This still makes sense when using vLLM because it doesn't care about the API key.
    client_sync = OpenAI(
        api_key=openai_client.api_key or "EMPTY", base_url=openai_client.base_url
    )
    models = client_sync.models.list()
    if len(models.data) != 1:
        raise ValueError("There are multiple models available. Please specify one.")
    return models.data[0].id


class VLLMStream:
    def __init__(
        self,
        client: AsyncOpenAI,
        temperature: float = 1.0,
        extra_body: dict[str, Any] | None = None,
    ):
        """
        If `model` is None, it will look at the available models, and if there is only
        one model, it will use that one. Otherwise, it will raise.
        """
        self.client = client
        self.model = autoselect_model()
        self.temperature = temperature
        self.extra_body = extra_body or {}

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[Any]:
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": cast(Any, messages),  # Cast and hope for the best
            "stream": True,
            "temperature": self.temperature,
            "extra_body": self.extra_body,
        }
        if tools:
            create_kwargs["tools"] = tools
        if tool_choice:
            create_kwargs["tool_choice"] = tool_choice

        stream = await self.client.chat.completions.create(**create_kwargs)

        async with stream:
            async for chunk in stream:
                yield chunk.choices[0].delta
