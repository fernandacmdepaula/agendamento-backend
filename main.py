import os
import json
import base64
import uuid
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, HTTPException
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import psycopg2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

APP_VERSION = "2026-03-06-debug-calendar-links-v1"

app = FastAPI()

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
BH_TZ = timezone(timedelta(hours=-3))

_RE_ISO_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
_RE_DMY = re.compile(r"^\d{2}/\d{2}/\d{4}T\d{2}:\d{2}(:\d{2})?$")


def get_google_service():
    b64 = os.getenv("TOKEN_JSON_B64")
    if not b64 or not b64.strip():
        raise RuntimeError("TOKEN_JSON_B64 não configurado no Railway")

    token_str = base64.b64decode(b64).decode("utf-8")
    info = json.loads(token_str)

    creds = Credentials.from_authorized_user_info(info, SCOPES)
    # cache_discovery=False evita warning e é mais estável em ambientes serverless
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise RuntimeError("DATABASE_URL não configurado no Railway")
    return psycopg2.connect(database_url)


def calc_duration_min(service: str) -> int:
    s = (service or "").lower()
    if "corte + barba" in s or ("corte" in s and "barba" in s):
        return 60
    if "barba" in s and "corte" not in s:
        return 30
    if "sobrancelha" in s:
        return 15
    if "hidrata" in s:
        return 20
    if "infantil" in s:
        return 35
    return 40


def normalize_to_rfc3339(dt_str: str) -> str:
    s = (dt_str or "").strip()
    if not s:
        raise HTTPException(status_code=422, detail="start_time vazio")

    if "@data." in s or "@system" in s or "@custom" in s or "@response" in s:
        raise HTTPException(status_code=422, detail=f"start_time inválido (variável não renderizada): {s}")

    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)

    tail = s[10:] if len(s) > 10 else ""
    has_tz = s.endswith("Z") or ("+" in tail) or (re.search(r"T\d{2}:\d{2}(:\d{2})?-\d{2}:\d{2}$", s) is not None)

    if has_tz:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt.isoformat()
        except ValueError:
            # fallback
            try:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
                return dt.isoformat()
            except ValueError:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M%z")
                return dt.isoformat()

    if _RE_ISO_YMD.match(s):
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            if len(s) == 16:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
            else:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=BH_TZ).isoformat()

    if _RE_DMY.match(s):
        try:
            if len(s) == 16:
                dt = datetime.strptime(s, "%d/%m/%YT%H:%M")
            else:
                dt = datetime.strptime(s, "%d/%m/%YT%H:%M:%S")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"start_time inválido (DD/MM/YYYY): {e}")
        return dt.replace(tzinfo=BH_TZ).isoformat()

    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BH_TZ)
        return dt.isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"start_time inválido (formato não reconhecido): {s}")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    body = await request.body()
    logger.info(f"request path={request.url.path} content-type={request.headers.get('content-type')} body_len={len(body)}")
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION, "calendar_id": CALENDAR_ID}


@app.get("/debug-latest")
async def debug_latest():
    """
    Lista os próximos eventos do CALENDAR_ID (para confirmar se está criando no calendário certo).
    """
    service = get_google_service()
    now = datetime.now(timezone.utc).isoformat()
    resp = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now,
        maxResults=10,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    items = resp.get("items", [])
    out = []
    for ev in items:
        out.append({
            "id": ev.get("id"),
            "summary": ev.get("summary"),
            "start": ev.get("start"),
            "end": ev.get("end"),
            "status": ev.get("status"),
            "htmlLink": ev.get("htmlLink"),
        })

    return {"calendar_id": CALENDAR_ID, "count": len(out), "events": out}
from datetime import datetime, timedelta, timezone

