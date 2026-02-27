import os
import json
import base64
from fastapi import FastAPI, Request, HTTPException
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import psycopg2

app = FastAPI()

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")


def get_google_service():
    b64 = os.getenv("TOKEN_JSON_B64")
    if not b64 or not b64.strip():
        raise RuntimeError("TOKEN_JSON_B64 não configurado no Railway")

    token_str = base64.b64decode(b64).decode("utf-8")
    info = json.loads(token_str)

    creds = Credentials.from_authorized_user_info(info, SCOPES)
    return build("calendar", "v3", credentials=creds)


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise RuntimeError("DATABASE_URL não configurado no Railway")

    return psycopg2.connect(database_url)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/booking-created")
async def booking_created(request: Request):
    data = await request.json()

    booking_id = data.get("booking_id") or data.get("id")
    client_name = data.get("client_name") or data.get("name") or "Cliente"

    start_time = data.get("start_time") or data.get("start")
    end_time = data.get("end_time") or data.get("end")

    if not booking_id:
        raise HTTPException(status_code=400, detail="booking_id ausente")
    if not start_time or not end_time:
        raise HTTPException(status_code=400, detail="start_time/end_time ausentes (ou start/end)")

    # 1) cria no Google Calendar
    service = get_google_service()

    event = {
        "summary": f"[ZAIA] {client_name} ({booking_id})",
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
        "description": data.get("notes", ""),
    }

    created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    google_event_id = created.get("id")

    if not google_event_id:
        raise HTTPException(status_code=500, detail="Google não retornou o id do evento")

    # 2) salva no Postgres
    conn = get_db_connection()
    cur = conn.cursor()

    # precisa do booking_id com UNIQUE/PK para ON CONFLICT funcionar
    cur.execute(
        """
        INSERT INTO appointments (booking_id, google_event_id, status)
        VALUES (%s, %s, %s)
        ON CONFLICT (booking_id) DO UPDATE
        SET google_event_id = EXCLUDED.google_event_id,
            status = EXCLUDED.status
        """,
        (booking_id, google_event_id, "created"),
    )

    conn.commit()
    cur.close()
    conn.close()

    return {"status": "created", "booking_id": booking_id, "google_event_id": google_event_id}


@app.post("/booking-canceled")
async def booking_canceled(request: Request):
    data = await request.json()
    booking_id = data.get("booking_id") or data.get("id")

    if not booking_id:
        raise HTTPException(status_code=400, detail="booking_id ausente")

    # 1) busca no Postgres
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT google_event_id FROM appointments WHERE booking_id = %s", (booking_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="booking_id não encontrado no banco")

    google_event_id = row[0]

    # 2) deleta no Google Calendar
    service = get_google_service()
    service.events().delete(calendarId=CALENDAR_ID, eventId=google_event_id).execute()

    # 3) atualiza status
    cur.execute("UPDATE appointments SET status = %s WHERE booking_id = %s", ("canceled", booking_id))
    conn.commit()

    cur.close()
    conn.close()

    return {"status": "deleted", "booking_id": booking_id, "google_event_id": google_event_id}
