import os
from logging import getLogger
from typing import Any, Literal

from unmute.llm.llm_utils import preprocess_messages_for_llm
from unmute.llm.context_optimizer import (
    truncate_context_to_token_limit,
    estimate_messages_tokens,
)
from unmute.llm.system_prompt import ConstantInstructions, Instructions

ConversationState = Literal["waiting_for_user", "user_speaking", "bot_speaking"]

logger = getLogger(__name__)

MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "4096"))
MAX_TOOLS_TOKENS = int(os.environ.get("MAX_TOOLS_TOKENS", "2048"))


class Chatbot:
    def __init__(self):
        # It's actually a list of ChatCompletionStreamRequestMessagesTypedDict but then
        # it's really difficult to convince Python you're passing in the right type
        self.chat_history: list[dict[Any, Any]] = [
            {"role": "system", "content": ConstantInstructions().make_system_prompt()}
        ]
        self._instructions: Instructions | None = None
        self.tools: list[dict[str, Any]] | None = None
        self.mcp_tools: list[dict[str, Any]] | None = None
        self.tool_choice: str | None = None

    def get_all_tools(self) -> list[dict[str, Any]]:
        """Get all tools (built-in + MCP) merged."""
        all_tools = list(self.tools or [])

        if self.mcp_tools:
            existing_names = set()
            for tool in all_tools:
                if isinstance(tool, dict) and "function" in tool:
                    existing_names.add(tool["function"].get("name"))

            for mcp_tool in self.mcp_tools:
                if mcp_tool.get("function", {}).get("name") not in existing_names:
                    all_tools.append(mcp_tool)

        return all_tools

    def conversation_state(self) -> ConversationState:
        if not self.chat_history:
            return "waiting_for_user"

        last_message = self.chat_history[-1]
        if last_message["role"] == "assistant":
            return "bot_speaking"
        elif last_message["role"] == "user":
            if last_message["content"].strip() != "":
                return "user_speaking"
            else:
                # Or do we want "user_speaking" here?
                return "waiting_for_user"
        elif last_message["role"] == "system":
            return "waiting_for_user"
        elif last_message["role"] == "tool":
            return "bot_speaking"
        else:
            raise RuntimeError(f"Unknown role: {last_message['role']}")

    async def add_chat_message_delta(
        self,
        delta: str,
        role: Literal["user", "assistant"],
        generating_message_i: int | None = None,  # Avoid race conditions
    ) -> bool:
        """Add a partial message to the chat history, adding spaces if necessary.

        Returns:
            True if the message is a new message, False if it is a continuation of
            the last message.
        """
        if (
            generating_message_i is not None
            and len(self.chat_history) > generating_message_i
        ):
            logger.warning(
                f"Tried to add {delta=} {role=} "
                f"but {generating_message_i=} didn't match"
            )
            return False

        if not self.chat_history or self.chat_history[-1]["role"] != role:
            self.chat_history.append({"role": role, "content": delta})
            return True
        else:
            last_message: str = self.chat_history[-1]["content"]

            # Add a space if necessary
            needs_space_left = last_message != "" and not last_message[-1].isspace()
            needs_space_right = delta != "" and not delta[0].isspace()

            if needs_space_left and needs_space_right:
                delta = " " + delta

            self.chat_history[-1]["content"] += delta
            return last_message == ""  # new message if `last_message` was empty

    def preprocessed_messages(self):
        if len(self.chat_history) > 2:
            messages = self.chat_history
        else:
            assert len(self.chat_history) >= 1
            assert self.chat_history[0]["role"] == "system"

            messages = [
                self.chat_history[0],
            #     # Some models, like Gemma, don't like it when there is no user message
            #     # so we add one.
            #    {"role": "user", "content": "Hello!"},
            ]

        messages = preprocess_messages_for_llm(messages)

        current_tokens = estimate_messages_tokens(messages)

        if current_tokens > MAX_CONTEXT_TOKENS:
            all_tools = self.get_all_tools()
            messages, filtered_tools = truncate_context_to_token_limit(
                messages,
                MAX_CONTEXT_TOKENS,
                MAX_TOOLS_TOKENS if all_tools else None,
                all_tools,
            )
            if filtered_tools is not None:
                self._filtered_tools = filtered_tools
        else:
            self._filtered_tools = None

        return messages

    def get_filtered_tools(self):
        """Get tools filtered by context relevance if applicable."""
        return getattr(self, "_filtered_tools", None)

    def set_instructions(self, instructions: Instructions):
        # Note that make_system_prompt() might not be deterministic, so we run it only
        # once and save the result. We still keep self._instructions because it's used
        # to check whether initial instructions have been set.
        self._update_system_prompt(instructions.make_system_prompt())
        self._instructions = instructions

    def _update_system_prompt(self, system_prompt: str):
        self.chat_history[0] = {"role": "system", "content": system_prompt}

    def get_system_prompt(self) -> str:
        assert len(self.chat_history) > 0
        assert self.chat_history[0]["role"] == "system"
        return self.chat_history[0]["content"]

    def get_instructions(self) -> Instructions | None:
        return self._instructions

    def trim_last_interactions(self, num_interactions: int) -> int:
        """Trim last N interactions, where one interaction starts at a user message.

        Keeps the system prompt intact.
        """
        if num_interactions <= 0:
            return 0

        removed = 0
        while removed < num_interactions:
            last_user_index: int | None = None
            for i in range(len(self.chat_history) - 1, 0, -1):
                if self.chat_history[i].get("role") == "user":
                    last_user_index = i
                    break

            if last_user_index is None:
                break

            del self.chat_history[last_user_index:]
            removed += 1

        return removed

    def last_message(self, role: str) -> str | None:
        valid_messages = [
            message
            for message in self.chat_history
            if message["role"] == role
            and message.get("content")
            and message["content"].strip() != ""
        ]
        if valid_messages:
            return valid_messages[-1]["content"]
        else:
            return None