@app.get("/debug-zaia")
async def debug_zaia(days_back: int = 60, days_forward: int = 180, max_results: int = 50):
    service = get_google_service()

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days_back)).isoformat()
    time_max = (now + timedelta(days=days_forward)).isoformat()

    resp = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=time_min,
        timeMax=time_max,
        q="[ZAIA]",            # procura eventos criados pelo seu sistema
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    items = resp.get("items", [])
    return {
        "calendar_id": CALENDAR_ID,
        "count": len(items),
        "events": [
            {
                "id": ev.get("id"),
                "summary": ev.get("summary"),
                "start": ev.get("start"),
                "end": ev.get("end"),
                "status": ev.get("status"),
                "htmlLink": ev.get("htmlLink"),
            }
            for ev in items
        ],
    }

@app.post("/booking-created")
async def booking_created(request: Request):
    data = await request.json()
    logger.info(f"version={APP_VERSION} booking-created keys={list(data.keys())}")

    booking_id = (data.get("booking_id") or data.get("id") or "").strip()
    if not booking_id:
        booking_id = str(uuid.uuid4())

    client_name = data.get("client_name") or data.get("name") or "Cliente"
    service_name = data.get("service") or data.get("servico") or ""
    raw_start = data.get("start_time") or data.get("start")
    raw_end = data.get("end_time") or data.get("end")
    client_phone = data.get("phone") or data.get("client_phone") or data.get("telefone") or ""

    if not raw_start:
        raise HTTPException(status_code=400, detail="start_time ausente (ou start)")

    logger.info(f"booking-created start_time_raw={raw_start}")
    start_time = normalize_to_rfc3339(raw_start)
    logger.info(f"booking-created normalize_ok start_time={start_time}")

    if raw_end:
        end_time = normalize_to_rfc3339(raw_end)
    else:
        duration = calc_duration_min(service_name)
        dt_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        dt_end = dt_start + timedelta(minutes=duration)
        end_time = dt_end.isoformat()

    dt_for_db = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(BH_TZ)
    start_at = dt_for_db.strftime("%Y-%m-%d %H:%M:%S")
    start_date = dt_for_db.strftime("%Y-%m-%d")

    service = get_google_service()
    event = {
        "summary": f"[ZAIA] {client_name} ({booking_id})",
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
        "description": data.get("notes", ""),
    }

    try:
        created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    except HttpError as e:
        raise HTTPException(status_code=400, detail=f"Google Calendar recusou o evento: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao criar no Google Calendar: {e}")

    google_event_id = created.get("id")
    html_link = created.get("htmlLink")
    logger.info(f"booking-created google_event_id={google_event_id} calendar_id={CALENDAR_ID} htmlLink={html_link}")

    if not google_event_id:
        raise HTTPException(status_code=500, detail="Google não retornou o id do evento")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO appointments (
            booking_id, google_event_id, status, client_phone, start_date, start_at,
            reminder_sent, reminder_sent_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, false, NULL)
        ON CONFLICT (booking_id) DO UPDATE
        SET google_event_id = EXCLUDED.google_event_id,
            status = EXCLUDED.status,
            client_phone = EXCLUDED.client_phone,
            start_date = EXCLUDED.start_date,
            start_at = EXCLUDED.start_at
        """,
        (booking_id, google_event_id, "created", client_phone, start_date, start_at),
    )
    conn.commit()
    cur.close()
    conn.close()

    return {
        "status": "created",
        "booking_id": booking_id,
        "google_event_id": google_event_id,
        "calendar_id": CALENDAR_ID,
        "htmlLink": html_link,
    }


@app.post("/booking-canceled")
async def booking_canceled(request: Request):
    data = await request.json()
    booking_id = (data.get("booking_id") or data.get("id") or "").strip()

    if not booking_id:
        raise HTTPException(status_code=400, detail="booking_id ausente")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT google_event_id FROM appointments WHERE booking_id = %s", (booking_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="booking_id não encontrado no banco")

    google_event_id = row[0]

    service = get_google_service()
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=google_event_id).execute()
    except HttpError:
        pass

    cur.execute("UPDATE appointments SET status = %s WHERE booking_id = %s", ("canceled", booking_id))
    conn.commit()
    cur.close()
    conn.close()

    return {"status": "deleted", "booking_id": booking_id, "google_event_id": google_event_id}

