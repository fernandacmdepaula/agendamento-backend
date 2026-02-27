import os
import json
import base64
import logging
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, HTTPException
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import psycopg2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI()

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")

BH_TZ = timezone(timedelta(hours=-3))


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
        return ""

    if "@data." in s or "{{" in s or "}}" in s:
        raise ValueError(f"start_time inválido (variável não renderizada): {s}")

    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)

    tail = s[10:]
    if s.endswith("Z") or ("+" in tail) or ("-" in tail and "T" in s):
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M%z")
        return dt.isoformat()

    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
    dt = dt.replace(tzinfo=BH_TZ)
    return dt.isoformat()


def mask_phone(p: str | None) -> str:
    if not p:
        return ""
    p = str(p).strip()
    if len(p) <= 4:
        return "***"
    return p[:2] + "***" + p[-2:]


async def read_body_any(request: Request) -> dict:
    content_type = (request.headers.get("content-type") or "").lower()
    raw = await request.body()

    logger.info("request content-type=%s body_len=%s", content_type, len(raw))

    if not raw:
        raise HTTPException(status_code=400, detail=f"Body vazio. content-type={content_type}")

    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        try:
            form = await request.form()
            return dict(form)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Body inválido (não é JSON nem FORM). content-type={content_type}")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/booking-created")
async def booking_created(request: Request):
    data = await read_body_any(request)

    # ✅ LOG: quais campos chegaram (não expõe valores)
    try:
        logger.info("booking-created keys=%s", list(data.keys()))
    except Exception:
        pass

    booking_id = (data.get("booking_id") or data.get("id") or "").strip()
    if not booking_id:
        booking_id = str(uuid4())

    client_name = data.get("client_name") or data.get("name") or "Cliente"
    service_name = data.get("service") or data.get("servico") or ""

    raw_start = data.get("start_time") or data.get("start")
    raw_end = data.get("end_time") or data.get("end")

    client_phone = data.get("phone") or data.get("client_phone") or data.get("telefone")

    # ✅ LOG: mostra start_time e phone mascarado (para diagnosticar)
    logger.info("booking-created start_time_raw=%s phone=%s", str(raw_start), mask_phone(client_phone))

    if not raw_start:
        raise HTTPException(status_code=422, detail="start_time ausente (ou start)")

    try:
        start_time = normalize_to_rfc3339(raw_start)
    except ValueError as ve:
        # ✅ LOG do motivo
        logger.info("booking-created normalize error=%s", str(ve))
        raise HTTPException(status_code=422, detail=str(ve))

    if not start_time:
        raise HTTPException(status_code=422, detail="start_time inválido")

    if raw_end:
        try:
            end_time = normalize_to_rfc3339(raw_end)
        except ValueError as ve:
            logger.info("booking-created end_time normalize error=%s", str(ve))
            raise HTTPException(status_code=422, detail=str(ve))
        if not end_time:
            raise HTTPException(status_code=422, detail="end_time inválido")
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
    if not google_event_id:
        raise HTTPException(status_code=500, detail="Google não retornou o id do evento")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO appointments (
            booking_id,
            google_event_id,
            status,
            client_phone,
            start_date,
            start_at,
            reminder_sent,
            reminder_sent_at
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

    return {"status": "created", "booking_id": booking_id, "google_event_id": google_event_id}


@app.post("/booking-canceled")
async def booking_canceled(request: Request):
    data = await read_body_any(request)

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
