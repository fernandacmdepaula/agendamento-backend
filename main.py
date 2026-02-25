import os
import json
import psycopg2
from fastapi import FastAPI, Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

app = FastAPI()

SCOPES = ["https://www.googleapis.com/auth/calendar"]

DATABASE_URL = os.getenv("DATABASE_URL")


def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada no Railway (web -> Variables)")
    return psycopg2.connect(DATABASE_URL)


def ensure_table():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id SERIAL PRIMARY KEY,
            booking_id TEXT UNIQUE,
            google_event_id TEXT,
            status TEXT DEFAULT 'active'
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def get_google_service():
    token_str = os.getenv("TOKEN_JSON")
    if not token_str:
        raise RuntimeError("TOKEN_JSON não configurado no Railway (web -> Variables)")

    info = json.loads(token_str)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    return build("calendar", "v3", credentials=creds)


@app.get("/health")
def health():
    ensure_table()
    return {"status": "ok"}


@app.post("/booking-created")
async def booking_created(request: Request):
    ensure_table()
    data = await request.json()

    booking_id = data["booking_id"]
    start_time = data["start_time"]  # ex: "2026-02-25T14:00:00-03:00"
    end_time = data["end_time"]      # ex: "2026-02-25T14:30:00-03:00"
    customer_name = data.get("customer_name", "Cliente")

    service = get_google_service()

    event = {
        "summary": f"Barbearia - {customer_name}",
        "start": {"dateTime": start_time, "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end_time, "timeZone": "America/Sao_Paulo"},
    }

    created = service.events().insert(calendarId="primary", body=event).execute()
    event_id = created["id"]

    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO appointments (booking_id, google_event_id, status)
        VALUES (%s, %s, 'active')
        ON CONFLICT (booking_id) DO UPDATE
        SET google_event_id = EXCLUDED.google_event_id, status='active'
        """,
        (booking_id, event_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    return {"status": "created", "google_event_id": event_id}


@app.post("/booking-canceled")
async def booking_canceled(request: Request):
    ensure_table()
    data = await request.json()
    booking_id = data["booking_id"]

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT google_event_id FROM appointments WHERE booking_id=%s", (booking_id,))
    row = cur.fetchone()

    if row and row[0]:
        event_id = row[0]

        service = get_google_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()

        cur.execute("UPDATE appointments SET status='canceled' WHERE booking_id=%s", (booking_id,))
        conn.commit()

    cur.close()
    conn.close()

    return {"status": "canceled"}
