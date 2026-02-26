import os
import json
import base64
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_google_service():
    b64 = os.getenv("TOKEN_JSON_B64")

    if not b64:
        raise RuntimeError("TOKEN_JSON_B64 não configurado no Railway")

    token_str = base64.b64decode(b64).decode("utf-8")

    info = json.loads(token_str)
    creds = Credentials.from_authorized_user_info(info, SCOPES)

    return build("calendar", "v3", credentials=creds)
