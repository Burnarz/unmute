import json
import datetime
import base64
from pathlib import Path
from zoneinfo import ZoneInfo
from mcp.server.fastmcp import FastMCP
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from email.message import EmailMessage
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-google-workspace")

mcp = FastMCP("google-workspace")

import os


def _token_path() -> Path:
    token_env = os.environ.get("GOOGLE_TOKEN_FILE")
    if not token_env:
        raise FileNotFoundError("GOOGLE_TOKEN_FILE is not configured")
    return Path(token_env)


def _calendar_timezone_name() -> str:
    return os.environ.get("CALENDAR_LOCAL_TIMEZONE") or os.environ.get("TZ", "UTC")


def _calendar_timezone() -> ZoneInfo:
    timezone_name = _calendar_timezone_name()
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        logger.warning("Invalid calendar timezone '%s', falling back to UTC", timezone_name)
        return ZoneInfo("UTC")


def _configured_calendars() -> list[dict[str, str]]:
    calendars = [{'id': 'primary', 'name': 'Votre calendrier'}]
    shared_cal_id = os.environ.get("SHARED_CALENDAR_ID")
    if shared_cal_id:
        calendars.append({'id': shared_cal_id, 'name': 'Calendrier partagé'})
    return calendars


def get_google_credentials():
    token_path = _token_path()
    if not token_path.exists():
        raise FileNotFoundError(f"Google token file not found at {token_path}")

    with open(token_path, "r") as f:
        data = json.load(f)
        
    return Credentials(
        token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=data.get("scopes", [])
    )

@mcp.tool()
def get_next_calendar_events(max_results: int = 10) -> str:
    """Read upcoming appointments and events from the user's primary calendar and shared calendars.

    Useful when the user asks about their plans or wants to identify an event before deleting it.
    Includes event IDs that can be passed to `delete_calendar_event`.
    """
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)

        now = datetime.datetime.utcnow().isoformat() + 'Z'
        calendars = _configured_calendars()

        all_events = []
        
        for cal in calendars:
            try:
                events_result = service.events().list(
                    calendarId=cal['id'], timeMin=now,
                    maxResults=max_results, singleEvents=True,
                    orderBy='startTime'
                ).execute()
                
                events = events_result.get('items', [])
                for event in events:
                    event['_calendar_name'] = cal['name']
                    event['_calendar_id'] = cal['id']
                    all_events.append(event)
            except Exception as cal_err:
                logger.warning(f"Could not fetch calendar {cal['id']}: {cal_err}")
                continue
        
        if not all_events:
            return "Aucun événement à venir trouvé dans vos calendriers."
            
        all_events.sort(key=lambda x: x['start'].get('dateTime', x['start'].get('date')))
        all_events = all_events[:max_results]
            
        result = "Événements à venir :\n"
        for event in all_events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            try:
                if 'T' in start:
                    dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                    start = dt.strftime('%d/%m %H:%M')
            except:
                pass
                
            result += f"- [{event['_calendar_name']}] {start}: {event.get('summary', 'Sans titre')} (ID: {event.get('id')})\n"
        return result
    except Exception as e:
        logger.error(f"Error fetching calendars: {e}")
        return f"Erreur lors de l'interaction avec Google Calendar : {str(e)}"

