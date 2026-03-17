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

from unmute.mcp_google_workspace import delete_calendar_event


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
