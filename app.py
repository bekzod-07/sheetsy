import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse

from db import get_last_file_id_by_tg
from google_oauth import (
    build_flow,
    verify_state,
    save_token_for_tg,
    get_credentials_for_tg
)
from googleapiclient.discovery import build
from bot_factory import create_bot_dp

app = FastAPI(title="Sheetsy Backend")


@app.get("/")
async def root():
    return {"ok": True, "domain": "sifatcall.uz"}


@app.get("/auth/start")
async def auth_start(request: Request):
    state_param = request.query_params.get("state")
    if not state_param:
        raise HTTPException(400, "state missing")

    _ = verify_state(state_param)

    flow = build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes=True,
        prompt="consent",
        state=state_param,
    )

    return RedirectResponse(auth_url)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    params = dict(request.query_params)

    if "error" in params:
        return PlainTextResponse(
            "Authorization failed: " + params["error"],
            status_code=400,
        )

    state = params.get("state")
    code = params.get("code")

    tg_id = verify_state(state)

    flow = build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    await save_token_for_tg(tg_id, creds)

    # foydalanuvchiga xabar beramiz
    try:
        bot, _ = create_bot_dp()
        await bot.send_message(tg_id, "✅ Google bilan ulandingiz!\n/mysheets ni sinab ko‘ring.")
    except:
        pass

    return PlainTextResponse("Ulanish muvaffaqiyatli!")


@app.post("/webhooks/aisha-stt")
async def aisha_stt(request: Request):
    tg_id = int(request.query_params.get("tg_id", "0"))
    raw = await request.body()

    try:
        data = json.loads(raw.decode())
    except:
        data = {}

    text = data.get("text") or data.get("transcript") or data.get("result") or ""

    if tg_id and text:
        file_id = await get_last_file_id_by_tg(tg_id)

        if file_id:
            try:
                creds = await get_credentials_for_tg(tg_id)
                sheets = build("sheets", "v4", credentials=creds)

                resp = sheets.spreadsheets().values().append(
                    spreadsheetId=file_id,
                    range="A:A",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [[text]]}
                ).execute()

                bot, _ = create_bot_dp()
                await bot.send_message(
                    tg_id,
                    f"✅ Matn qo‘shildi:\n{text}",
                )
            except Exception as e:
                bot, _ = create_bot_dp()
                await bot.send_message(tg_id, f"Xatolik: {e}")

    return "ok"
