import os
import sys
import types
from unittest.mock import patch

os.environ.setdefault("GOOGLE_TOKEN_FILE", "/tmp/fake-token.json")

sys.modules.setdefault(
    "mcp.server.fastmcp",
    types.SimpleNamespace(FastMCP=lambda *args, **kwargs: types.SimpleNamespace(tool=lambda: (lambda fn: fn))),
)
sys.modules.setdefault(
    "google.oauth2.credentials",
    types.SimpleNamespace(Credentials=lambda *args, **kwargs: object()),
)
sys.modules.setdefault(
    "googleapiclient.discovery",
    types.SimpleNamespace(build=lambda *args, **kwargs: object()),
)

from unmute.mcp_google_workspace import (
    delete_calendar_event,
    get_calendar_event_details,
    get_daily_agenda,
)


def test_delete_calendar_event_tries_shared_calendar_after_primary():
    calls = []

    class FakeEvents:
        def delete(self, calendarId: str, eventId: str):
            calls.append((calendarId, eventId))

            class Execute:
                def execute(inner_self):
                    if calendarId == "primary":
                        raise Exception("not found")
                    return None

            return Execute()

    class FakeService:
        def events(self):
            return FakeEvents()

    with (
        patch.dict(os.environ, {"SHARED_CALENDAR_ID": "shared"}, clear=False),
        patch("unmute.mcp_google_workspace.get_google_credentials", return_value=object()),
        patch("unmute.mcp_google_workspace.build", return_value=FakeService()),
    ):
        result = delete_calendar_event("abc123")

    assert calls == [("primary", "abc123"), ("shared", "abc123")]
    assert result == "Événement abc123 supprimé avec succès."


def test_get_calendar_event_details_tries_shared_calendar_after_primary():
    calls = []

    class FakeEvents:
        def get(self, calendarId: str, eventId: str):
            calls.append((calendarId, eventId))

            class Execute:
                def execute(inner_self):
                    if calendarId == "primary":
                        raise Exception("not found")
                    return {
                        "summary": "Dentiste",
                        "start": {"dateTime": "2026-03-18T15:00:00+00:00"},
                        "end": {"dateTime": "2026-03-18T15:30:00+00:00"},
                    }

            return Execute()

    class FakeService:
        def events(self):
            return FakeEvents()

    with (
        patch.dict(os.environ, {"SHARED_CALENDAR_ID": "shared"}, clear=False),
        patch("unmute.mcp_google_workspace.get_google_credentials", return_value=object()),
        patch("unmute.mcp_google_workspace.build", return_value=FakeService()),
    ):
        result = get_calendar_event_details("abc123")

    assert calls == [("primary", "abc123"), ("shared", "abc123")]
    assert result == {
        "event_id": "abc123",
        "summary": "Dentiste",
        "start": "2026-03-18T15:00:00+00:00",
        "end": "2026-03-18T15:30:00+00:00",
        "calendar_id": "shared",
        "calendar_name": "Calendrier partagé",
    }


def test_get_daily_agenda_includes_event_ids():
    class FakeEventsList:
        def execute(self):
            return {
                "items": [
                    {
                        "id": "abc123",
                        "summary": "Dentiste",
                        "start": {"dateTime": "2026-03-18T15:00:00+00:00"},
                    }
                ]
            }

    class FakeEvents:
        def list(self, **kwargs):
            return FakeEventsList()

    class FakeService:
        def events(self):
            return FakeEvents()

    with (
        patch("unmute.mcp_google_workspace.get_google_credentials", return_value=object()),
        patch("unmute.mcp_google_workspace.build", return_value=FakeService()),
    ):
        result = get_daily_agenda("2026-03-18")

    assert "Dentiste" in result
    assert "ID: abc123" in result
