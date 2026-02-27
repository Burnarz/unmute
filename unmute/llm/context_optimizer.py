import re
from logging import getLogger
from typing import Any

logger = getLogger(__name__)

TOKEN_ESTIMATE_RATIO = 4  # Rough estimate: 1 token ≈ 4 characters


def estimate_tokens(text: str) -> int:
    """Estimate token count from text.

    Uses a rough ratio of 4 characters per token.
    More accurate implementations would use tiktoken or similar.
    """
    if not text:
        return 0
    return len(text) // TOKEN_ESTIMATE_RATIO


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate tokens for a single message including role and content."""
    tokens = 4  # Base overhead per message

    if "role" in message:
        tokens += estimate_tokens(message["role"])

    content = message.get("content")
    if content:
        tokens += estimate_tokens(content)

    if "tool_calls" in message:
        for tool_call in message["tool_calls"]:
            if isinstance(tool_call, dict):
                func = tool_call.get("function", {})
                tokens += estimate_tokens(func.get("name", ""))
                tokens += estimate_tokens(func.get("arguments", ""))

    if "tool_call_id" in message:
        tokens += estimate_tokens(message["tool_call_id"])

    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens for a list of messages."""
    return sum(estimate_message_tokens(m) for m in messages)


def count_tools_tokens(tools: list[dict[str, Any]]) -> int:
    """Estimate tokens for tools definition."""
    import json

    tokens = 0
    for tool in tools:
        tool_dict = tool.model_dump() if hasattr(tool, "model_dump") else tool
        tokens += estimate_tokens(json.dumps(tool_dict))
    return tokens


def get_relevant_tools_for_context(
    available_tools: list[dict[str, Any]],
    recent_messages: list[dict[str, Any]],
    max_tools_tokens: int,
) -> list[dict[str, Any]]:
    """Filter tools based on conversation context.

    This is a simple heuristic that:
    1. Looks for keywords in recent messages
    2. Selects tools whose descriptions/names match those keywords

    More advanced implementations could use embeddings or LLM-based selection.
    """
    if not available_tools or not recent_messages:
        return available_tools

    recent_text = ""
    for msg in recent_messages[-4:]:  # Look at last 4 messages
        content = msg.get("content", "")
        if content:
            recent_text += " " + content.lower()

    recent_text = recent_text.strip()
    if not recent_text:
        return available_tools

    relevant_tools = []
    irrelevant_tools = []

    tool_keywords = {
        "calendar": [
            "calendar",
            "meeting",
            "schedule",
            "event",
            "rdv",
            "réunion",
            "disponibil",
        ],
        "gmail": ["email", "mail", "gmail", "message", "envoyer", "read", "inbox"],
        "file": [
            "file",
            "folder",
            "directory",
            "read",
            "write",
            "create",
            "delete",
            "fichier",
            "dossier",
        ],
        "fetch": ["fetch", "web", "url", "http", "download", "récupérer", "site"],
        "search": ["search", "google", "查找", "搜索"],
    }

    for tool in available_tools:
        tool_dict = tool.model_dump() if hasattr(tool, "model_dump") else tool
        tool_name = tool_dict.get("function", {}).get("name", "").lower()
        tool_desc = tool_dict.get("function", {}).get("description", "").lower()

        is_relevant = False
        for keyword_category, keywords in tool_keywords.items():
            if any(kw in tool_name or kw in tool_desc for kw in keywords):
                is_relevant = True
                break

        if is_relevant:
            relevant_tools.append(tool)
        else:
            irrelevant_tools.append(tool)

    estimated_relevant_tokens = count_tools_tokens(relevant_tools)

    if estimated_relevant_tokens <= max_tools_tokens:
        logger.debug(
            f"Using {len(relevant_tools)} relevant tools ({estimated_relevant_tokens} tokens), "
            f"filtered out {len(irrelevant_tools)} tools"
        )
        return relevant_tools

    logger.debug(
        f"All tools ({count_tools_tokens(available_tools)} tokens) fit in context, "
        f"using all {len(available_tools)}"
    )
    return available_tools


def truncate_context_to_token_limit(
    messages: list[dict[str, Any]],
    max_context_tokens: int,
    max_tools_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Truncate messages to fit within token limit using sliding window.

    Returns:
        Tuple of (truncated_messages, remaining_tools or None)
    """
    if not messages:
        return [], tools

    system_prompt = messages[0] if messages[0].get("role") == "system" else None
    non_system_messages = messages[1:] if system_prompt else messages

    system_tokens = estimate_message_tokens(system_prompt) if system_prompt else 0

    available_tools = tools
    if max_tools_tokens and tools:
        tools_tokens = count_tools_tokens(tools)
        if tools_tokens > max_tools_tokens:
            available_tools = get_relevant_tools_for_context(
                tools, non_system_messages, max_tools_tokens
            )

    tools_tokens = count_tools_tokens(available_tools) if available_tools else 0

    max_history_tokens = max_context_tokens - system_tokens - tools_tokens - 100

    if max_history_tokens < 0:
        max_history_tokens = max_context_tokens // 2

    current_tokens = 0
    truncated_messages = []

    for msg in reversed(non_system_messages):
        msg_tokens = estimate_message_tokens(msg)

        if current_tokens + msg_tokens <= max_history_tokens:
            truncated_messages.insert(0, msg)
            current_tokens += msg_tokens
        else:
            break

    final_messages = []
    if system_prompt:
        final_messages.append(system_prompt)

    if truncated_messages:
        if truncated_messages[0].get("role") != "user":
            for msg in reversed(non_system_messages):
                if msg.get("role") == "user" and msg not in truncated_messages:
                    truncated_messages.insert(0, msg)
                    break

    final_messages.extend(truncated_messages)

    original_count = len(non_system_messages)
    truncated_count = len(truncated_messages)

    if original_count > truncated_count:
        logger.info(
            f"Context truncated: {original_count} -> {truncated_count} messages "
            f"(~{current_tokens} tokens, limit: {max_history_tokens})"
        )

    return final_messages, available_tools
