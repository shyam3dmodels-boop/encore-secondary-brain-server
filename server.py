import os
import sys
import logging
import asyncio
import tempfile
import sqlite3
import json
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

# Enable CORS for web dashboards
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
        device_id TEXT DEFAULT 'SB-A1B2-C3D4',
        battery_level INTEGER DEFAULT 80,
        battery_charging INTEGER DEFAULT 0,
        battery_temp_celsius REAL DEFAULT 32.5,
        step_count INTEGER DEFAULT 0, 
        location_name TEXT DEFAULT 'Home',
        latitude REAL DEFAULT 28.6139,
        longitude REAL DEFAULT 77.2090,
        wifi_ssid TEXT DEFAULT 'WiFi-Connected', 
        wifi_bssid TEXT DEFAULT 'AA:BB:CC:DD:EE:FF',
        network_type TEXT DEFAULT 'WIFI',
        signal_strength_dbm INTEGER DEFAULT -55,
        ram_used_mb INTEGER DEFAULT 3200,
        ram_total_mb INTEGER DEFAULT 6000,
        storage_used_gb REAL DEFAULT 48.0,
        storage_total_gb REAL DEFAULT 128.0,
        cpu_temp_celsius REAL DEFAULT 38.0,
        screen_on_minutes INTEGER DEFAULT 0,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        amount REAL, 
        category TEXT, 
        merchant TEXT, 
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS command_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT,
        status TEXT DEFAULT 'SUCCESS',
        response TEXT,
        issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Insert initial telemetry seed if table is empty
    cursor.execute("SELECT COUNT(*) FROM telemetry")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''INSERT INTO telemetry (
            device_id, battery_level, battery_charging, battery_temp_celsius,
            step_count, location_name, wifi_ssid, wifi_bssid, network_type,
            signal_strength_dbm, ram_used_mb, ram_total_mb, storage_used_gb,
            storage_total_gb, cpu_temp_celsius, screen_on_minutes
        ) VALUES (
            'SB-HA77-2026', 85, 0, 31.8, 5420, 'Kamakura HQ', 'HomeNet_5G',
            'C4:EA:1D:9A:88:2F', 'WIFI', -52, 3450, 8000, 52.4, 256.0, 36.5, 168
        )''')
    
    conn.commit()
    conn.close()

init_db()

# ─── Auth Dependency ─────────────────────────────────────────────────────────
def verify_admin(x_admin_token: Optional[str] = Header(None)):
    if x_admin_token and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid Admin Token")
    return True

# ─── REST Endpoints for Web Mission Control ──────────────────────────────────

@app.get("/")
def health_check():
    return {
        "status": "Encore OS Server Online 🚀",
        "active_api_keys": len(GROQ_KEYS),
        "keep_alive": "Active",
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
        return {
            "success": True,
            "data": {
                "battery_level": 85,
                "battery_charging": False,
                "battery_temp_celsius": 31.8,
                "step_count_today": 5420,
                "wifi_bssid": "C4:EA:1D:9A:88:2F",
                "wifi_ssid": "HomeNet_5G",
                "network_type": "WIFI",
                "signal_strength_dbm": -52,
                "ram_used_mb": 3450,
                "ram_total_mb": 8000,
                "storage_used_gb": 52.4,
                "storage_total_gb": 256.0,
                "cpu_temp_celsius": 36.5,
                "screen_on_minutes_today": 168,
                "last_updated": datetime.utcnow().isoformat()
            }
        }
    
    d = dict(row)
    return {
        "success": True,
        "data": {
            "battery_level": d.get("battery_level", 85),
            "battery_charging": bool(d.get("battery_charging", 0)),
            "battery_temp_celsius": d.get("battery_temp_celsius", 31.8),
            "step_count_today": d.get("step_count", 0),
            "wifi_bssid": d.get("wifi_bssid", "AA:BB:CC:DD:EE:FF"),
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
            "model": "Brain Master Unit",
            "last_seen": datetime.utcnow().isoformat(),
            "registered_at": "2026-09-27T00:00:00Z"
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
    
    # If empty, return a welcoming seed
    if not tasks:
        tasks = [
            {
                "id": 1,
                "raw_text": "Secondary Brain 2.0 system active and standing by for voice notes.",
                "category": "NOTE",
                "urgency": "LOW",
                "parsed_title": "System Active & Initialized",
                "completed": True,
                "groq_transcription": "Secondary Brain 2.0 system active and standing by for voice notes.",
                "ai_summary": "All systems operating normally on Render cloud node.",
                "created_at": datetime.utcnow().isoformat()
            }
        ]
        
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO command_logs (command, status, response) VALUES (?, ?, ?)", 
              (cmd, "SUCCESS", f"Dispatched {cmd} to Android agent"))
    conn.commit()
    conn.close()
    
    # If Telegram bot is active, broadcast command to user chat
    if bot_app and TELEGRAM_CHAT_ID:
        try:
            await bot_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚡ *C2 Remote Command Dispatched*: `{cmd}`",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not forward C2 command to Telegram: {e}")
            
    return {
        "success": True,
        "data": {
            "command": cmd,
            "success": True,
            "message": f"Command {cmd} dispatched successfully to device",
            "timestamp": datetime.utcnow().isoformat()
        }
    }

@app.get("/db/table/{table_name}")
def get_db_table(table_name: str, page: int = Query(0), page_size: int = Query(25)):
    allowed_tables = ["recordings", "clipboard", "telemetry", "expenses", "command_logs", "voice_tasks", "app_usage_logs", "device_profiles", "sleep_states", "dwell_logs"]
    
    # Map alias table names
    real_table = table_name
    if table_name == "voice_tasks":
        real_table = "recordings"
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Check table existence
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

class TelemetryPayload(BaseModel):
    device_id: Optional[str] = "SB-HA77-2026"
    battery_level: Optional[int] = 80
    battery_charging: Optional[bool] = False
    battery_temp_celsius: Optional[float] = 32.0
    step_count: Optional[int] = 0
    wifi_ssid: Optional[str] = "WiFi"
    wifi_bssid: Optional[str] = "00:00:00:00:00:00"
    network_type: Optional[str] = "WIFI"
    signal_strength_dbm: Optional[int] = -60
    ram_used_mb: Optional[int] = 3000
    ram_total_mb: Optional[int] = 6000
    storage_used_gb: Optional[float] = 40.0
    storage_total_gb: Optional[float] = 128.0
    cpu_temp_celsius: Optional[float] = 37.0
    screen_on_minutes: Optional[int] = 0

@app.post("/api/telemetry")
def ingest_telemetry(data: TelemetryPayload):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO telemetry (
        device_id, battery_level, battery_charging, battery_temp_celsius,
        step_count, wifi_ssid, wifi_bssid, network_type, signal_strength_dbm,
        ram_used_mb, ram_total_mb, storage_used_gb, storage_total_gb,
        cpu_temp_celsius, screen_on_minutes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
        data.device_id, data.battery_level, 1 if data.battery_charging else 0,
        data.battery_temp_celsius, data.step_count, data.wifi_ssid,
        data.wifi_bssid, data.network_type, data.signal_strength_dbm,
        data.ram_used_mb, data.ram_total_mb, data.storage_used_gb,
        data.storage_total_gb, data.cpu_temp_celsius, data.screen_on_minutes
    ))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Telemetry logged"}

# ─── Telegram Bot Logic ───────────────────────────────────────────────────────
bot_app = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "🧠 *Encore OS Secondary Brain Online*\n\n⚡ Web Mission Control Connected.\n\nCommands:\n• */recent* - View recent voice notes\n• */stats* - View daily telemetry"
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

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                logger.warning(f"Key attempt {attempt+1} failed for completion: {err}")
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
    bot_app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.ALL, handle_audio))
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Telegram Bot polling started successfully.")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
