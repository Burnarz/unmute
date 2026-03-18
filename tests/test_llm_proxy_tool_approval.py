from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio
from unittest.mock import patch

from unmute.llm_proxy.main import (
    _approval_cancelled_result,
    _with_preloaded_memory,
    _normalize_create_event_arguments,
    _parse_relative_french_datetime,
    _tool_approval_key,
    _strip_internal_fields,
    _tool_approval_summary,
    _tool_requires_approval,
)


def test_strip_internal_fields_removes_session_id():
    body = {"messages": [], "stream": True, "unmute_session_id": "session_123"}

    assert _strip_internal_fields(body) == {"messages": [], "stream": True}


def test_tool_requires_approval_for_calendar_tools():
    assert _tool_requires_approval("create_event")
    assert _tool_requires_approval("delete_calendar_event")
    assert _tool_requires_approval("mcp__google-workspace__create_calendar_event")
    assert not _tool_requires_approval("get_weather")


def test_tool_approval_key_is_stable_for_same_args_order():
    key_a = _tool_approval_key(
        "create_calendar_event",
        {"summary": "Fleuriste", "duration_minutes": 45},
    )
    key_b = _tool_approval_key(
        "create_calendar_event",
        {"duration_minutes": 45, "summary": "Fleuriste"},
    )

    assert key_a == key_b


def test_parse_relative_french_datetime_for_demain():
    parsed = _parse_relative_french_datetime(
        "Ajoute un rdv fleuriste demain a 15h",
        now=datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Europe/Paris")),
    )

    assert parsed == "2026-03-19T15:00:00+01:00"


def test_parse_relative_french_datetime_for_vendredi():
    parsed = _parse_relative_french_datetime(
        "Ajoute un rdv fleuriste vendredi a 15h",
        now=datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Europe/Paris")),
    )

    assert parsed == "2026-03-20T15:00:00+01:00"


def test_normalize_create_event_arguments_prefers_latest_user_message():
    args = {
        "summary": "Fleuriste",
        "start_time": "2026-03-18T15:00:00+01:00",
        "description": "vendredi a 15h",
    }
    messages = [
        {"role": "user", "content": "Ajoute fleuriste vendredi a 15h"},
    ]

    with patch(
        "unmute.llm_proxy.main._now_in_calendar_timezone",
        return_value=datetime(2026, 3, 18, 10, 0, tzinfo=ZoneInfo("Europe/Paris")),
    ):
        normalized = _normalize_create_event_arguments(
            "create_calendar_event",
            args,
            messages,
        )

    assert normalized["start_time"] == "2026-03-20T15:00:00+01:00"


def test_tool_approval_summary_for_create_calendar_event():
    summary = asyncio.run(
        _tool_approval_summary(
            "create_calendar_event",
            {
                "summary": "Fleuriste",
                "start_time": "2026-03-18T15:00:00+00:00",
                "duration_minutes": 45,
                "description": "Commander un bouquet",
            },
        )
    )

    assert "Fleuriste" in summary
    assert "18/03/2026 a 15:00" in summary
    assert "45 min" in summary
    assert "Commander un bouquet" in summary


def test_tool_approval_summary_for_delete_calendar_event():
    summary = asyncio.run(
        _tool_approval_summary(
            "delete_calendar_event",
            {"event_id": "abc123", "summary": "Dentiste"},
        )
    )

    assert summary == "Supprimer l'evenement 'Dentiste' (ID: abc123)."


def test_tool_approval_summary_for_delete_calendar_event_resolves_event_details():
    with patch(
        "unmute.llm_proxy.main._get_calendar_event_summary",
        return_value="'Dentiste' le 18/03/2026 a 15:00 (ID: abc123)",
    ):
        summary = asyncio.run(
            _tool_approval_summary(
                "delete_calendar_event",
                {"event_id": "abc123"},
            )
        )

    assert summary == "Supprimer l'evenement 'Dentiste' le 18/03/2026 a 15:00 (ID: abc123)."


def test_tool_approval_summary_for_delete_calendar_event_requires_resolved_details():
    with patch(
        "unmute.llm_proxy.main._get_calendar_event_summary",
        return_value=None,
    ):
        summary = asyncio.run(
            _tool_approval_summary(
                "delete_calendar_event",
                {"event_id": "abc123"},
            )
        )

    assert summary == "Supprimer l'evenement avec l'identifiant abc123."


