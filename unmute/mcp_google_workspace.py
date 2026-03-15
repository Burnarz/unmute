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
def get_next_calendar_events(max_results: int = 3) -> str:
    """Read the upcoming user appointments and events from their Google Calendar. Useful when the user asks what they have planned."""
    try:
        creds = get_google_credentials()
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        
        now = datetime.datetime.utcnow().isoformat() + 'Z'
        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=max_results, singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        if not events:
            return "No upcoming events found on Google Calendar."
            
        result = "Upcoming events:\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            result += f"- {start}: {event.get('summary', 'No Title')}\n"
        return result
    except Exception as e:
        logger.error(f"Error fetching calendar: {e}")
        return f"Error interacting with Google Calendar: {str(e)}"

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
