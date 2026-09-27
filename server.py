import os
import sys
import logging
import asyncio
import tempfile
import sqlite3
import json
import itertools
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
import uvicorn
from fastapi import FastAPI

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Parse single or multiple comma-separated Groq API keys
raw_keys = os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY") or ""
GROQ_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not GROQ_KEYS or not TELEGRAM_BOT_TOKEN:
    logger.error("Missing GROQ_API_KEY(S) or TELEGRAM_BOT_TOKEN in environment variables!")

# Create a round-robin cycle of Groq Clients
key_cycle = itertools.cycle(GROQ_KEYS) if GROQ_KEYS else None

def get_groq_client():
    if not key_cycle:
        return None
    key = next(key_cycle)
    return Groq(api_key=key)

app = FastAPI()

DB_PATH = os.path.join(os.getcwd(), "secondary_brain.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_file_id TEXT,
            transcript TEXT,
            summary TEXT,
            action_items TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clipboard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            content_type TEXT,
            category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            step_count INTEGER,
            location_name TEXT,
            wifi_ssid TEXT,
            focus_minutes INTEGER,
            battery_level INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL,
            category TEXT,
            merchant TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.get("/")
def health_check():
    return {
        "status": "Encore OS Server Online 🚀",
        "active_api_keys": len(GROQ_KEYS),
        "multi_key_rotation": "Enabled" if len(GROQ_KEYS) > 1 else "Single Key Active"
    }

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🧠 *Encore OS Multi-Key Secondary Brain Online*\n\n"
        f"🔑 *Active Groq Keys*: {len(GROQ_KEYS)}\n"
        f"⚡ Automatic Key Rotation & Failover Enabled.\n\n"
        f"Commands:\n"
        f"• */recent* - View recent indexed recordings (0 AI tokens)\n"
        f"• */stats* - View daily telemetry (0 AI tokens)",
        parse_mode="Markdown"
    )

async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, summary, created_at FROM recordings ORDER BY id DESC LIMIT 3")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await update.message.reply_text("📭 No recordings indexed yet.")
        return
        
    msg = "📝 *Recent Recordings (0 AI Tokens Used)*:\n\n"
    for r in rows:
        msg += f"• *ID #{r[0]}* ({r[2]}):\n{r[1][:150]}...\n\n"
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

        # Key Failover Execution for Transcription
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
                logger.warning(f"Key attempt {attempt+1} failed, trying next key... ({err})")

        if os.path.exists(tmp_path):
            os.remove(tmp_path)

        if not transcription:
            raise Exception("All Groq API keys exhausted or rate-limited.")

        await status_msg.edit_text("🧠 *Compact AI Summarization...*", parse_mode="Markdown")

        compact_prompt = f"""Summarize this audio transcript into exactly 3 brief bullet points:

{transcription[:2000]}"""

        # Key Failover Execution for Summarization
        ai_summary = None
        for attempt in range(len(GROQ_KEYS) or 1):
            try:
                client = get_groq_client()
                completion = client.chat.completions.create(
                    model="qwen/qwen3.8-27b",
                    messages=[{"role": "user", "content": compact_prompt}],
                    temperature=0.2,
                    max_tokens=300
                )
                ai_summary = completion.choices[0].message.content
                break
            except Exception as err:
                logger.warning(f"Key attempt {attempt+1} failed for completion... ({err})")

        if not ai_summary:
            ai_summary = f"Summary unavailable (Rate limited). Full transcript preserved below."

        # Save to Database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO recordings (telegram_file_id, transcript, summary, action_items) VALUES (?, ?, ?, ?)",
            (audio_file.file_id, transcription, ai_summary, "")
        )
        conn.commit()
        conn.close()

        full_response = f"✅ *Indexed in Database (Multi-Key Active)*\n\n{ai_summary}\n\n---
📝 *Transcript*:\n_{transcription[:300]}..._"
        await status_msg.edit_text(full_response, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error processing audio: {e}")
        await status_msg.edit_text(f"❌ *Error processing recording*: {str(e)}", parse_mode="Markdown")

async def run_bot():
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("recent", recent_command))
    bot_app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.ALL, handle_audio))
    
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Multi-Key Telegram Bot polling started successfully.")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