def test_get_calendar_event_summary_tries_shared_calendar_after_primary():
    class FakeMCPManager:
        async def call(self, openai_tool_name: str, arguments: dict[str, str]) -> dict[str, str]:
            assert openai_tool_name == "mcp__google-workspace__get_calendar_event_details"
            assert arguments == {"event_id": "abc123"}
            return {
                "summary": "Dentiste",
                "start": "2026-03-18T15:00:00+00:00",
                "end": "2026-03-18T15:30:00+00:00",
            }

    with patch("unmute.llm_proxy.main._mcp_manager", new=FakeMCPManager()):
        from unmute.llm_proxy.main import _get_calendar_event_summary

        summary = asyncio.run(_get_calendar_event_summary("abc123"))

    assert summary == "'Dentiste' le 18/03/2026 a 15:00 jusqu'a 18/03/2026 a 15:30 (ID: abc123)"


def test_get_calendar_event_summary_reads_mcp_structured_content():
    class FakeMCPManager:
        async def call(self, openai_tool_name: str, arguments: dict[str, str]) -> dict[str, object]:
            assert openai_tool_name == "mcp__google-workspace__get_calendar_event_details"
            assert arguments == {"event_id": "abc123"}
            return {
                "structuredContent": {
                    "summary": "Dentiste",
                    "start": "2026-03-18T15:00:00+00:00",
                    "end": "2026-03-18T15:30:00+00:00",
                }
            }

    with patch("unmute.llm_proxy.main._mcp_manager", new=FakeMCPManager()):
        from unmute.llm_proxy.main import _get_calendar_event_summary

        summary = asyncio.run(_get_calendar_event_summary("abc123"))

    assert summary == "'Dentiste' le 18/03/2026 a 15:00 jusqu'a 18/03/2026 a 15:30 (ID: abc123)"


def test_get_calendar_event_summary_reads_mcp_content_json():
    class FakeMCPManager:
        async def call(self, openai_tool_name: str, arguments: dict[str, str]) -> dict[str, object]:
            assert openai_tool_name == "mcp__google-workspace__get_calendar_event_details"
            assert arguments == {"event_id": "abc123"}
            return {
                "content": [
                    {
                        "type": "text",
                        "text": '{"summary":"Dentiste","start":"2026-03-18T15:00:00+00:00","end":"2026-03-18T15:30:00+00:00"}',
                    }
                ]
            }

    with patch("unmute.llm_proxy.main._mcp_manager", new=FakeMCPManager()):
        from unmute.llm_proxy.main import _get_calendar_event_summary

        summary = asyncio.run(_get_calendar_event_summary("abc123"))

    assert summary == "'Dentiste' le 18/03/2026 a 15:00 jusqu'a 18/03/2026 a 15:30 (ID: abc123)"


def test_approval_cancelled_result_tells_model_not_to_retry():
    result = _approval_cancelled_result()

    assert result["status"] == "cancelled"
    assert "Do not retry the same action" in result["message"]
    assert result["retry_allowed_without_new_user_confirmation"] is False


def test_with_preloaded_memory_injects_read_graph_before_fake_hello():
    class FakeMCPManager:
        openai_tools = [
            {"function": {"name": "mcp__memory__read_graph"}}
        ]

    async def fake_tool_result(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        assert tool_name == "mcp__memory__read_graph"
        assert arguments == {}
        return {"graph": [{"name": "default_user"}]}

    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Hello!"},
    ]

    with (
        patch("unmute.llm_proxy.main._mcp_manager", new=FakeMCPManager()),
        patch("unmute.llm_proxy.main._tool_result", new=fake_tool_result),
    ):
        enriched = asyncio.run(_with_preloaded_memory(messages))

    assert enriched[0]["role"] == "system"
    assert enriched[1]["role"] == "assistant"
    assert enriched[1]["tool_calls"][0]["function"]["name"] == "mcp__memory__read_graph"
    assert enriched[2]["role"] == "tool"
    assert "default_user" in enriched[2]["content"]
    assert enriched[3] == {"role": "user", "content": "Hello!"}