@mcp.tool()
def get_daily_agenda(date_str: str = None) -> str:
    """Get all events for a specific day. If date_str is None, it defaults to today.
    date_str should be in 'YYYY-MM-DD' format.
    Includes event IDs so you can identify an event before deleting it.
    """
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)

        timezone = _calendar_timezone()
        if date_str:
            base_date = datetime.date.fromisoformat(date_str)
        else:
            base_date = datetime.datetime.now(timezone).date()

        day_start = datetime.datetime.combine(
            base_date, datetime.time.min, tzinfo=timezone
        )
        day_end = datetime.datetime.combine(
            base_date, datetime.time(23, 59, 59), tzinfo=timezone
        )

        all_events = []
        for calendar in _configured_calendars():
            try:
                events_result = service.events().list(
                    calendarId=calendar['id'],
                    timeMin=day_start.isoformat(),
                    timeMax=day_end.isoformat(),
                    singleEvents=True,
                    orderBy='startTime',
                ).execute()
            except Exception as cal_err:
                logger.warning(f"Could not fetch calendar {calendar['id']}: {cal_err}")
                continue

            for event in events_result.get('items', []):
                event['_calendar_name'] = calendar['name']
                all_events.append(event)

        if not all_events:
            return f"Aucun événement prévu pour le {base_date.strftime('%d/%m/%Y')}."

        all_events.sort(key=lambda x: x['start'].get('dateTime', x['start'].get('date')))
        result = f"Agenda pour le {base_date.strftime('%d/%m/%Y')} :\n"
        for event in all_events:
            start = event['start'].get('dateTime', 'Journée entière')
            if 'T' in start:
                start = datetime.datetime.fromisoformat(start.replace('Z', '+00:00')).strftime('%H:%M')
            result += (
                f"- [{event['_calendar_name']}] {start}: "
                f"{event.get('summary', 'Sans titre')} (ID: {event.get('id')})\n"
            )
        return result
    except Exception as e:
        return f"Erreur lors de la récupération de l'agenda : {str(e)}"

@mcp.tool()
def create_calendar_event(
    summary: str,
    start_time: str,
    duration_minutes: int = 30,
    description: str = ""
) -> str:
    """Create a new event in the user's primary Google Calendar."""
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)

        try:
            if 'T' not in start_time:
                start_dt = datetime.datetime.fromisoformat(start_time.replace(' ', 'T'))
            else:
                start_dt = datetime.datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        except Exception:
            return f"Format de date invalide : {start_time}. Utilisez le format ISO (AAAA-MM-JJTHH:MM:SSZ)."

        calendar_timezone = _calendar_timezone()
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=calendar_timezone)
        else:
            start_dt = start_dt.astimezone(calendar_timezone)

        end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)

        event = {
            'summary': summary,
            'description': description,
            'start': {
                'dateTime': start_dt.isoformat(),
                'timeZone': _calendar_timezone_name(),
            },
            'end': {
                'dateTime': end_dt.isoformat(),
                'timeZone': _calendar_timezone_name(),
            },
        }

        event = service.events().insert(calendarId='primary', body=event).execute()
        return f"Événement créé : {summary} le {start_dt.strftime('%d/%m à %H:%M')}."
    except Exception as e:
        return f"Erreur lors de la création : {str(e)}"


@mcp.tool()
def get_calendar_event_details(event_id: str) -> dict:
    """Get calendar event details by ID from the primary or shared calendar."""
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        calendars = _configured_calendars()

        last_error = None
        for calendar in calendars:
            try:
                event = service.events().get(
                    calendarId=calendar['id'],
                    eventId=event_id,
                ).execute()
                return {
                    "event_id": event_id,
                    "summary": event.get('summary', 'Sans titre'),
                    "start": event.get('start', {}).get('dateTime', event.get('start', {}).get('date')),
                    "end": event.get('end', {}).get('dateTime', event.get('end', {}).get('date')),
                    "calendar_id": calendar['id'],
                    "calendar_name": calendar['name'],
                }
            except Exception as exc:
                last_error = exc
                logger.info(
                    "Could not fetch event %s from calendar %s: %s",
                    event_id,
                    calendar['id'],
                    exc,
                )

        return {"error": f"Erreur lors de la récupération de l'événement : {last_error}"}
    except Exception as e:
        return {"error": f"Erreur lors de la récupération de l'événement : {str(e)}"}


