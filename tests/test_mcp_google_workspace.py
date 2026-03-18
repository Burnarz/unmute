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
    create_calendar_event,
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


def test_get_daily_agenda_includes_shared_calendar_events_and_ids():
    calls = []

    class FakeEventsList:
        def __init__(self, calendar_id: str):
            self.calendar_id = calendar_id

        def execute(self):
            if self.calendar_id == "primary":
                return {
                    "items": [
                        {
                            "id": "abc123",
                            "summary": "Dentiste",
                            "start": {"dateTime": "2026-03-18T15:00:00+00:00"},
                        }
                    ]
                }
            return {
                "items": [
                    {
                        "id": "shared456",
                        "summary": "Réunion équipe",
                        "start": {"dateTime": "2026-03-18T16:00:00+00:00"},
                    }
                ]
            }

    class FakeEvents:
        def list(self, **kwargs):
            calls.append(kwargs["calendarId"])
            return FakeEventsList(kwargs["calendarId"])

    class FakeService:
        def events(self):
            return FakeEvents()

    with (
        patch.dict(os.environ, {"SHARED_CALENDAR_ID": "shared"}, clear=False),
        patch("unmute.mcp_google_workspace.get_google_credentials", return_value=object()),
        patch("unmute.mcp_google_workspace.build", return_value=FakeService()),
    ):
        result = get_daily_agenda("2026-03-18")

    assert calls == ["primary", "shared"]
    assert "Dentiste" in result
    assert "ID: abc123" in result
    assert "Réunion équipe" in result
    assert "ID: shared456" in result


def test_create_calendar_event_uses_configured_calendar_timezone():
    inserted_bodies = []

    class FakeInsert:
        def execute(self):
            return {"id": "evt_123"}

    class FakeEvents:
        def insert(self, *, calendarId: str, body: dict[str, object]):
            assert calendarId == "primary"
            inserted_bodies.append(body)
            return FakeInsert()

    class FakeService:
        def events(self):
            return FakeEvents()

    with (
        patch.dict(os.environ, {"CALENDAR_LOCAL_TIMEZONE": "Europe/Paris"}, clear=False),
        patch("unmute.mcp_google_workspace.get_google_credentials", return_value=object()),
        patch("unmute.mcp_google_workspace.build", return_value=FakeService()),
    ):
        result = create_calendar_event(
            summary="Dentiste",
            start_time="2026-03-18T15:00:00+01:00",
            duration_minutes=30,
        )

    assert "Événement créé" in result
    assert inserted_bodies[0]["start"]["timeZone"] == "Europe/Paris"
    assert inserted_bodies[0]["end"]["timeZone"] == "Europe/Paris"
    assert inserted_bodies[0]["start"]["dateTime"] == "2026-03-18T15:00:00+01:00"
