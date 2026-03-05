import os
import json
import base64
import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, HTTPException
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import psycopg2

# -----------------------------
# Logging
# -----------------------------
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


def _ensure_t_separator(s: str) -> str:
    # Converte "YYYY-MM-DD HH:MM" -> "YYYY-MM-DDTHH:MM"
    # Converte "DD/MM/YYYY HH:MM" -> "DD/MM/YYYYTHH:MM"
    if " " in s and "T" not in s:
        return s.replace(" ", "T", 1)
    return s


def normalize_to_rfc3339(dt_str: str) -> str:
    """
    Aceita formatos comuns vindos do Zaia:
      - YYYY-MM-DDTHH:MM
      - YYYY-MM-DDTHH:MM:SS
      - YYYY-MM-DD HH:MM
      - DD/MM/YYYYTHH:MM
      - DD/MM/YYYYTHH:MM:SS
      - DD/MM/YYYY HH:MM
      - com timezone: ...-03:00 / +00:00 / Z
    Retorna ISO/RFC3339 com timezone. Se não vier timezone, assume BH (-03:00).
    """
    s = (dt_str or "").strip()
    if not s:
        return ""

    s = _ensure_t_separator(s)

    # Caso venha com Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    # Se já vier com offset explícito (ex: -03:00 / +00:00)
    # Observação: checamos apenas a "cauda" depois da data
    tail = s[10:]
    has_tz = ("+" in tail) or ("-" in tail and ":" in tail and "T" in s)

    # Tentativas de parse (ordem importa)
    parse_attempts = []

    if has_tz:
        # Com timezone no final
        parse_attempts = [
            ("fromisoformat", None),
            ("strptime", "%Y-%m-%dT%H:%M:%S%z"),
            ("strptime", "%Y-%m-%dT%H:%M%z"),
            ("strptime", "%d/%m/%YT%H:%M:%S%z"),
            ("strptime", "%d/%m/%YT%H:%M%z"),
        ]
    else:
        # Sem timezone -> vamos assumir BH
        parse_attempts = [
            ("fromisoformat", None),
            ("strptime", "%Y-%m-%dT%H:%M:%S"),
            ("strptime", "%Y-%m-%dT%H:%M"),
            ("strptime", "%d/%m/%YT%H:%M:%S"),
            ("strptime", "%d/%m/%YT%H:%M"),
        ]

    dt = None
    last_err = None

    for kind, fmt in parse_attempts:
        try:
            if kind == "fromisoformat":
                dt = datetime.fromisoformat(s)
            else:
                dt = datetime.strptime(s, fmt)
            break
        except Exception as e:
            last_err = e
            dt = None

    if dt is None:
        logger.info(f"booking-created normalize error=Falha parse start_time '{s}' err={last_err}")
        return ""

    if not has_tz:
        dt = dt.replace(tzinfo=BH_TZ)

    return dt.isoformat()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/booking-created")
async def booking_created(request: Request):
    data = await request.json()

    booking_id = data.get("booking_id") or data.get("id")
    client_name = data.get("client_name") or data.get("name") or "Cliente"
    service_name = data.get("service") or data.get("servico") or ""

    raw_start = data.get("start_time") or data.get("start")
    raw_end = data.get("end_time") or data.get("end")

    client_phone = data.get("phone") or data.get("client_phone") or data.get("telefone")

    logger.info(f"request content-type={request.headers.get('content-type')} body_len={request.headers.get('content-length')}")
    logger.info(f"booking-created keys={list(data.keys())}")
    logger.info(f"booking-created start_time_raw={raw_start} phone={(client_phone or '')[:2]}***{(client_phone or '')[-2:]}")

    if not booking_id:
        raise HTTPException(status_code=400, detail="booking_id ausente")
    if not raw_start:
        raise HTTPException(status_code=400, detail="start_time ausente (ou start)")

    start_time = normalize_to_rfc3339(raw_start)
    if not start_time:
        raise HTTPException(status_code=422, detail="start_time inválido")

    # end_time opcional: calcula se não vier
    if raw_end:
        end_time = normalize_to_rfc3339(raw_end)
        if not end_time:
            raise HTTPException(status_code=422, detail="end_time inválido")
    else:
        duration = calc_duration_min(service_name)
        dt_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        dt_end = dt_start + timedelta(minutes=duration)
        end_time = dt_end.isoformat()

    # salvar no banco
    dt_for_db = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(BH_TZ)
    start_at = dt_for_db.strftime("%Y-%m-%d %H:%M:%S")
    start_date = dt_for_db.strftime("%Y-%m-%d")

    # Google Calendar
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

    # Postgres
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
    data = await request.json()
    booking_id = data.get("booking_id") or data.get("id")

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
