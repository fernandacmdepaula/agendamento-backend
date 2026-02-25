import os, json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_google_service():
    token_str = os.getenv("TOKEN_JSON")
    if not token_str:
        raise RuntimeError("TOKEN_JSON não configurado nas variáveis do Railway")

    info = json.loads(token_str)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    return build("calendar", "v3", credentials=creds)
