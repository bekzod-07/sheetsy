import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, PlainTextResponse

from db import init_db
from bot import create_bot_dp, setup_commands
from google_oauth import build_flow, verify_state, save_token_for_tg

import json

from db import get_last_file_id_by_tg
from google_oauth import get_credentials_for_tg
from googleapiclient.discovery import build

app = FastAPI(title="Sheetsy Bot Backend")

@app.get("/")
async def root():
    return {"ok": True}

@app.get("/auth/start")
async def auth_start(request: Request):
    state_param = request.query_params.get("state")
    if not state_param:
        raise HTTPException(400, "state is required")
    _ = verify_state(state_param)
    flow = build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state_param,
    )
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    params = dict(request.query_params)
    if "error" in params:
        return PlainTextResponse("Authorization failed: " + params["error"], status_code=400)
    state = params.get("state")
    code = params.get("code")
    if not state or not code:
        raise HTTPException(400, "missing params")

    tg_id = verify_state(state)
    flow = build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    await save_token_for_tg(tg_id, creds)

    try:
        bot, _ = create_bot_dp()
        await bot.send_message(tg_id, "✅ Google bilan ulandingiz! /mysheets ni sinab ko'ring.")
    except Exception:
        pass

    return PlainTextResponse("Ulanish muvaffaqiyatli. Telegram’ga qayting.")

# app.py (mavjud faylingizga qo'shing)
@app.post("/webhooks/aisha-stt")
async def aisha_stt_webhook(request: Request):
    tg_id = int(request.query_params.get("tg_id", "0"))
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        payload = {}

    # STT servisidan keladigan matn maydonini moslang
    text = payload.get("text") or payload.get("result") or payload.get("transcript") or ""

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
                updated_range = (resp.get("updates") or {}).get("updatedRange", "A?")

                # foydalanuvchiga xabar
                from bot import create_bot_dp
                bot, _ = create_bot_dp()
                await bot.send_message(
                    tg_id,
                    f"✅ STT tayyor, A ustunga qo‘shildi!\nA: `{text}`\nJoylashuvi: `{updated_range}`",
                    parse_mode="Markdown"
                )
            except Exception as e:
                try:
                    from bot import create_bot_dp
                    bot, _ = create_bot_dp()
                    await bot.send_message(tg_id, f"⚠️ STT natijasini qo‘shishda xatolik: {e}")
                except Exception:
                    pass

    return PlainTextResponse("ok")


async def main():
    await init_db()
    bot, dp = create_bot_dp()
    await setup_commands(bot)

    import uvicorn
    api_task = asyncio.create_task(
        uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=8000, loop="asyncio")
        ).serve()
    )
    bot_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    await asyncio.gather(api_task, bot_task)


if __name__ == "__main__":
    asyncio.run(main())
