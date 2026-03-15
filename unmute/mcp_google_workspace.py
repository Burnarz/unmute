import json
import datetime
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-google-workspace")

mcp = FastMCP("google-workspace")

import os

# Read the token path from environment variable, fallback to previous if not set
token_env = os.environ.get("GOOGLE_TOKEN_FILE")
TOKEN_PATH = Path(token_env)

def get_google_credentials():
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(f"Google token file not found at {TOKEN_PATH}")
    
    with open(TOKEN_PATH, "r") as f:
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
    """Read upcoming appointments and events from the user's primary calendar and Sauvane's shared calendar. Useful when the user asks about their plans or their wife's plans."""
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        
        now = datetime.datetime.utcnow().isoformat() + 'Z'
        calendars = [{'id': 'primary', 'name': 'Votre calendrier'}]
        
        shared_cal_id = os.environ.get("SHARED_CALENDAR_ID")
        if shared_cal_id:
            calendars.append({'id': shared_cal_id, 'name': 'Calendrier partagé'})
        
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
                    # Add calendar name to the event for clarity
                    event['_calendar_name'] = cal['name']
                    all_events.append(event)
            except Exception as cal_err:
                logger.warning(f"Could not fetch calendar {cal['id']}: {cal_err}")
                continue
        
        if not all_events:
            return "Aucun événement à venir trouvé dans vos calendriers."
            
        # Sort combined events by start time
        all_events.sort(key=lambda x: x['start'].get('dateTime', x['start'].get('date')))
        
        # Limit to global max_results after merging
        all_events = all_events[:max_results]
            
        result = "Événements à venir :\n"
        for event in all_events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            # Format start time slightly for better readability if it's an ISO string
            try:
                if 'T' in start:
                    dt = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                    start = dt.strftime('%d/%m %H:%M')
            except:
                pass
                
            result += f"- [{event['_calendar_name']}] {start}: {event.get('summary', 'Sans titre')}\n"
        return result
    except Exception as e:
        logger.error(f"Error fetching calendars: {e}")
        return f"Erreur lors de l'interaction avec Google Calendar : {str(e)}"

@mcp.tool()
def create_calendar_event(
    summary: str,
    start_time: str,
    duration_minutes: int = 30,
    description: str = ""
) -> str:
    """Create a new event in the user's primary Google Calendar.
    
    Args:
        summary: Title of the event.
        start_time: Start time in ISO format (e.g., '2026-03-15T14:30:00Z') or natural language if the LLM can parse it.
        duration_minutes: Duration of the event in minutes.
        description: Optional description or notes for the event.
    """
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        
        # Try to parse the start time
        try:
            # Handle natural formats or simple ISO
            if 'T' not in start_time:
                # If LLM sends something like '2026-03-15 14:30', convert to ISO
                start_dt = datetime.datetime.fromisoformat(start_time.replace(' ', 'T'))
            else:
                start_dt = datetime.datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        except Exception:
            return f"Format de date invalide : {start_time}. Utilisez le format ISO (AAAA-MM-JJTHH:MM:SSZ)."

        end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
        
        event = {
            'summary': summary,
            'description': description,
            'start': {
                'dateTime': start_dt.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_dt.isoformat(),
                'timeZone': 'UTC',
            },
        }
        
        event = service.events().insert(calendarId='primary', body=event).execute()
        
        return f"Événement créé avec succès : {summary} le {start_dt.strftime('%d/%m à %H:%M')}. Lien : {event.get('htmlLink')}"
    except Exception as e:
        logger.error(f"Error creating event: {e}")
        return f"Erreur lors de la création de l'événement : {str(e)}"

@mcp.tool()
def get_latest_emails(max_results: int = 3) -> str:
    """Read the user's latest received emails. Returns the sender, subject, and a short text snippet. Useful when the user asks if they have new messages."""
    try:
        creds = get_google_credentials()
        service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
        
        # Fetch the latest INBOX messages
        results = service.users().messages().list(userId='me', labelIds=['INBOX'], maxResults=max_results).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return "No messages found in INBOX."
            
        result = "Latest emails:\n"
        for msg in messages:
            msg_data = service.users().messages().get(userId='me', id=msg['id'], format='metadata', metadataHeaders=['From', 'Subject']).execute()
            headers = {h['name']: h['value'] for h in msg_data['payload']['headers']}
            sender = headers.get('From', 'Unknown')
            subject = headers.get('Subject', 'No Subject')
            snippet = msg_data.get('snippet', '')
            
            result += f"- From: {sender}\n  Subject: {subject}\n  Preview: {snippet}\n\n"
        return result
    except Exception as e:
        logger.error(f"Error fetching emails: {e}")
        return f"Error interacting with Gmail: {str(e)}"

if __name__ == "__main__":
    mcp.run()
