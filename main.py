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

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

APP_VERSION = "2026-03-05-parse-ddmmyyyy-v3-robust"

app = FastAPI()

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")

BH_TZ = timezone(timedelta(hours=-3))

# ---------- Helpers ----------
def get_google_service():
    b64 = os.getenv("TOKEN_JSON_B64")
    if not b64 or not b64.strip():
        raise RuntimeError("TOKEN_JSON_B64 não configurado no Railway")

    token_str = base64.b64decode(b64).decode("utf-8")
    info = json.loads(token_str)

    creds = Credentials.from_authorized_user_info(info, SCOPES)
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


# ---------- Date parsing (robusto) ----------
_RE_ISO_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
_RE_DMY = re.compile(r"^\d{2}/\d{2}/\d{4}T\d{2}:\d{2}(:\d{2})?$")

def _fail_422(msg: str):
    raise HTTPException(status_code=422, detail=msg)

def normalize_to_rfc3339(dt_str: str) -> str:
    s = (dt_str or "").strip()
    if not s:
        _fail_422("start_time vazio")

    # pega erros comuns do Zaia (variável não renderizada)
    if "@data." in s or "@system" in s or "@custom" in s or "@response" in s:
        _fail_422(f"start_time inválido (variável não renderizada): {s}")

    # troca " " por "T"
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)

    # --- Se já tem timezone explícito (Z ou +hh:mm) ---
    # Observação: um "-" pode aparecer na data YYYY-MM-DD, então olhamos depois do índice 10
    tail = s[10:] if len(s) > 10 else ""
    if s.endswith("Z") or ("+" in tail) or (re.search(r"T\d{2}:\d{2}(:\d{2})?-\d{2}:\d{2}$", s) is not None):
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt.isoformat()
        except ValueError:
            # fallback raro
            try:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
                return dt.isoformat()
            except ValueError:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M%z")
                return dt.isoformat()

    # --- Sem timezone -> assume BH ---
    # ISO YYYY-MM-DDTHH:MM(:SS)?
    if _RE_ISO_YMD.match(s):
        try:
            dt = datetime.fromisoformat(s)  # aceita com/sem segundos
        except ValueError:
            # fallback
            if len(s) == 16:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
            else:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=BH_TZ)
        return dt.isoformat()

    # BR DD/MM/YYYYTHH:MM(:SS)?
    if _RE_DMY.match(s):
        try:
            if len(s) == 16:
                dt = datetime.strptime(s, "%d/%m/%YT%H:%M")
            else:
                dt = datetime.strptime(s, "%d/%m/%YT%H:%M:%S")
        except ValueError as e:
            _fail_422(f"start_time inválido (DD/MM/YYYY): {e}")
        dt = dt.replace(tzinfo=BH_TZ)
        return dt.isoformat()

    # Último fallback: tentar ISO direto
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BH_TZ)
        return dt.isoformat()
    except ValueError:
        _fail_422(f"start_time inválido (formato não reconhecido): {s}")


# ---------- Middleware de log ----------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    try:
        body = await request.body()
        logger.info(f"request path={request.url.path} content-type={request.headers.get('content-type')} body_len={len(body)}")
    except Exception:
        pass
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/booking-created")
async def booking_created(request: Request):
    data = await request.json()

    logger.info(f"version={APP_VERSION} booking-created keys={list(data.keys())}")

    # booking_id opcional -> gera automaticamente
    booking_id = (data.get("booking_id") or data.get("id") or "").strip()
    if not booking_id:
        booking_id = str(uuid.uuid4())

    client_name = data.get("client_name") or data.get("name") or "Cliente"
    service_name = data.get("service") or data.get("servico") or ""

    raw_start = data.get("start_time") or data.get("start")
    raw_end = data.get("end_time") or data.get("end")
    client_phone = data.get("phone") or data.get("client_phone") or data.get("telefone")

    if not raw_start:
        raise HTTPException(status_code=400, detail="start_time ausente (ou start)")
    if not client_phone:
        raise HTTPException(status_code=400, detail="phone ausente (ou client_phone/telefone)")

    logger.info(f"booking-created start_time_raw={raw_start}")

    try:
        start_time = normalize_to_rfc3339(raw_start)
        logger.info(f"booking-created normalize_ok start_time={start_time}")
    except HTTPException as e:
        logger.info(f"booking-created normalize_error={e.detail}")
        raise

    # end_time opcional: calcula se não vier
    if raw_end:
        end_time = normalize_to_rfc3339(raw_end)
    else:
        duration = calc_duration_min(service_name)
        dt_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        dt_end = dt_start + timedelta(minutes=duration)
        end_time = dt_end.isoformat()

    # salvar no banco (BH)
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
