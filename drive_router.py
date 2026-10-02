"""
Telegram Drive REST Router for Encore Secondary Brain Cloud Backend.
Provides cloud drive endpoints (/api/drive/upload, /api/drive/files, /api/drive/stream/{file_id}).
Powered by Telegram Bot API.
"""

import os
import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/drive", tags=["Telegram Drive"])

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

@router.post("/upload")
async def upload_to_telegram_drive(
    file: UploadFile = File(...),
    virtual_folder: str = Form("/General")
):
    if not BOT_TOKEN or not CHAT_ID:
        raise HTTPException(status_code=500, detail="Telegram BOT_TOKEN or CHAT_ID not configured on server.")

    contents = await file.read()
    files = {
        "document": (file.filename, contents, file.content_type or "application/octet-stream")
    }
    data = {
        "chat_id": CHAT_ID,
        "caption": f"📁 Telegram-Drive: `{virtual_folder}/{file.filename}`\n📊 Size: {len(contents)} bytes",
        "parse_mode": "Markdown"
    }

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, data=data, files=files)
        res_json = resp.json()

        if not res_json.get("ok"):
            raise HTTPException(status_code=400, detail=f"Telegram upload failed: {res_json}")

        doc = res_json.get("result", {}).get("document", {})
        file_id = doc.get("file_id", "")
        message_id = res_json.get("result", {}).get("message_id")

        # Resolve Direct Streaming Link
        stream_url = None
        get_file_resp = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}")
        gf_json = get_file_resp.json()
        if gf_json.get("ok"):
            file_path = gf_json.get("result", {}).get("file_path", "")
            stream_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

        return JSONResponse(content={
            "status": "success",
            "file_name": file.filename,
            "virtual_folder": virtual_folder,
            "size_bytes": len(contents),
            "telegram_file_id": file_id,
            "message_id": message_id,
            "stream_url": stream_url
        })

@router.get("/stream/{file_id}")
async def get_stream_url(file_id: str):
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="Telegram BOT_TOKEN not configured.")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        res_json = resp.json()

        if not res_json.get("ok"):
            raise HTTPException(status_code=404, detail="File ID not found on Telegram servers.")

        file_path = res_json.get("result", {}).get("file_path", "")
        direct_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

        return JSONResponse(content={
            "file_id": file_id,
            "direct_stream_url": direct_url,
            "file_size": res_json.get("result", {}).get("file_size")
        })
