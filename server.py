import os
import sys
import logging
import asyncio
import tempfile
import sqlite3
import json
import re
import itertools
from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

raw_keys = os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY") or ""
GROQ_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN") or "HarshBrainSecretKey2026!#"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

key_cycle = itertools.cycle(GROQ_KEYS) if GROQ_KEYS else None

def get_groq_client():
    if not key_cycle:
        return None
    key = next(key_cycle)
    return Groq(api_key=key)

app = FastAPI(title="Secondary Brain 2.0 Mission Control Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.getcwd(), "secondary_brain.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS recordings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        telegram_file_id TEXT, 
        transcript TEXT, 
        summary TEXT, 
        action_items TEXT, 
        category TEXT DEFAULT 'NOTE',
        urgency TEXT DEFAULT 'MEDIUM',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS clipboard (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        content TEXT, 
        content_type TEXT, 
        category TEXT, 
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        device_id TEXT DEFAULT 'SB-HA77-2026',
        battery_level INTEGER DEFAULT 85,
        battery_charging INTEGER DEFAULT 0,
        battery_temp_celsius REAL DEFAULT 31.8,
        step_count INTEGER DEFAULT 5420, 
        location_name TEXT DEFAULT 'Kamakura HQ',
        latitude REAL DEFAULT 28.6139,
        longitude REAL DEFAULT 77.2090,
        wifi_ssid TEXT DEFAULT 'HomeNet_5G', 
        wifi_bssid TEXT DEFAULT 'C4:EA:1D:9A:88:2F',
        network_type TEXT DEFAULT 'WIFI',
        signal_strength_dbm INTEGER DEFAULT -52,
        ram_used_mb INTEGER DEFAULT 3450,
        ram_total_mb INTEGER DEFAULT 8000,
        storage_used_gb REAL DEFAULT 52.4,
        storage_total_gb REAL DEFAULT 256.0,
        cpu_temp_celsius REAL DEFAULT 36.5,
        screen_on_minutes INTEGER DEFAULT 168,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS dwell_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        location_label TEXT DEFAULT 'Current Location',
        latitude REAL,
        longitude REAL,
        accuracy_meters REAL DEFAULT 15.0,
        dwell_minutes INTEGER DEFAULT 10,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS command_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT,
        status TEXT DEFAULT 'SUCCESS',
        response TEXT,
        issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS bot_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    # Seed initial telemetry & dwell log
    cursor.execute("SELECT COUNT(*) FROM telemetry")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''INSERT INTO telemetry (
            device_id, battery_level, battery_charging, battery_temp_celsius,
            step_count, location_name, latitude, longitude, wifi_ssid, wifi_bssid, network_type,
            signal_strength_dbm, ram_used_mb, ram_total_mb, storage_used_gb,
            storage_total_gb, cpu_temp_celsius, screen_on_minutes
        ) VALUES (
            'SB-HA77-2026', 85, 0, 31.8, 5420, 'Kamakura Station Area', 28.6139, 77.2090, 'HomeNet_5G',
            'C4:EA:1D:9A:88:2F', 'WIFI', -52, 3450, 8000, 52.4, 256.0, 36.5, 168
        )''')
        
    cursor.execute("SELECT COUNT(*) FROM dwell_logs")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''INSERT INTO dwell_logs (
            location_label, latitude, longitude, accuracy_meters, dwell_minutes
        ) VALUES ('Kamakura GPS Fix', 28.6139, 77.2090, 8.5, 45)''')
        
    conn.commit()
    conn.close()

init_db()

def get_active_chat_id():
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM bot_config WHERE key='active_chat_id'")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_active_chat_id(chat_id: str):
    global TELEGRAM_CHAT_ID
    TELEGRAM_CHAT_ID = str(chat_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES ('active_chat_id', ?)", (str(chat_id),))
    conn.commit()
    conn.close()

# ─── REST Endpoints ──────────────────────────────────────────────────────────

@app.get("/")
def health_check():
    return {
        "status": "Encore OS Server Online 🚀",
        "active_api_keys": len(GROQ_KEYS),
        "keep_alive": "Active",
        "chat_connected": bool(get_active_chat_id()),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/device/health")
def get_device_health():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    
    if not row:
        return {"success": True, "data": {}}
    
    d = dict(row)
    return {
        "success": True,
        "data": {
            "battery_level": d.get("battery_level", 85),
            "battery_charging": bool(d.get("battery_charging", 0)),
            "battery_temp_celsius": d.get("battery_temp_celsius", 31.8),
            "step_count_today": d.get("step_count", 5420),
            "wifi_bssid": d.get("wifi_bssid", "C4:EA:1D:9A:88:2F"),
            "wifi_ssid": d.get("wifi_ssid", "HomeNet_5G"),
            "network_type": d.get("network_type", "WIFI"),
            "signal_strength_dbm": d.get("signal_strength_dbm", -52),
            "ram_used_mb": d.get("ram_used_mb", 3450),
            "ram_total_mb": d.get("ram_total_mb", 8000),
            "storage_used_gb": d.get("storage_used_gb", 52.4),
            "storage_total_gb": d.get("storage_total_gb", 256.0),
            "cpu_temp_celsius": d.get("cpu_temp_celsius", 36.5),
            "screen_on_minutes_today": d.get("screen_on_minutes", 168),
            "last_updated": d.get("timestamp", datetime.utcnow().isoformat())
        }
    }

@app.get("/device/profile")
def get_device_profile():
    return {
        "success": True,
        "data": {
            "device_id": "SB-HA77-2026",
            "device_name": "Secondary Brain Node 1",
            "android_version": "Android 14 (HyperOS)",
            "manufacturer": "Secondary Brain Hardware",
            "model": "Master Unit",
            "last_seen": datetime.utcnow().isoformat(),
            "registered_at": "2026-09-27T00:00:00Z"
        }
    }

@app.get("/device/location")
def get_device_location():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM dwell_logs ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    
    if not row:
        return {
            "success": True,
            "data": {
                "latitude": 28.6139,
                "longitude": 77.2090,
                "accuracy_meters": 10.0,
                "location_label": "Kamakura Crossing Area",
                "google_maps_url": "https://www.google.com/maps?q=28.6139,77.2090",
                "timestamp": datetime.utcnow().isoformat()
            }
        }
    
    d = dict(row)
    lat = d.get("latitude", 28.6139)
    lon = d.get("longitude", 77.2090)
    return {
        "success": True,
        "data": {
            "latitude": lat,
            "longitude": lon,
            "accuracy_meters": d.get("accuracy_meters", 12.0),
            "location_label": d.get("location_label", "Extracted Location"),
            "google_maps_url": f"https://www.google.com/maps?q={lat},{lon}",
            "timestamp": d.get("timestamp", datetime.utcnow().isoformat())
        }
    }

@app.get("/voice-tasks")
def get_voice_tasks(limit: int = 20, offset: int = 0):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM recordings ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    rows = c.fetchall()
    conn.close()
    
    tasks = []
    for r in rows:
        d = dict(r)
        tasks.append({
            "id": d["id"],
            "raw_text": d.get("transcript") or "",
            "category": d.get("category") or "NOTE",
            "urgency": d.get("urgency") or "MEDIUM",
            "parsed_title": (d.get("summary") or "Voice Note").split("\n")[0][:60],
            "completed": False,
            "groq_transcription": d.get("transcript") or "",
            "ai_summary": d.get("summary") or "",
            "created_at": d.get("created_at") or datetime.utcnow().isoformat()
        })
        
    return {"success": True, "data": tasks}

@app.get("/summary/daily")
def get_daily_summary():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT step_count, screen_on_minutes FROM telemetry ORDER BY id DESC LIMIT 1")
    t_row = c.fetchone()
    c.execute("SELECT COUNT(*) FROM recordings")
    rec_count = c.fetchone()[0]
    conn.close()
    
    steps = t_row[0] if t_row else 5420
    screen_mins = t_row[1] if t_row else 168
    
    return {
        "success": True,
        "data": {
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "steps": steps,
            "screen_time_minutes": screen_mins,
            "sleep_hours": 7.5,
            "voice_notes_count": rec_count,
            "top_app": "Focus Tracker",
            "productivity_score": 82
        }
    }

class CommandRequest(BaseModel):
    command: str
    args: Optional[Dict[str, Any]] = None

@app.post("/c2/command")
async def dispatch_c2_command(req: CommandRequest):
    cmd = req.command
    target_chat = get_active_chat_id()
    
    telegram_sent = False
    telegram_note = "Chat ID pending. Send /start to bot in Telegram to link your account."
    
    if bot_app and target_chat:
        try:
            await bot_app.bot.send_message(
                chat_id=target_chat,
                text=f"⚡ *Mission Control C2 Dispatch*\n\nCommand: `{cmd}`\nTimestamp: `{datetime.utcnow().strftime('%H:%M:%S UTC')}`",
                parse_mode="Markdown"
            )
            telegram_sent = True
            telegram_note = f"Sent to Telegram Chat ID {target_chat}"
        except Exception as e:
            telegram_note = f"Telegram send error: {str(e)}"
            logger.warning(telegram_note)

    # Log command in SQLite database
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO command_logs (command, status, response) VALUES (?, ?, ?)", 
              (cmd, "SENT" if telegram_sent else "LOGGED", telegram_note))
    conn.commit()
    conn.close()
            
    return {
        "success": True,
        "data": {
            "command": cmd,
            "success": True,
            "telegram_sent": telegram_sent,
            "message": telegram_note,
            "timestamp": datetime.utcnow().isoformat()
        }
    }

@app.get("/c2/latest-response")
def get_latest_c2_response():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM command_logs ORDER BY id DESC LIMIT 5")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"success": True, "data": rows}

@app.get("/db/table/{table_name}")
def get_db_table(table_name: str, page: int = Query(0), page_size: int = Query(25)):
    real_table = table_name
    if table_name == "voice_tasks":
        real_table = "recordings"
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (real_table,))
    if not c.fetchone():
        conn.close()
        return {"success": True, "data": {"rows": [], "total": 0}}
        
    c.execute(f"SELECT COUNT(*) FROM {real_table}")
    total = c.fetchone()[0]
    
    c.execute(f"SELECT * FROM {real_table} ORDER BY 1 DESC LIMIT ? OFFSET ?", (page_size, page * page_size))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    
    return {
        "success": True,
        "data": {
            "rows": rows,
            "total": total
        }
    }

# ─── Telegram Bot Logic ───────────────────────────────────────────────────────
bot_app = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    msg = f"🧠 *Encore OS Secondary Brain Online*\n\n✅ *Linked with Mission Control!* (Chat ID: `{chat_id}`)\n\n⚡ Features:\n• Send voice notes to get instant Groq Whisper transcription\n• Send GPS location to record live coordinates\n• Commands: `/locate`, `/status`, `/recent`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, summary, created_at FROM recordings ORDER BY id DESC LIMIT 3")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📭 No recordings indexed yet.")
        return
    msg = "📝 *Recent Recordings*:\n\n"
    for r in rows:
        summary_preview = r[1][:150] if r[1] else "No summary"
        msg += f"• *ID #{r[0]}* ({r[2]}):\n{summary_preview}...\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_location_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    
    loc = update.message.location
    if not loc:
        return
        
    lat = loc.latitude
    lon = loc.longitude
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO dwell_logs (location_label, latitude, longitude, accuracy_meters) VALUES (?, ?, ?, ?)",
              ("Telegram Live Pin", lat, lon, 10.0))
    c.execute("UPDATE telemetry SET latitude = ?, longitude = ?, location_name = 'Telegram Live Pin' WHERE id = (SELECT MAX(id) FROM telemetry)",
              (lat, lon))
    conn.commit()
    conn.close()
    
    maps_link = f"https://www.google.com/maps?q={lat},{lon}"
    await update.message.reply_text(
        f"📍 *Location Indexed in Mission Control*\n\n• *Coordinates*: `{lat:.5f}, {lon:.5f}`\n• [Open in Google Maps]({maps_link})",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    
    text = update.message.text or ""
    
    # Check if user sent coordinates like "28.6139, 77.2090"
    coord_match = re.search(r'(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)', text)
    if coord_match:
        lat = float(coord_match.group(1))
        lon = float(coord_match.group(2))
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO dwell_logs (location_label, latitude, longitude, accuracy_meters) VALUES ('Telegram Text Coords', ?, ?, 15.0)", (lat, lon))
        c.execute("UPDATE telemetry SET latitude = ?, longitude = ? WHERE id = (SELECT MAX(id) FROM telemetry)", (lat, lon))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"📍 Extracted coordinates: `{lat:.5f}, {lon:.5f}` $\\rightarrow$ Synced to Mission Control Database!")
        return

    # Store text note in clipboard table
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO clipboard (content, content_type, category) VALUES (?, 'text', 'TELEGRAM_NOTE')", (text,))
    conn.commit()
    conn.close()
    await update.message.reply_text("📋 Note saved to Secondary Brain database.", parse_mode="Markdown")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    
    message = update.message
    audio_file = message.audio or message.voice or message.document
    if not audio_file:
        return
    if hasattr(audio_file, 'file_size') and audio_file.file_size and audio_file.file_size < 5000:
        await message.reply_text("⚠️ Skipping empty audio snippet (<5KB) to save API quota.")
        return
    status_msg = await message.reply_text("📥 *Receiving recording...*", parse_mode="Markdown")
    try:
        file = await context.bot.get_file(audio_file.file_id)
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp_file:
            await file.download_to_drive(tmp_file.name)
            tmp_path = tmp_file.name
        await status_msg.edit_text("⚡ *Minimal Whisper Turbo Transcription...*", parse_mode="Markdown")
        transcription = None
        for attempt in range(len(GROQ_KEYS) or 1):
            try:
                client = get_groq_client()
                with open(tmp_path, "rb") as file_obj:
                    transcription = client.audio.transcriptions.create(
                        file=(tmp_path, file_obj.read()),
                        model="whisper-large-v3-turbo",
                        response_format="text"
                    )
                break
            except Exception as err:
                logger.warning(f"Key attempt {attempt+1} failed: {err}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if not transcription:
            raise Exception("All Groq API keys exhausted or rate-limited.")
        await status_msg.edit_text("🧠 *Generating AI Summary...*", parse_mode="Markdown")
        snippet_text = transcription[:8000]
        compact_prompt = f"Analyze transcript:\n{snippet_text}\n\nExtract 3 brief bullet points and main action items."
        ai_summary = None
        for attempt in range(len(GROQ_KEYS) or 1):
            try:
                client = get_groq_client()
                completion = client.chat.completions.create(
                    model="qwen/qwen3.8-27b",
                    messages=[{"role": "user", "content": compact_prompt}],
                    temperature=0.2,
                    max_tokens=450
                )
                ai_summary = completion.choices[0].message.content
                break
            except Exception as err:
                logger.warning(f"Key attempt {attempt+1} failed: {err}")
        if not ai_summary:
            ai_summary = "Summary unavailable. Full transcript indexed below."
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO recordings (telegram_file_id, transcript, summary, action_items) VALUES (?, ?, ?, ?)", (audio_file.file_id, transcription, ai_summary, ""))
        conn.commit()
        conn.close()
        preview = transcription[:400]
        full_response = f"✅ *Indexed in Secondary Brain DB*\n\n{ai_summary}\n\n---\n📝 *Transcript Snippet*:\n_{preview}..._"
        await status_msg.edit_text(full_response, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error processing audio: {e}")
        await status_msg.edit_text(f"❌ *Error processing recording*: {str(e)}", parse_mode="Markdown")

async def run_bot():
    global bot_app
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not provided; skipping bot polling.")
        return
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("recent", recent_command))
    bot_app.add_handler(MessageHandler(filters.LOCATION, handle_location_message))
    bot_app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.ALL, handle_audio))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Telegram Bot polling started with auto-chat linking and location parser.")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
