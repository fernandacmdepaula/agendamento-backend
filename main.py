from datetime import datetime, timedelta, timezone

BH_TZ = timezone(timedelta(hours=-3))

def normalize_dt(dt_str: str) -> str:
    """
    Aceita:
      - 'YYYY-MM-DD HH:MM:SS'
      - 'YYYY-MM-DDTHH:MM:SS'
      - já com timezone
    Retorna sempre RFC3339 com timezone (-03:00) se não vier.
    """
    s = (dt_str or "").strip()
    if not s:
        return ""

    # Se vier com espaço, troca por T
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)

    # Se já tem timezone (Z ou +hh:mm ou -hh:mm), devolve como está
    if s.endswith("Z") or ("+" in s[10:] or "-" in s[10:]):
        return s

    # Não tem timezone: assume BH (-03:00)
    # Tenta parse com segundos
    try:
        dt = datetime.fromisoformat(s)  # aceita 'YYYY-MM-DDTHH:MM:SS'
    except ValueError:
        # fallback sem segundos
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")

    dt = dt.replace(tzinfo=BH_TZ)
    return dt.isoformat()

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
    # padrão
    return 40


@app.post("/booking-created")
async def booking_created(request: Request):
    data = await request.json()

    booking_id = data.get("booking_id") or data.get("id")
    client_name = data.get("client_name") or data.get("name") or "Cliente"
    service_name = data.get("service") or data.get("servico") or ""

    raw_start = data.get("start_time") or data.get("start")
    raw_end = data.get("end_time") or data.get("end")

    client_phone = data.get("phone") or data.get("client_phone") or data.get("telefone")

    if not booking_id:
        raise HTTPException(status_code=400, detail="booking_id ausente")
    if not raw_start:
        raise HTTPException(status_code=400, detail="start_time ausente (ou start)")

    # Normaliza start
    start_time = normalize_dt(raw_start)
    if not start_time:
        raise HTTPException(status_code=400, detail="start_time inválido")

    # Se não vier end_time, calcula automaticamente
    if raw_end:
        end_time = normalize_dt(raw_end)
        if not end_time:
            raise HTTPException(status_code=400, detail="end_time inválido")
    else:
        # calcula end = start + duração
        dt_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        duration = calc_duration_min(service_name)
        dt_end = dt_start + timedelta(minutes=duration)
        end_time = dt_end.isoformat()

    # Para salvar no banco (texto simples)
    start_at = start_time.replace("T", " ").split("+")[0].split("-03:00")[0].split("Z")[0]
    start_date = start_at.split(" ")[0] if " " in start_at else start_at

    # 1) cria no Google Calendar
    service = get_google_service()

    event = {
        "summary": f"[ZAIA] {client_name} ({booking_id})",
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
        "description": data.get("notes", ""),
    }

    try:
        created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    except Exception as e:
        # devolve erro mais claro ao invés de 500 “seco”
        raise HTTPException(status_code=400, detail=f"Erro ao criar no Google Calendar: {str(e)}")

    google_event_id = created.get("id")
    if not google_event_id:
        raise HTTPException(status_code=500, detail="Google não retornou o id do evento")

    # 2) salva no Postgres
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
