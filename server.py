import os
import sys
import logging
import asyncio
import tempfile
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

@app.get("/")
def health_check():
    return {"status": "Encore OS Secondary Brain Server is Online & Active 🚀"}

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 *Encore OS Secondary Brain Server Online*\n\n"
        "Send or auto-upload audio recordings (lectures, voice notes, conversations).\n"
        "I will transcribe them, extract key summaries, and store them securely.\n\n"
        "Ask me anything like: *'Summarize my lectures'* or *'What did I talk about today?'*",
        parse_mode="Markdown"
    )

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    audio_file = message.audio or message.voice or message.document
    
    if not audio_file:
        return

    status_msg = await message.reply_text("📥 *Receiving audio recording from Encore OS...*", parse_mode="Markdown")
    
    try:
        file = await context.bot.get_file(audio_file.file_id)
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp_file:
            await file.download_to_drive(tmp_file.name)
            tmp_path = tmp_file.name

        await status_msg.edit_text("⚡ *Transcribing audio with Whisper Turbo (Zero Data Retention)...*", parse_mode="Markdown")

        with open(tmp_path, "rb") as file_obj:
            transcription = groq_client.audio.transcriptions.create(
                file=(tmp_path, file_obj.read()),
                model="whisper-large-v3-turbo",
                response_format="text"
            )

        if os.path.exists(tmp_path):
            os.remove(tmp_path)

        await status_msg.edit_text("🧠 *Generating AI Summary & Action Items...*", parse_mode="Markdown")

        prompt = f"""You are a personal secondary brain AI assistant.
Analyze the following audio transcript of a lecture or personal conversation:

--- TRANSCRIPT ---
{transcription}
--- END TRANSCRIPT ---

Generate a structured note:
1. 📌 **Main Title & Core Topic**
2. 💡 **Key Takeaways & Summary (Bullet points)**
3. 🎯 **Action Items / Study Notes**

Keep it clean, concise, and easy to read."""

        completion = groq_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )

        ai_summary = completion.choices[0].message.content

        full_response = f"✅ *Recording Processed Successfully*\n\n{ai_summary}\n\n---\n📝 *Transcript Snippet*:\n_{transcription[:500]}..._"
        await status_msg.edit_text(full_response, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error processing audio: {e}")
        await status_msg.edit_text(f"❌ *Error processing recording*: {str(e)}", parse_mode="Markdown")

async def run_bot():
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
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