@mcp.tool()
def delete_calendar_event(event_id: str) -> str:
    """Delete an event from the primary calendar or shared calendar using its ID.

    If you do not know the event ID yet, first call `get_next_calendar_events` or
    `get_daily_agenda` to retrieve the matching event and its ID.
    """
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        calendar_ids = ['primary']
        shared_cal_id = os.environ.get("SHARED_CALENDAR_ID")
        if shared_cal_id:
            calendar_ids.append(shared_cal_id)

        last_error = None
        for calendar_id in calendar_ids:
            try:
                service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
                return f"Événement {event_id} supprimé avec succès."
            except Exception as exc:
                last_error = exc
                logger.info(
                    "Could not delete event %s from calendar %s: %s",
                    event_id,
                    calendar_id,
                    exc,
                )

        return f"Erreur lors de la suppression : {str(last_error)}"
    except Exception as e:
        return f"Erreur lors de la suppression : {str(e)}"

@mcp.tool()
def get_latest_emails(max_results: int = 3) -> str:
    """Read the user's latest received emails."""
    try:
        creds = get_google_credentials()
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        results = service.users().messages().list(userId='me', labelIds=['INBOX'], maxResults=max_results).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return "Aucun message dans la boîte de réception."
            
        result = "Derniers emails :\n"
        for msg in messages:
            msg_data = service.users().messages().get(userId='me', id=msg['id'], format='metadata', metadataHeaders=['From', 'Subject']).execute()
            headers = {h['name']: h['value'] for h in msg_data['payload']['headers']}
            result += f"- De: {headers.get('From', 'Inconnu')}\n  Sujet: {headers.get('Subject', 'Sans sujet')}\n  Aperçu: {msg_data.get('snippet', '')}\n\n"
        return result
    except Exception as e:
        return f"Erreur Gmail : {str(e)}"

@mcp.tool()
def search_emails(query: str, max_results: int = 5) -> str:
    """Search for emails matching a query (sender, keyword, etc.)."""
    try:
        creds = get_google_credentials()
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        results = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return f"Aucun email trouvé pour la recherche : '{query}'."
            
        result = f"Résultats pour '{query}' :\n"
        for msg in messages:
            m = service.users().messages().get(userId='me', id=msg['id'], format='metadata', metadataHeaders=['From', 'Subject', 'Date']).execute()
            h = {header['name']: header['value'] for header in m['payload']['headers']}
            result += f"- {h.get('Date')}\n  De: {h.get('From')}\n  Sujet: {h.get('Subject')}\n  Extrait: {m.get('snippet')}\n\n"
        return result
    except Exception as e:
        return f"Erreur recherche Gmail : {str(e)}"

@mcp.tool()
def create_email_draft(to: str, subject: str, body: str) -> str:
    """Create a draft email in Gmail. This is safer than sending directly by voice."""
    try:
        creds = get_google_credentials()
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        
        message = EmailMessage()
        message.set_content(body)
        message['To'] = to
        message['Subject'] = subject
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_raw = {'message': {'raw': raw_message}}
        
        draft = service.users().drafts().create(userId='me', body=create_raw).execute()
        return f"Brouillon créé avec succès pour {to} (ID: {draft.get('id')})."
    except Exception as e:
        return f"Erreur création brouillon : {str(e)}"

@mcp.tool()
def get_contact_info(name: str) -> str:
    """Search for a contact's info (email, phone) by name."""
    try:
        creds = get_google_credentials()
        service = build('people', 'v1', credentials=creds, cache_discovery=False)
        results = service.people().searchContacts(
            query=name, readMask='names,emailAddresses,phoneNumbers'
        ).execute()
        
        connections = results.get('results', [])
        if not connections:
            return f"Aucun contact trouvé pour '{name}'."
            
        result = f"Contacts trouvés pour '{name}' :\n"
        for person in connections:
            p = person.get('person', {})
            n = p.get('names', [{}])[0].get('displayName', 'Inconnu')
            e = p.get('emailAddresses', [{}])[0].get('value', 'Pas d\'email')
            ph = p.get('phoneNumbers', [{}])[0].get('value', 'Pas de téléphone')
            result += f"- {n}: {e}, {ph}\n"
        return result
    except Exception as e:
        return f"Erreur People API : {str(e)}"

if __name__ == "__main__":
    mcp.run()
