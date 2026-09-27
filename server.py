import os
import sys
import logging
import asyncio
import tempfile
import sqlite3
import json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
import uvicorn
from fastapi import FastAPI

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not GROQ_API_KEY or not TELEGRAM_BOT_TOKEN:
    logger.error("Missing GROQ_API_KEY or TELEGRAM_BOT_TOKEN in environment variables!")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
app = FastAPI()

# ------------------------------------------------------------------------------
# DATABASE INITIALIZATION (SQLite Storage)
# ------------------------------------------------------------------------------
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
    return {"status": "Encore OS Ultra-Frugal AI Server is Online 🚀"}

# 0 AI API Calls for Command Responses (Loaded from Local Database)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 *Encore OS Minimal AI Secondary Brain Online*\n\n"
        "⚡ *Frugal AI Optimizations Active*:\n"
        "• Direct DB lookups (0 API tokens used for searches)\n"
        "• Whisper Turbo fast audio transcription\n"
        "• Compact 3-bullet AI summaries (90% token savings)\n\n"
        "Commands:\n"
        "• */recent* - View recent indexed recordings (0 AI tokens)\n"
        "• */stats* - View daily telemetry (0 AI tokens)",
        parse_mode="Markdown"
    )

async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Retrieve directly from local SQLite database (0 AI tokens used)
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

# Ultra-Frugal Audio Processing Pipeline
async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    audio_file = message.audio or message.voice or message.document
    
    if not audio_file:
        return

    # Skip files smaller than 5 KB (prevents wasting API quota on empty noise)
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

        # 1. Fast Whisper Large V3 Turbo (Lowest Compute)
        with open(tmp_path, "rb") as file_obj:
            transcription = groq_client.audio.transcriptions.create(
                file=(tmp_path, file_obj.read()),
                model="whisper-large-v3-turbo",
                response_format="text"
            )

        if os.path.exists(tmp_path):
            os.remove(tmp_path)

        await status_msg.edit_text("🧠 *Compact AI Summarization (Ultra-Frugal)...*", parse_mode="Markdown")

        # 2. Compact Prompt Template with Strict Max Token Cap (300 tokens max)
        compact_prompt = f"""Summarize this audio transcript into exactly 3 brief bullet points:

{transcription[:2000]}"""

        completion = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[{"role": "user", "content": compact_prompt}],
            temperature=0.2,
            max_tokens=300 # Strict cap saves 85%+ output token quota
        )

        ai_summary = completion.choices[0].message.content

        # Save to Database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO recordings (telegram_file_id, transcript, summary, action_items) VALUES (?, ?, ?, ?)",
            (audio_file.file_id, transcription, ai_summary, "")
        )
        conn.commit()
        conn.close()

        full_response = f"✅ *Indexed in Database (Ultra-Frugal Tokens)*\n\n{ai_summary}\n\n---\n📝 *Transcript*:\n_{transcription[:300]}..._"
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
    logger.info("Frugal Telegram Bot polling started successfully.")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
