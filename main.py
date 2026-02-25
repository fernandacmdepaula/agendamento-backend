from fastapi import FastAPI, Request
import os
import psycopg2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

app = FastAPI()

DATABASE_URL = os.getenv(DATABASE_URL)

SCOPES = ['httpswww.googleapis.comauthcalendar']

def get_google_service()
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    return build('calendar', 'v3', credentials=creds)

def save_appointment(booking_id, event_id)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        CREATE TABLE IF NOT EXISTS appointments (
            id SERIAL PRIMARY KEY,
            booking_id TEXT,
            google_event_id TEXT
        )
    )
    cur.execute(
        INSERT INTO appointments (booking_id, google_event_id)
        VALUES (%s, %s)
    , (booking_id, event_id))
    conn.commit()
    cur.close()
    conn.close()

def get_event_id(booking_id)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        SELECT google_event_id FROM appointments
        WHERE booking_id = %s
    , (booking_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result[0] if result else None

@app.post(booking-created)
async def booking_created(request Request)
    data = await request.json()

    booking_id = data[booking_id]
    start_time = data[start_time]
    end_time = data[end_time]
    customer_name = data[customer_name]

    service = get_google_service()

    event = {
        'summary' fCorte - {customer_name},
        'start' {'dateTime' start_time, 'timeZone' 'AmericaSao_Paulo'},
        'end' {'dateTime' end_time, 'timeZone' 'AmericaSao_Paulo'},
    }

    created = service.events().insert(calendarId='primary', body=event).execute()

    save_appointment(booking_id, created[id])

    return {status created}

@app.post(booking-canceled)
async def booking_canceled(request Request)
    data = await request.json()
    booking_id = data[booking_id]

    event_id = get_event_id(booking_id)

    if event_id
        service = get_google_service()
        service.events().delete(calendarId='primary', eventId=event_id).execute()

    return {status deleted}

@app.get(health)
def health()
    return {status ok}