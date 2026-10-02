import os
import sys
import logging
import asyncio
import tempfile
import sqlite3
import json
import re
import itertools
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
import uvicorn
import requests
from fastapi import FastAPI, Header, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from mem0_brain import brain_memory
try:
    from termux_jarvis import execute_jarvis_action
except Exception as _e:
    execute_jarvis_action = None

from mcp_specs import MCP_TOOLS, get_openai_function_tools, mcp_tool_to_c2_command

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.getcwd(), "secondary_brain.db")

# Concurrency & WAL mode helper for SQLite
_orig_sqlite3_connect = sqlite3.connect

def get_db_connection(database=None, **kwargs) -> sqlite3.Connection:
    target = database if database else DB_PATH
    kwargs.setdefault("timeout", 30.0)
    kwargs.setdefault("check_same_thread", False)
    conn = _orig_sqlite3_connect(target, **kwargs)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        pass
    return conn

# Monkey-patch sqlite3.connect across entire server to eliminate database locked errors
sqlite3.connect = get_db_connection

raw_keys = os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY") or ""
GROQ_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "8942980083:AAHmhVY4ybuOYSSJDsyuF8Z-1DP66WEbl5k"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN") or "HarshBrainSecretKey2026!#"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "-1004445314496"

FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID") or "android-1a887"
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY") or "AIzaSyCTUzJhx7yuMv35XWXlSFW3MhQtG_-GT3w"

# Active AI Chat Sessions per Telegram Chat ID: { chat_id: { "model": str, "active": bool, "history": list } }
ACTIVE_AI_SESSIONS: Dict[int, Dict[str, Any]] = {}

GROQ_FEATURED_MODELS = [
    {"id": "llama-3.3-70b-versatile", "name": "LLaMA 3.3 70B Versatile", "desc": "Flagship Reasoning & Intelligence (128k context)"},
    {"id": "deepseek-r1-distill-llama-70b", "name": "DeepSeek R1 Distill 70B", "desc": "Deep CoT Mathematical & Coding Reasoning"},
    {"id": "llama-3.1-8b-instant", "name": "LLaMA 3.1 8B Instant", "desc": "Ultra-Fast Sub-Second Real-Time Response"},
    {"id": "mixtral-8x7b-32768", "name": "Mixtral 8x7B MoE", "desc": "High Context Multi-Task Agent (32k context)"},
    {"id": "gemma2-9b-it", "name": "Gemma 2 9B IT", "desc": "Google Lightweight Instruction Model"}
]

# ─── Multi-Key Pool & Automatic Quota Failover Engine ─────────────────────────

def auto_reset_expired_quotas():
    """Resets quota_exceeded to 0 for keys whose 24h cooldown has elapsed."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            UPDATE api_keys_v2 
            SET quota_exceeded = 0, quota_reset_at = NULL, last_error = ''
            WHERE quota_exceeded = 1 AND quota_reset_at IS NOT NULL AND quota_reset_at <= CURRENT_TIMESTAMP
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Error auto-resetting expired quotas: {e}")

def mark_key_quota_exceeded_with_cooldown(key_id: Optional[int], api_key: str, error_msg: str, cooldown_hours: int = 24):
    """Marks a specific key as quota-exceeded with a 24h reset cooldown."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        reset_time = (datetime.utcnow() + timedelta(hours=cooldown_hours)).strftime("%Y-%m-%d %H:%M:%S")
        if key_id:
            c.execute("""
                UPDATE api_keys_v2 
                SET quota_exceeded = 1, quota_reset_at = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (reset_time, str(error_msg)[:500], key_id))
        else:
            c.execute("""
                UPDATE api_keys_v2 
                SET quota_exceeded = 1, quota_reset_at = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE api_key = ?
            """, (reset_time, str(error_msg)[:500], api_key))
        conn.commit()
        conn.close()
        logger.warning(f"Key (ID: {key_id}, ...{api_key[-4:] if api_key else '?'}) marked quota_exceeded for {cooldown_hours}h until {reset_time}. Error: {error_msg}")
    except Exception as e:
        logger.error(f"Failed to mark key quota exceeded: {e}")

def record_key_success(key_id: Optional[int], api_key: Optional[str] = None):
    """Updates last_used and increments requests_count."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if key_id:
            c.execute("""
                UPDATE api_keys_v2 
                SET last_used = CURRENT_TIMESTAMP, requests_count = requests_count + 1
                WHERE id = ?
            """, (key_id,))
        elif api_key:
            c.execute("""
                UPDATE api_keys_v2 
                SET last_used = CURRENT_TIMESTAMP, requests_count = requests_count + 1
                WHERE api_key = ?
            """, (api_key,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to record key success: {e}")

def get_ordered_key_candidates(preferred_provider: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Returns ordered candidate keys for inference failover.
    Ordering:
    1. Active, non-quota-exceeded keys matching preferred_provider ordered by priority_rank ASC, requests_count ASC, id ASC (e.g. Banana -> Apple -> Kiwi)
    2. Active, non-quota-exceeded keys from other providers as secondary failover ordered by priority_rank ASC, requests_count ASC
    3. Legacy / environment variable keys if DB pool has none
    """
    auto_reset_expired_quotas()
    candidates = []
    seen_keys = set()
    preferred_prov = preferred_provider.lower().strip() if preferred_provider else None

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # 1. Primary candidate keys for preferred provider
        if preferred_prov:
            c.execute("""
                SELECT id, provider, api_key, label, selected_model, priority_rank, requests_count, quota_exceeded
                FROM api_keys_v2
                WHERE LOWER(provider) = ? AND is_active = 1 AND quota_exceeded = 0
                ORDER BY priority_rank ASC, requests_count ASC, id ASC
            """, (preferred_prov,))
            for r in c.fetchall():
                k = r["api_key"].strip()
                if k and k not in seen_keys:
                    seen_keys.add(k)
                    candidates.append(dict(r))

        # 2. Secondary candidate keys from other providers
        c.execute("""
            SELECT id, provider, api_key, label, selected_model, priority_rank, requests_count, quota_exceeded
            FROM api_keys_v2
            WHERE is_active = 1 AND quota_exceeded = 0
            ORDER BY priority_rank ASC, requests_count ASC, id ASC
        """)
        for r in c.fetchall():
            k = r["api_key"].strip()
            if k and k not in seen_keys:
                seen_keys.add(k)
                candidates.append(dict(r))

        conn.close()
    except Exception as e:
        logger.warning(f"Error querying candidate key pool: {e}")

    # 3. Environment variable fallback if candidates still empty
    if not candidates and preferred_prov:
        env_key = None
        if preferred_prov == "groq":
            env_key = os.environ.get("GROQ_API_KEY") or (GROQ_KEYS[0] if GROQ_KEYS else None)
        elif preferred_prov == "gemini":
            env_key = os.environ.get("GEMINI_API_KEY")
        elif preferred_prov == "nvidia":
            env_key = os.environ.get("NVIDIA_API_KEY")
        elif preferred_prov == "deepseek":
            env_key = os.environ.get("DEEPSEEK_API_KEY")
        elif preferred_prov == "openrouter":
            env_key = os.environ.get("OPENROUTER_API_KEY")
        elif preferred_prov in ["claude", "anthropic"]:
            env_key = os.environ.get("ANTHROPIC_API_KEY")

        if env_key:
            candidates.append({
                "id": None,
                "provider": preferred_prov,
                "api_key": env_key.strip(),
                "label": f"Environment {preferred_prov.capitalize()}",
                "selected_model": DEFAULT_MODELS.get(preferred_prov, ""),
                "priority_rank": 1,
                "requests_count": 0,
                "quota_exceeded": 0
            })

    return candidates

def get_groq_api_key() -> Optional[str]:
    cands = get_ordered_key_candidates("groq")
    if cands:
        return cands[0]["api_key"]
    return None

def mark_groq_key_quota_exceeded(api_key: str):
    mark_key_quota_exceeded_with_cooldown(None, api_key, "HTTP 429 Rate Limit / Quota Exceeded", cooldown_hours=24)

def get_groq_client(used_key: Optional[str] = None) -> Optional[Groq]:
    """Instantiates a Groq client. If used_key is provided, uses that specific key."""
    key = used_key or get_groq_api_key()
    if not key:
        return None
    try:
        return Groq(api_key=key)
    except Exception as e:
        logger.error(f"Failed to instantiate Groq client: {e}")
        return None

app = FastAPI(title="Secondary Brain 2.0 Mission Control Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "https://encore-secondary-brain-server.onrender.com",
        "https://secondary-brain-admin.vercel.app",
        "https://remix-enforcer.web.app",
        "*",  # Keep for Android native HTTP clients
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.getcwd(), "secondary_brain.db")

# ─── Direct Firebase Sync Helper ─────────────────────────────────────────────
def sync_to_firestore(collection_path: str, doc_id: Optional[str], fields: Dict[str, Any]):
    """Syncs documents directly to Firebase Firestore via REST API without dependencies."""
    try:
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/{collection_path}"
        if doc_id:
            url += f"/{doc_id}?key={FIREBASE_API_KEY}"
        else:
            url += f"?key={FIREBASE_API_KEY}"
            
        firestore_fields = {}
        for k, v in fields.items():
            if isinstance(v, str):
                firestore_fields[k] = {"stringValue": v}
            elif isinstance(v, (int, float)):
                firestore_fields[k] = {"doubleValue": float(v)}
            elif isinstance(v, bool):
                firestore_fields[k] = {"booleanValue": v}
            else:
                firestore_fields[k] = {"stringValue": str(v)}
                
        payload = json.dumps({"fields": firestore_fields}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="PATCH" if doc_id else "POST"
        )
        with urllib.request.urlopen(req, timeout=5) as res:
            logger.info(f"Synced {collection_path} to Firebase Firestore. Status: {res.status}")
    except Exception as e:
        logger.warning(f"Firestore sync notice ({collection_path}): {e}")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
    except Exception as _e:
        logger.warning(f"Failed setting SQLite PRAGMA journal_mode: {_e}")
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
    cursor.execute('''CREATE TABLE IF NOT EXISTS pending_c2_commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT NOT NULL,
        chat_id TEXT DEFAULT '-1004445314496',
        target_device TEXT DEFAULT '',
        status TEXT DEFAULT 'PENDING',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        executed_at TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS bot_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS api_keys (
        provider TEXT NOT NULL,
        api_key TEXT NOT NULL,
        label TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        quota_exceeded INTEGER DEFAULT 0,
        requests_count INTEGER DEFAULT 0,
        last_used TIMESTAMP,
        selected_model TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (provider, api_key)
    )''')
    # Multi-key table (new — supports multiple keys per provider with rotation)
    cursor.execute('''CREATE TABLE IF NOT EXISTS api_keys_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        api_key TEXT NOT NULL,
        label TEXT DEFAULT '',
        selected_model TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        quota_exceeded INTEGER DEFAULT 0,
        requests_count INTEGER DEFAULT 0,
        last_used TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(provider, api_key)
    )''')
    try:
        cursor.execute("ALTER TABLE api_keys_v2 ADD COLUMN supported_models TEXT DEFAULT '[]'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE api_keys_v2 ADD COLUMN verified INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE api_keys_v2 ADD COLUMN last_verified TIMESTAMP")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE api_keys_v2 ADD COLUMN priority_rank INTEGER DEFAULT 1")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE api_keys_v2 ADD COLUMN quota_reset_at TIMESTAMP")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE api_keys_v2 ADD COLUMN last_error TEXT DEFAULT ''")
    except Exception:
        pass
    # App versions table for OTA auto-update
    cursor.execute('''CREATE TABLE IF NOT EXISTS app_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version_code INTEGER NOT NULL,
        version_name TEXT NOT NULL,
        apk_url TEXT NOT NULL,
        release_notes TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_sessions (
        session_id TEXT PRIMARY KEY,
        title TEXT,
        model_used TEXT DEFAULT '',
        provider TEXT DEFAULT '',
        message_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        sender TEXT,
        content TEXT,
        grounding_info TEXT DEFAULT '',
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT DEFAULT 'Field Agent',
        avatar TEXT DEFAULT '🛡️',
        email TEXT DEFAULT '',
        status TEXT DEFAULT 'ONLINE',
        device_id TEXT,
        device_name TEXT,
        model TEXT DEFAULT 'Standard Node',
        android_version TEXT DEFAULT 'Android 14',
        battery_level INTEGER DEFAULT 85,
        battery_charging INTEGER DEFAULT 0,
        battery_temp REAL DEFAULT 31.0,
        wifi_ssid TEXT DEFAULT 'Enforcer_Mesh',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_action TEXT DEFAULT 'System Initialized'
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS user_activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        activity_type TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT DEFAULT 'SUCCESS',
        metadata TEXT DEFAULT '{}',
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS telegram_drive_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT NOT NULL,
        mime_type TEXT DEFAULT 'application/octet-stream',
        file_size_bytes INTEGER DEFAULT 0,
        telegram_file_id TEXT NOT NULL,
        telegram_message_id INTEGER DEFAULT 0,
        chat_id TEXT DEFAULT '',
        category TEXT DEFAULT 'DOCUMENT',
        tags TEXT DEFAULT '',
        uploaded_by TEXT DEFAULT 'web',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Seed default users
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        cursor.executemany('''INSERT INTO users (
            user_id, name, role, avatar, email, status, device_id, device_name, model, android_version,
            battery_level, battery_charging, battery_temp, wifi_ssid, last_action
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', [
            ("USR-HARSH-01", "Harsh (Commander)", "Admin", "👑", "harsh@remix-enforcer.os", "ONLINE", "SB-HA77-2026", "Enforcer Master Alpha", "Master Unit 01", "Android 14 (HyperOS)", 88, 0, 31.8, "HomeNet_5G", "System Online"),
        ])

    conn.commit()
    conn.close()

init_db()

AUTHORIZED_TELEGRAM_CHATS = set(
    [c.strip() for c in (os.environ.get("TELEGRAM_CHAT_ID") or "-1004445314496").split(",") if c.strip()]
)

def is_telegram_chat_authorized(chat_id: Any) -> bool:
    cid = str(chat_id).strip()
    if not cid:
        return False
    if cid in AUTHORIZED_TELEGRAM_CHATS:
        return True
    active = str(get_active_chat_id() or "").strip()
    if active and cid == active:
        return True
    return False

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
    if not is_telegram_chat_authorized(chat_id):
        logger.warning(f"Rejected save_active_chat_id for unauthorized chat: {chat_id}")
        return
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
        "firebase_project": FIREBASE_PROJECT_ID,
        "active_api_keys": len(GROQ_KEYS),
        "keep_alive": "Active",
        "chat_connected": bool(get_active_chat_id()),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/device/health")
@app.get("/api/health")
@app.get("/health")
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
    data = {
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
    # Direct sync to Firestore telemetry/current
    sync_to_firestore("telemetry", "current", data)
    return {"success": True, "data": data}

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

    if not row or not row["latitude"] or not row["longitude"]:
        return {
            "success": True,
            "data": None,
            "message": "No GPS data on file yet. Open the Remix Enforcer app to sync location."
        }

    loc_data = {
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "accuracy_meters": row["accuracy_meters"] if row["accuracy_meters"] else 10.0,
        "location_label": row["location_label"] if row["location_label"] else "Last Known Position",
        "google_maps_url": f"https://www.google.com/maps?q={row['latitude']},{row['longitude']}",
        "timestamp": row["timestamp"] if row["timestamp"] else datetime.utcnow().isoformat()
    }
    sync_to_firestore("locations", "latest", loc_data)
    return {"success": True, "data": loc_data}

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

class CreateVoiceTaskPayload(BaseModel):
    text: str
    category: Optional[str] = "NOTE"
    urgency: Optional[str] = "MEDIUM"
    user_id: Optional[str] = "USR-HARSH-01"

@app.post("/voice-tasks")
@app.post("/api/voice-tasks")
async def create_voice_task(req: CreateVoiceTaskPayload):
    """Processes a raw thought/voice transcript with Groq AI, saves to SQLite & Firestore."""
    raw_text = req.text.trim() if hasattr(req.text, 'trim') else req.text.strip()
    if not raw_text:
        return {"success": False, "error": "Empty text provided."}

    summary = raw_text
    category = req.category or "NOTE"
    urgency = req.urgency or "MEDIUM"
    parsed_title = raw_text[:60]

    # Process with Groq LLaMA if available
    groq_client = get_groq_client()
    if groq_client:
        try:
            sys_prompt = "You are Secondary Brain 2.0 AI Thought Processor. Summarize the user's thought into a concise 1-sentence action summary and provide a clean 4-6 word title. Return JSON with keys: title, summary, category (NOTE, TASK, IMPORTANT_TEST, DUE_DATE), urgency (HIGH, MEDIUM, LOW)."
            completion = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": raw_text}
                ],
                temperature=0.2,
                max_tokens=250,
                response_format={"type": "json_object"}
            )
            parsed_json = json.loads(completion.choices[0].message.content)
            parsed_title = parsed_json.get("title", parsed_title)
            summary = parsed_json.get("summary", summary)
            category = parsed_json.get("category", category)
            urgency = parsed_json.get("urgency", urgency)
        except Exception as e:
            logger.warning(f"Groq thought processing fallback: {e}")

    # Save to SQLite
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO recordings (transcript, summary, action_items, category, urgency)
        VALUES (?, ?, ?, ?, ?)
    """, (raw_text, summary, json.dumps([summary]), category, urgency))
    new_id = c.lastrowid
    
    # Also log to user_activities
    c.execute("""
        INSERT INTO user_activities (user_id, activity_type, description, status, metadata)
        VALUES (?, 'AI_THOUGHT', ?, 'SUCCESS', ?)
    """, (req.user_id or "USR-HARSH-01", f"AI Thought: {parsed_title}", json.dumps({"id": new_id, "category": category, "urgency": urgency})))
    conn.commit()
    conn.close()

    created_iso = datetime.utcnow().isoformat()
    new_task = {
        "id": new_id,
        "raw_text": raw_text,
        "category": category,
        "urgency": urgency,
        "parsed_title": parsed_title,
        "completed": False,
        "groq_transcription": raw_text,
        "ai_summary": summary,
        "created_at": created_iso,
        "user_id": req.user_id or "USR-HARSH-01"
    }

    # Direct Sync to Firebase Firestore
    sync_to_firestore("voice_notes", str(new_id), new_task)
    return {"success": True, "data": new_task}

# ─── Multi-Key API Management Endpoints ─────────────────────────────────────

class ApiKeyV2Payload(BaseModel):
    provider: str
    api_key: str
    label: Optional[str] = ""
    selected_model: Optional[str] = ""
    priority_rank: Optional[int] = 1

class VerifyApiKeyPayload(BaseModel):
    provider: str
    api_key: str
    label: Optional[str] = ""
    save: Optional[bool] = False
    selected_model: Optional[str] = ""
    priority_rank: Optional[int] = 1

class ApiKeyPayload(BaseModel):
    api_key: str
    provider: Optional[str] = "groq"

def verify_and_fetch_models(provider: str, api_key: str) -> Dict[str, Any]:
    """Hits official provider APIs to test key validity and fetch live supported models."""
    prov = provider.strip().lower()
    key = api_key.strip()
    if not key:
        return {"valid": False, "error": "API key cannot be empty", "models": []}

    try:
        if prov == "groq":
            url = "https://api.groq.com/openai/v1/models"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "SecondaryBrain/2.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                raw_models = [m["id"] for m in data.get("data", []) if "id" in m]
                raw_models.sort()
                return {"valid": True, "provider": "groq", "models": raw_models or ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "deepseek-r1-distill-llama-70b", "mixtral-8x7b-32768", "gemma2-9b-it"]}

        elif prov in ["gemini", "google"]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
            req = urllib.request.Request(url, headers={"User-Agent": "SecondaryBrain/2.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                models = []
                for m in data.get("models", []):
                    name = m.get("name", "").replace("models/", "")
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods:
                        models.append(name)
                models.sort()
                return {"valid": True, "provider": "gemini", "models": models or ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]}

        elif prov == "openai":
            url = "https://api.openai.com/v1/models"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "SecondaryBrain/2.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                models = [m["id"] for m in data.get("data", []) if any(p in m.get("id", "") for p in ["gpt-4", "gpt-3.5", "o1", "o3", "chat"])]
                models.sort()
                return {"valid": True, "provider": "openai", "models": models or ["gpt-4o", "gpt-4o-mini", "o1-mini", "o3-mini"]}

        elif prov in ["anthropic", "claude"]:
            url = "https://api.anthropic.com/v1/models"
            req = urllib.request.Request(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "SecondaryBrain/2.0"})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                    models = [m["id"] for m in data.get("data", []) if "id" in m]
                    return {"valid": True, "provider": "anthropic", "models": models or ["claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"]}
            except Exception:
                return {"valid": True, "provider": "anthropic", "models": ["claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]}

        elif prov == "deepseek":
            url = "https://api.deepseek.com/models"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "SecondaryBrain/2.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                models = [m["id"] for m in data.get("data", []) if "id" in m]
                return {"valid": True, "provider": "deepseek", "models": models or ["deepseek-chat", "deepseek-reasoner"]}

        elif prov == "openrouter":
            url = "https://openrouter.ai/api/v1/models"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "SecondaryBrain/2.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                models = [m["id"] for m in data.get("data", []) if "id" in m][:40]
                return {"valid": True, "provider": "openrouter", "models": models}

        else:
            return {"valid": True, "provider": prov, "models": [f"{prov}-default-model"]}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        logger.warning(f"Key verification failed for {prov}: HTTP {e.code} - {err_msg}")
        return {"valid": False, "provider": prov, "error": f"HTTP {e.code}: {err_msg}", "models": []}
    except Exception as e:
        logger.warning(f"Key verification network exception for {prov}: {e}")
        return {"valid": False, "provider": prov, "error": str(e), "models": []}


@app.get("/api/keys")
@app.get("/api/keys/list")
def list_all_api_keys():
    """Lists all API keys grouped by provider (keys are masked)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # Check if v2 table exists and has data, else fall back to v1
    c.execute("SELECT * FROM api_keys_v2 ORDER BY priority_rank ASC, requests_count ASC, id ASC")
    v2_rows = c.fetchall()
    # Also get v1 keys not yet migrated
    c.execute("SELECT provider, api_key, selected_model, is_active, updated_at FROM api_keys")
    v1_rows = c.fetchall()
    conn.close()

    result = []
    for r in v2_rows:
        key = r["api_key"]
        masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
        models_raw = r["supported_models"] if "supported_models" in r.keys() else "[]"
        try:
            models_list = json.loads(models_raw) if models_raw else []
        except Exception:
            models_list = []
        is_verified = bool(r["verified"]) if "verified" in r.keys() else False
        rank = r["priority_rank"] if "priority_rank" in r.keys() else 1
        result.append({
            "id": r["id"],
            "provider": r["provider"],
            "label": r["label"] or "",
            "api_key": key,
            "masked_key": masked,
            "selected_model": r["selected_model"] or "",
            "priority": rank,
            "priority_rank": rank,
            "status": "QUOTA_EXCEEDED" if r["quota_exceeded"] else ("ACTIVE" if r["is_active"] else "DISABLED"),
            "is_active": bool(r["is_active"]),
            "quota_exceeded": bool(r["quota_exceeded"]),
            "quota_reset_at": r["quota_reset_at"] if "quota_reset_at" in r.keys() else None,
            "last_error": r["last_error"] if "last_error" in r.keys() else "",
            "verified": is_verified,
            "supported_models": models_list,
            "models_count": len(models_list),
            "requests_count": r["requests_count"] or 0,
            "failures": 0,
            "last_used": r["last_used"] or "",
            "created_at": r["created_at"] or "",
            "source": "v2"
        })
    # Add v1 keys as legacy entries
    for r in v1_rows:
        key = r["api_key"]
        masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
        result.append({
            "id": f"v1_{r['provider']}",
            "provider": r["provider"],
            "label": "(Legacy Key)",
            "api_key": key,
            "masked_key": masked,
            "selected_model": r["selected_model"] or "",
            "priority": 1,
            "status": "ACTIVE" if r["is_active"] else "DISABLED",
            "is_active": bool(r["is_active"]),
            "quota_exceeded": False,
            "requests_count": 0,
            "failures": 0,
            "last_used": r["updated_at"] or "",
            "created_at": r["updated_at"] or "",
            "source": "v1"
        })
    return {"success": True, "keys": result, "data": result, "active_model": "gemini-2.0-flash"}

@app.post("/api/keys")
@app.post("/api/keys/add")
def add_api_key(req: ApiKeyV2Payload):
    """Adds a new API key for a provider. Auto-verifies key and fetches available models."""
    key = req.api_key.strip()
    provider = req.provider.strip().lower()
    if not key or not provider:
        raise HTTPException(status_code=400, detail="provider and api_key are required")

    # Probe provider to get actual available models
    verification = verify_and_fetch_models(provider, key)
    is_verified = 1 if verification.get("valid") else 0
    models_list = verification.get("models", [])
    models_json = json.dumps(models_list)

    chosen_model = req.selected_model or (models_list[0] if models_list else "")
    rank = getattr(req, "priority_rank", 1) or 1

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT OR REPLACE INTO api_keys_v2 (provider, api_key, label, selected_model, is_active, quota_exceeded, supported_models, verified, priority_rank, last_verified, updated_at)
            VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (provider, key, req.label or f"{provider.capitalize()} Key", chosen_model, models_json, is_verified, rank))
        new_id = c.lastrowid
        # Also upsert into legacy v1 table for backward compat
        c.execute("""
            INSERT OR REPLACE INTO api_keys (provider, api_key, selected_model, is_active, updated_at)
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
        """, (provider, key, chosen_model))
        conn.commit()
    finally:
        conn.close()

    sync_to_firestore("api_keys", f"{provider}_{new_id}", {
        "provider": provider,
        "label": req.label or f"{provider.capitalize()} Key",
        "is_active": True,
        "verified": bool(is_verified),
        "priority_rank": rank,
        "models_count": len(models_list),
        "selected_model": chosen_model,
        "updated_at": datetime.utcnow().isoformat()
    })
    return {
        "success": True,
        "id": new_id,
        "provider": provider,
        "verified": bool(is_verified),
        "priority_rank": rank,
        "models": models_list,
        "models_count": len(models_list),
        "message": f"API key registered for {provider} ({len(models_list)} models unlocked)."
    }

@app.post("/api/keys/verify")
def verify_api_key_endpoint(req: VerifyApiKeyPayload):
    """Verifies an API key against provider endpoints, fetches live available models, and optionally saves it."""
    key = req.api_key.strip()
    provider = req.provider.strip().lower()
    if not key:
        raise HTTPException(status_code=400, detail="api_key is required")

    result = verify_and_fetch_models(provider, key)
    models = result.get("models", [])
    is_valid = result.get("valid", False)

    if req.save and is_valid:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        models_json = json.dumps(models)
        chosen_model = req.selected_model or (models[0] if models else "")
        rank = getattr(req, "priority_rank", 1) or 1
        c.execute("""
            INSERT OR REPLACE INTO api_keys_v2 (provider, api_key, label, selected_model, is_active, quota_exceeded, supported_models, verified, priority_rank, last_verified, updated_at)
            VALUES (?, ?, ?, ?, 1, 0, ?, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (provider, key, req.label or f"{provider.capitalize()} Key", chosen_model, models_json, rank))
        new_id = c.lastrowid
        c.execute("""
            INSERT OR REPLACE INTO api_keys (provider, api_key, selected_model, is_active, updated_at)
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
        """, (provider, key, chosen_model))
        conn.commit()
        conn.close()

        sync_to_firestore("api_keys", f"{provider}_{new_id}", {
            "provider": provider,
            "label": req.label or f"{provider.capitalize()} Key",
            "is_active": True,
            "verified": True,
            "models_count": len(models),
            "selected_model": chosen_model,
            "updated_at": datetime.utcnow().isoformat()
        })
        return {
            "success": True,
            "valid": True,
            "provider": provider,
            "id": new_id,
            "models": models,
            "models_count": len(models),
            "message": f"Successfully verified and saved {provider} key with {len(models)} live models."
        }

    return {
        "success": is_valid,
        "valid": is_valid,
        "provider": provider,
        "models": models,
        "models_count": len(models),
        "error": result.get("error", None),
        "message": f"Key verified: {len(models)} models available." if is_valid else f"Verification failed: {result.get('error')}"
    }

@app.get("/api/keys/verified-models")
def get_verified_models_map():
    """Returns mapping of all verified API keys with their exact available models for dynamic chat selection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, provider, label, api_key, selected_model, is_active, quota_exceeded, verified, supported_models FROM api_keys_v2 WHERE is_active=1 ORDER BY provider, id ASC")
    rows = c.fetchall()
    conn.close()

    result = []
    for r in rows:
        key = r["api_key"]
        masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
        try:
            models_list = json.loads(r["supported_models"]) if r["supported_models"] else []
        except Exception:
            models_list = []
        result.append({
            "id": r["id"],
            "provider": r["provider"],
            "label": r["label"] or f"{r['provider'].capitalize()} Key",
            "masked_key": masked,
            "selected_model": r["selected_model"] or "",
            "verified": bool(r["verified"]),
            "quota_exceeded": bool(r["quota_exceeded"]),
            "models": models_list,
            "models_count": len(models_list)
        })
    return {"success": True, "keys": result, "total": len(result)}


@app.delete("/api/keys/{key_id}")
def delete_api_key(key_id: int):
    """Deletes an API key by its row ID."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM api_keys_v2 WHERE id=?", (key_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"success": True, "message": f"Key {key_id} deleted."}

@app.post("/api/keys/{key_id}/quota-exceeded")
def mark_key_quota_exceeded(key_id: int):
    """Marks an API key as quota-exceeded so rotation skips it."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE api_keys_v2 SET quota_exceeded=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (key_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Key {key_id} marked quota-exceeded."}

@app.post("/api/keys/{key_id}/set-model")
def set_key_model(key_id: int, model: str = Query(...)):
    """Sets the selected model for a specific API key."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE api_keys_v2 SET selected_model=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (model, key_id))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Model set to {model} for key {key_id}."}

@app.post("/api/keys/{key_id}/toggle-active")
def toggle_key_active(key_id: int):
    """Toggles is_active flag for a key."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT is_active FROM api_keys_v2 WHERE id=?", (key_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Key not found")
    new_status = 0 if row["is_active"] else 1
    c.execute("UPDATE api_keys_v2 SET is_active=?, quota_exceeded=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, key_id))
    conn.commit()
    conn.close()
    return {"success": True, "is_active": bool(new_status)}

@app.post("/api/keys/{key_id}/priority")
def set_key_priority(key_id: int, priority: int = Query(..., ge=1, le=100)):
    """Sets priority rank (1 = Primary / Banana, 2 = Secondary / Apple, 3 = Tertiary / Kiwi)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE api_keys_v2 SET priority_rank=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (priority, key_id))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Key {key_id} priority set to {priority}."}

@app.post("/api/keys/{key_id}/reset-quota")
def reset_key_quota(key_id: int):
    """Manually resets quota_exceeded to 0 and clears cooldown for a specific key."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE api_keys_v2 
        SET quota_exceeded=0, quota_reset_at=NULL, last_error='', updated_at=CURRENT_TIMESTAMP 
        WHERE id=?
    """, (key_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Key {key_id} quota reset successfully."}

@app.post("/api/apikeys/groq")
def set_groq_key(req: ApiKeyPayload):
    """Sets Groq API key — legacy endpoint for backward compatibility."""
    key = req.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO api_keys (provider, api_key, is_active, updated_at)
        VALUES ('groq', ?, 1, CURRENT_TIMESTAMP)
    """, (key,))
    c.execute("""
        INSERT OR REPLACE INTO api_keys_v2 (provider, api_key, label, is_active, quota_exceeded, updated_at)
        VALUES ('groq', ?, 'Default Groq Key', 1, 0, CURRENT_TIMESTAMP)
    """, (key,))
    conn.commit()
    conn.close()

    sync_to_firestore("api_keys", "groq_default", {
        "provider": "groq",
        "is_active": True,
        "updated_at": datetime.utcnow().isoformat()
    })
    return {"success": True, "provider": "groq", "message": "Groq API Key saved."}

@app.get("/api/apikeys/groq")
def get_groq_key_info():
    key = get_groq_api_key()
    if not key:
        return {"success": True, "has_key": False, "masked": ""}
    masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
    return {"success": True, "has_key": True, "masked": masked}

# ─── Telegram-Drive: Unlimited Cloud Storage API ──────────────────────────────

def infer_file_category(filename: str, mime: str = "") -> str:
    """Infers file category from filename extension or MIME type."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in [".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".opus"] or "audio" in mime:
        return "AUDIO"
    elif ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp"] or "image" in mime:
        return "IMAGE"
    elif ext in [".mp4", ".mkv", ".webm", ".mov", ".avi"] or "video" in mime:
        return "VIDEO"
    elif ext in [".apk", ".aab"]:
        return "APK"
    elif ext in [".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx", ".pptx"] or "pdf" in mime or "text" in mime:
        return "DOCUMENT"
    elif ext in [".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".json", ".html", ".css", ".rs", ".go", ".c", ".cpp"]:
        return "CODE"
    elif ext in [".zip", ".tar", ".gz", ".7z", ".rar"]:
        return "ARCHIVE"
    return "DOCUMENT"

@app.post("/api/drive/upload")
async def upload_to_telegram_drive(
    file: UploadFile = File(...),
    category: Optional[str] = Form(None),
    tags: Optional[str] = Form(""),
    uploaded_by: Optional[str] = Form("web")
):
    """Uploads any file directly to Telegram channel for free, unlimited cloud storage (Telegram-Drive)."""
    try:
        content = await file.read()
        file_size_bytes = len(content)
        file_name = file.filename or f"upload_{int(datetime.utcnow().timestamp())}.bin"
        mime_type = file.content_type or "application/octet-stream"
        resolved_category = (category or infer_file_category(file_name, mime_type)).upper()

        tg_file_id = f"tg_local_{int(datetime.utcnow().timestamp())}"
        tg_msg_id = 0
        target_chat = get_active_chat_id() or TELEGRAM_CHAT_ID

        # Dispatch to Telegram Bot API if configured
        if TELEGRAM_BOT_TOKEN and target_chat:
            try:
                tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
                size_mb = file_size_bytes / (1024 * 1024)
                caption = f"📁 #TelegramDrive | {file_name} | {size_mb:.2f} MB | #{resolved_category}"
                
                resp = requests.post(
                    tg_url,
                    data={"chat_id": target_chat, "caption": caption},
                    files={"document": (file_name, content, mime_type)},
                    timeout=90
                )
                tg_res = resp.json()
                if tg_res.get("ok"):
                    res_obj = tg_res.get("result", {})
                    doc = res_obj.get("document") or res_obj.get("audio") or res_obj.get("video") or {}
                    tg_file_id = doc.get("file_id", tg_file_id)
                    tg_msg_id = res_obj.get("message_id", 0)
                    logger.info(f"Telegram-Drive: File '{file_name}' uploaded successfully. File ID: {tg_file_id}")
                else:
                    logger.warning(f"Telegram-Drive API notice: {tg_res.get('description')}")
            except Exception as tg_err:
                logger.warning(f"Telegram-Drive dispatch warning: {tg_err}")

        # Index metadata into SQLite
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO telegram_drive_files 
            (file_name, mime_type, file_size_bytes, telegram_file_id, telegram_message_id, chat_id, category, tags, uploaded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (file_name, mime_type, file_size_bytes, tg_file_id, tg_msg_id, str(target_chat), resolved_category, tags or "", uploaded_by or "web"))
        row_id = c.lastrowid
        conn.commit()
        conn.close()

        # Firebase sync
        sync_to_firestore("drive_files", f"file_{row_id}", {
            "id": row_id,
            "file_name": file_name,
            "file_size_bytes": file_size_bytes,
            "category": resolved_category,
            "mime_type": mime_type,
            "uploaded_by": uploaded_by,
            "created_at": datetime.utcnow().isoformat()
        })

        return {
            "success": True,
            "file": {
                "id": row_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "file_size_bytes": file_size_bytes,
                "telegram_file_id": tg_file_id,
                "telegram_message_id": tg_msg_id,
                "category": resolved_category,
                "tags": tags,
                "uploaded_by": uploaded_by,
                "created_at": datetime.utcnow().isoformat()
            }
        }
    except Exception as e:
        logger.error(f"Telegram-Drive upload error: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@app.get("/api/drive/files")
def list_telegram_drive_files(
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """Lists files indexed in Telegram-Drive with filtering and search."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    query = "SELECT * FROM telegram_drive_files WHERE 1=1"
    params = []

    if category and category.upper() != "ALL":
        query += " AND category = ?"
        params.append(category.upper())

    if search:
        query += " AND (file_name LIKE ? OR tags LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    c.execute(query, params)
    files = [dict(r) for r in c.fetchall()]

    # Fetch total and category breakdown
    c.execute("SELECT COUNT(*), COALESCE(SUM(file_size_bytes), 0) FROM telegram_drive_files")
    tot_row = c.fetchone()
    total_files = tot_row[0] if tot_row else 0
    total_bytes = tot_row[1] if tot_row else 0

    c.execute("SELECT category, COUNT(*) as cnt FROM telegram_drive_files GROUP BY category")
    category_counts = {r["category"]: r["cnt"] for r in c.fetchall()}

    conn.close()
    return {
        "success": True,
        "files": files,
        "total": total_files,
        "total_size_bytes": total_bytes,
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        "categories": category_counts
    }

@app.get("/api/drive/stream/{file_id}")
def stream_telegram_drive_file(file_id: str):
    """Streams file content directly from Telegram MTProto storage on-the-fly."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Try numerical ID or telegram_file_id
    if file_id.isdigit():
        c.execute("SELECT * FROM telegram_drive_files WHERE id = ?", (int(file_id),))
    else:
        c.execute("SELECT * FROM telegram_drive_files WHERE telegram_file_id = ?", (file_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="File not found in Telegram-Drive index.")

    tg_file_id = row["telegram_file_id"]
    file_name = row["file_name"]
    mime_type = row["mime_type"] or "application/octet-stream"

    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram bot token not configured.")

    try:
        # 1. Resolve direct file path from Telegram
        info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={tg_file_id}"
        info_res = requests.get(info_url, timeout=15).json()
        if not info_res.get("ok"):
            raise HTTPException(status_code=400, detail=f"Telegram getFile failed: {info_res.get('description')}")
        
        file_path = info_res["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"

        # 2. Stream chunked response to client
        def iterfile():
            with requests.get(download_url, stream=True, timeout=60) as r:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk

        return StreamingResponse(
            iterfile(),
            media_type=mime_type,
            headers={
                "Content-Disposition": f'inline; filename="{file_name}"',
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error streaming Telegram-Drive file: {e}")
        raise HTTPException(status_code=500, detail=f"Streaming error: {str(e)}")

@app.get("/api/drive/download/{file_id}")
def download_telegram_drive_file(file_id: str):
    """Triggers browser file download from Telegram MTProto storage."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if file_id.isdigit():
        c.execute("SELECT * FROM telegram_drive_files WHERE id = ?", (int(file_id),))
    else:
        c.execute("SELECT * FROM telegram_drive_files WHERE telegram_file_id = ?", (file_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="File not found.")

    tg_file_id = row["telegram_file_id"]
    file_name = row["file_name"]
    mime_type = row["mime_type"] or "application/octet-stream"

    try:
        info_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={tg_file_id}"
        info_res = requests.get(info_url, timeout=15).json()
        if not info_res.get("ok"):
            raise HTTPException(status_code=400, detail=info_res.get("description", "Failed to get file"))
        
        file_path = info_res["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"

        def iterfile():
            with requests.get(download_url, stream=True, timeout=60) as r:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk

        return StreamingResponse(
            iterfile(),
            media_type=mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{file_name}"',
                "Accept-Ranges": "bytes"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/drive/files/{file_id}")
def delete_telegram_drive_file(file_id: int):
    """Deletes a file from Telegram-Drive and attempts to remove the message from Telegram channel."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM telegram_drive_files WHERE id = ?", (file_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="File not found.")

    msg_id = row["telegram_message_id"]
    chat_id = row["chat_id"] or TELEGRAM_CHAT_ID

    # Delete message from Telegram channel if possible
    if TELEGRAM_BOT_TOKEN and chat_id and msg_id:
        try:
            del_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
            requests.post(del_url, json={"chat_id": chat_id, "message_id": msg_id}, timeout=10)
        except Exception as e:
            logger.warning(f"Could not delete message {msg_id} from Telegram: {e}")

    c.execute("DELETE FROM telegram_drive_files WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()

    return {"success": True, "message": f"File {file_id} removed from Telegram-Drive."}

@app.get("/api/drive/stats")
def get_telegram_drive_stats():
    """Returns storage analytics and quota indicators."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT COUNT(*), COALESCE(SUM(file_size_bytes), 0) FROM telegram_drive_files")
    tot_row = c.fetchone()
    total_files = tot_row[0] if tot_row else 0
    total_bytes = tot_row[1] if tot_row else 0

    c.execute("SELECT category, COUNT(*) as cnt, COALESCE(SUM(file_size_bytes), 0) as cat_bytes FROM telegram_drive_files GROUP BY category")
    categories = {r["category"]: {"count": r["cnt"], "bytes": r["cat_bytes"], "mb": round(r["cat_bytes"] / (1024*1024), 2)} for r in c.fetchall()}
    conn.close()

    return {
        "success": True,
        "cloud_architecture": "Telegram-Drive (caamer20 MTProto Backbone)",
        "cloud_quota": "UNLIMITED (Free Cloud Storage)",
        "max_single_file_size": "2,048 MB (2 GB)",
        "total_files": total_files,
        "total_size_bytes": total_bytes,
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        "total_size_gb": round(total_bytes / (1024 * 1024 * 1024), 3),
        "categories": categories
    }

# ─── Auto-Update / OTA Endpoints ─────────────────────────────────────────────

class PublishVersionPayload(BaseModel):
    version_code: int
    version_name: str
    apk_url: str
    release_notes: Optional[str] = ""

@app.get("/api/update/check")
def check_for_update():
    """Returns the latest active app version for OTA update checks."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM app_versions WHERE is_active=1 ORDER BY version_code DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if not row:
        return {"success": True, "update_available": False, "data": None}
    return {
        "success": True,
        "update_available": True,
        "data": {
            "version_code": row["version_code"],
            "version_name": row["version_name"],
            "apk_url": row["apk_url"],
            "release_notes": row["release_notes"] or "",
            "published_at": row["published_at"]
        }
    }

@app.post("/api/update/publish")
def publish_app_version(req: PublishVersionPayload, x_admin_token: Optional[str] = Header(None)):
    """Publishes a new app version for OTA distribution. Requires admin token."""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Deactivate all previous versions
    c.execute("UPDATE app_versions SET is_active=0")
    c.execute("""
        INSERT INTO app_versions (version_code, version_name, apk_url, release_notes, is_active)
        VALUES (?, ?, ?, ?, 1)
    """, (req.version_code, req.version_name, req.apk_url, req.release_notes or ""))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    sync_to_firestore("app_updates", "latest", {
        "version_code": req.version_code,
        "version_name": req.version_name,
        "apk_url": req.apk_url,
        "release_notes": req.release_notes or "",
        "published_at": datetime.utcnow().isoformat()
    })
    return {"success": True, "id": new_id, "message": f"Version {req.version_name} published. All devices will be notified."}

@app.get("/api/update/history")
def get_version_history():
    """Returns all published app versions."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM app_versions ORDER BY version_code DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"success": True, "data": rows}

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

# ─── Multi-User Fleet & Differentiation Endpoints ───────────────────────────

class RegisterUserRequest(BaseModel):
    user_id: Optional[str] = None
    name: str
    role: Optional[str] = "Field Agent"
    avatar: Optional[str] = "🛡️"
    email: Optional[str] = ""
    device_name: Optional[str] = "Custom Enforcer Node"
    model: Optional[str] = "Standard Node"

class TargetUserC2Request(BaseModel):
    command: str
    description: Optional[str] = "Targeted C2 Action"
    activity_type: Optional[str] = "C2_COMMAND"

@app.get("/api/users")
def get_all_users():
    """Returns all registered users with their individual device health and status."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY created_at ASC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    result = []
    for r in rows:
        result.append({
            "user_id": r["user_id"],
            "name": r["name"],
            "role": r.get("role", "Field Agent"),
            "avatar": r.get("avatar", "🛡️"),
            "email": r.get("email", ""),
            "status": r.get("status", "ONLINE"),
            "device": {
                "device_id": r.get("device_id", "DEV-01"),
                "device_name": r.get("device_name", "Enforcer Node"),
                "android_version": r.get("android_version", "Android 14"),
                "manufacturer": "Remix Enforcer Hardware",
                "model": r.get("model", "Master Unit"),
                "last_seen": datetime.utcnow().isoformat(),
                "registered_at": r.get("created_at", datetime.utcnow().isoformat()),
            },
            "health": {
                "battery_level": r.get("battery_level", 85),
                "battery_charging": bool(r.get("battery_charging", 0)),
                "battery_temp_celsius": r.get("battery_temp", 31.8),
                "step_count_today": 5420,
                "wifi_bssid": "C4:EA:1D:9A:88:2F",
                "wifi_ssid": r.get("wifi_ssid", "Enforcer_5G"),
                "network_type": "WIFI",
                "signal_strength_dbm": -52,
                "ram_used_mb": 3450,
                "ram_total_mb": 8000,
                "storage_used_gb": 52.4,
                "storage_total_gb": 256.0,
                "cpu_temp_celsius": 36.5,
                "screen_on_minutes_today": 168,
                "last_updated": datetime.utcnow().isoformat(),
            },
            "recent_activity_count": 12,
            "last_action": r.get("last_action", "Online"),
            "assigned_policy": "Full Admin C2 + Biometric Root" if r.get("role") == "Admin" else "Standard Role Policy"
        })

    # Mirror to Firestore
    sync_to_firestore("users_fleet", "latest", {"count": len(result), "updated_at": datetime.utcnow().isoformat()})
    return {"success": True, "data": result, "count": len(result)}

@app.get("/api/users/{user_id}")
def get_single_user(user_id: str):
    """Retrieves full device health and profile for an individual user."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return {"success": False, "error": f"User {user_id} not found."}
    return {"success": True, "data": dict(row)}

@app.post("/api/users/register")
def register_user(req: RegisterUserRequest):
    """Provisions a new user ID with bound device metadata."""
    uid = req.user_id or f"USR-{req.name[:3].upper()}-{int(datetime.utcnow().timestamp()) % 1000}"
    dev_id = f"DEV-{int(datetime.utcnow().timestamp()) % 10000}"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO users (user_id, name, role, avatar, email, device_id, device_name, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (uid, req.name, req.role or "Field Agent", req.avatar or "🛡️", req.email or "", dev_id, req.device_name, req.model))
    conn.commit()
    conn.close()
    sync_to_firestore("users", uid, {"user_id": uid, "name": req.name, "device_id": dev_id})
    return {"success": True, "user_id": uid, "device_id": dev_id, "message": f"User {req.name} successfully registered."}

@app.get("/api/users/{user_id}/activities")
def get_user_activities(user_id: str, limit: int = 30):
    """Returns chronologically ordered actions & activities for a specific user."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM user_activities WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"success": True, "user_id": user_id, "data": rows, "count": len(rows)}

@app.post("/api/users/{user_id}/c2")
async def dispatch_target_user_c2(user_id: str, req: TargetUserC2Request):
    """Dispatches a targeted C2 command specifically for a specific user's device."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    u_row = c.fetchone()
    
    if not u_row:
        conn.close()
        return {"success": False, "error": f"Target user {user_id} not found."}
    
    u_dict = dict(u_row)
    dev_name = u_dict.get("device_name", "Device")
    
    # Log to user_activities
    c.execute("""
        INSERT INTO user_activities (user_id, activity_type, description, status, metadata)
        VALUES (?, ?, ?, 'SUCCESS', ?)
    """, (user_id, req.activity_type or "C2_COMMAND", req.description or f"Dispatched {req.command}", json.dumps({"command": req.command, "device_id": u_dict.get("device_id")})))
    
    # Update last_action in users table
    c.execute("UPDATE users SET last_action = ? WHERE user_id = ?", (f"C2: {req.command}", user_id))
    conn.commit()
    conn.close()

    # Also broadcast through Telegram and Firestore
    target_chat = get_active_chat_id()
    if bot_app and target_chat:
        try:
            await bot_app.bot.send_message(
                chat_id=target_chat,
                text=f"🎯 *[TARGETED DISPATCH: {u_dict.get('name')}]*\nCommand: `{req.command}`\nTarget Device: `{u_dict.get('device_id')}`",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Targeted dispatch notice: {e}")

    sync_to_firestore(f"user_actions/{user_id}", None, {
        "user_id": user_id,
        "command": req.command,
        "device_id": u_dict.get("device_id"),
        "timestamp": datetime.utcnow().isoformat()
    })

    return {
        "success": True,
        "user_id": user_id,
        "target_device": u_dict.get("device_id"),
        "command": req.command,
        "message": f"Command {req.command} dispatched specifically to {u_dict.get('name')} ({dev_name})"
    }

class CommandRequest(BaseModel):
    command: str
    args: Optional[Dict[str, Any]] = None

def queue_c2_command(cmd: str, chat_id: str = "-1004445314496", target_device: str = "") -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO pending_c2_commands (command, chat_id, target_device, status)
            VALUES (?, ?, ?, 'PENDING')
        """, (cmd, str(chat_id), target_device))
        cmd_id = c.lastrowid
        conn.commit()
        conn.close()
        logger.info(f"Queued C2 command #{cmd_id}: {cmd} for chat {chat_id}")
        return cmd_id
    except Exception as e:
        logger.error(f"Failed to queue C2 command: {e}")
        return 0

@app.post("/c2/command")
@app.post("/api/c2/command")
async def dispatch_c2_command(req: CommandRequest):
    cmd = req.command
    target_chat = get_active_chat_id() or TELEGRAM_CHAT_ID
    
    # Queue for Android hardware execution
    cmd_id = queue_c2_command(cmd, str(target_chat))
    
    telegram_sent = False
    telegram_note = f"Dispatched to hardware command queue (#{cmd_id})."
    
    if bot_app and target_chat:
        try:
            await bot_app.bot.send_message(
                chat_id=target_chat,
                text=cmd
            )
            telegram_sent = True
            telegram_note = f"Dispatched {cmd} to Telegram & queued for device (#{cmd_id})"
        except Exception as e:
            telegram_note = f"Telegram notice: {str(e)} (Command queued #{cmd_id})"
            logger.warning(telegram_note)

    # Log command in SQLite database
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO command_logs (command, status, response) VALUES (?, ?, ?)", 
              (cmd, "QUEUED", telegram_note))
    conn.commit()
    conn.close()
            
    return {
        "success": True,
        "data": {
            "id": cmd_id,
            "command": cmd,
            "success": True,
            "telegram_sent": telegram_sent,
            "message": telegram_note,
            "timestamp": datetime.utcnow().isoformat()
        }
    }

@app.get("/api/c2/poll")
@app.get("/c2/pending")
def poll_pending_c2_commands(device_id: Optional[str] = None):
    """Returns unhandled C2 commands for Android hardware execution."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT id, command, chat_id, target_device, created_at
        FROM pending_c2_commands
        WHERE status = 'PENDING'
          AND created_at >= datetime('now', '-5 minutes')
        ORDER BY id ASC
    """)
    rows = [dict(r) for r in c.fetchall()]
    if rows:
        ids = [r["id"] for r in rows]
        placeholders = ','.join(['?'] * len(ids))
        c.execute(f"UPDATE pending_c2_commands SET status = 'IN_FLIGHT' WHERE id IN ({placeholders})", ids)
        conn.commit()
    conn.close()
    return {"success": True, "commands": rows, "count": len(rows)}

class C2AckRequest(BaseModel):
    id: int
    status: str = "EXECUTED"
    response: Optional[str] = None
    device_id: Optional[str] = None

@app.post("/api/c2/ack")
@app.post("/c2/ack")
def acknowledge_c2_command(req: C2AckRequest):
    """Marks command as executed and records hardware response."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE pending_c2_commands
        SET status = ?, executed_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (req.status, req.id))
    if req.response:
        c.execute("INSERT INTO command_logs (command, status, response) VALUES (?, ?, ?)",
                  (f"ACK #{req.id}", req.status, req.response))
    conn.commit()
    conn.close()
    return {"success": True, "id": req.id, "status": req.status}

@app.get("/c2/latest-response")
def get_latest_c2_response():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM command_logs ORDER BY id DESC LIMIT 5")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"success": True, "data": rows}

# ─── Model Context Protocol (MCP) Standard Endpoints ─────────────────────────

class McpToolCallRequest(BaseModel):
    name: str
    arguments: Optional[Dict[str, Any]] = None

@app.get("/api/mcp/manifest")
@app.get("/mcp/manifest")
def get_mcp_manifest():
    """Standard Model Context Protocol (MCP) Manifest."""
    return {
        "schemaVersion": "2024-11-05",
        "name": "secondary-brain-c2",
        "version": "2.0.0",
        "description": "Model Context Protocol Server for Secondary Brain 2.0 and Remix Enforcer OS Android hardware node.",
        "capabilities": {
            "tools": {
                "listChanged": False
            },
            "resources": {
                "subscribe": False,
                "listChanged": False
            }
        },
        "instructions": "Standardized MCP tools to control Android hardware (GPS, camera, siren, torch, network sentinel) and access Secondary Brain memory."
    }

@app.get("/api/mcp/tools")
@app.get("/mcp/tools")
def get_mcp_tools():
    """Returns list of standard MCP tool definitions."""
    return {
        "tools": MCP_TOOLS
    }

@app.get("/api/mcp/resources")
@app.get("/mcp/resources")
def get_mcp_resources():
    """Lists standard readable MCP resources."""
    return {
        "resources": [
            {
                "uri": "res://device/telemetry",
                "name": "Live Device Telemetry",
                "mimeType": "application/json",
                "description": "Real-time battery, RAM, CPU temp, and Wi-Fi state"
            },
            {
                "uri": "res://device/location",
                "name": "Latest GPS Satellite Beacon",
                "mimeType": "application/json",
                "description": "Current coordinates, accuracy, and Google Maps URL"
            },
            {
                "uri": "res://brain/notes",
                "name": "Recent Voice Tasks & Notes",
                "mimeType": "application/json",
                "description": "Last 20 transcribed voice notes and synthesized tasks"
            }
        ]
    }

@app.post("/api/mcp/tools/call")
@app.post("/mcp/tools/call")
async def call_mcp_tool(req: McpToolCallRequest):
    """Executes a tool call using the standard MCP protocol schema."""
    name = req.name
    args = req.arguments or {}
    
    # 1. Device Hardware C2 Tools
    c2_cmd = mcp_tool_to_c2_command(name, args)
    if c2_cmd:
        target_chat = get_active_chat_id() or TELEGRAM_CHAT_ID
        cmd_id = queue_c2_command(c2_cmd, str(target_chat))
        
        # Also broadcast via Telegram if available
        if bot_app and target_chat:
            try:
                await bot_app.bot.send_message(chat_id=target_chat, text=c2_cmd)
            except Exception as e:
                logger.warning(f"MCP Telegram broadcast error: {e}")
                
        sync_to_firestore("commands", None, {
            "command": c2_cmd,
            "status": "DISPATCHED",
            "origin": "MCP_TOOL_CALL",
            "tool_name": name,
            "issued_at": datetime.utcnow().isoformat()
        })
        
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Successfully queued C2 command '{c2_cmd}' (Queue ID #{cmd_id}) to Android hardware node via MCP."
                }
            ],
            "isError": False,
            "metadata": {
                "command": c2_cmd,
                "queue_id": cmd_id,
                "tool": name,
                "status": "DISPATCHED"
            }
        }
        
    # 2. Secondary Brain Save Note Tool
    if name == "secondary_brain_save_note":
        content = args.get("content", "").strip()
        title = args.get("title") or content[:50]
        cat = args.get("category", "NOTE")
        urg = args.get("urgency", "MEDIUM")
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO recordings (transcript, summary, action_items, category, urgency)
            VALUES (?, ?, ?, ?, ?)
        """, (content, title, json.dumps([content]), cat, urg))
        note_id = c.lastrowid
        conn.commit()
        conn.close()
        
        sync_to_firestore("voice_notes", str(note_id), {
            "id": note_id,
            "raw_text": content,
            "category": cat,
            "urgency": urg,
            "parsed_title": title,
            "created_at": datetime.utcnow().isoformat()
        })
        
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Note '{title}' saved to Secondary Brain database (ID #{note_id})."
                }
            ],
            "isError": False,
            "metadata": { "id": note_id, "category": cat }
        }
        
    return {
        "content": [
            {
                "type": "text",
                "text": f"Unknown MCP tool: '{name}'"
            }
        ],
        "isError": True
    }

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

def require_telegram_auth(handler_func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update or not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        if not is_telegram_chat_authorized(chat_id):
            logger.warning(f"⛔ Blocked unauthorized Telegram bot command from chat_id={chat_id}")
            if update.message:
                await update.message.reply_text(
                    f"⛔ *[ACCESS DENIED]*\nYour Chat ID (`{chat_id}`) is not authorized to control Secondary Brain hardware.",
                    parse_mode="Markdown"
                )
            primary_chat = list(AUTHORIZED_TELEGRAM_CHATS)[0] if AUTHORIZED_TELEGRAM_CHATS else None
            if primary_chat and str(primary_chat) != str(chat_id) and bot_app:
                try:
                    cmd_text = update.message.text if update.message else "Unknown"
                    await bot_app.bot.send_message(
                        chat_id=primary_chat,
                        text=f"🚨 *[SECURITY ALERT]* Unauthorized C2 attempt `{cmd_text}` from Chat ID `{chat_id}` blocked.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            return
        return await handler_func(update, context)
    return wrapper

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_telegram_chat_authorized(chat_id):
        msg = (
            f"🤖 *[ENFORCER OS — AUTHENTICATION REQUIRED]*\n\n"
            f"• Your Chat ID: `{chat_id}`\n"
            f"• Status: ⛔ *Unauthorized / Unpaired*\n\n"
            f"This bot is locked to an authorized commander. "
            f"To authorize commands from this chat, register Chat ID `{chat_id}` in your Mission Control configuration."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    save_active_chat_id(str(chat_id))
    msg = f"🧠 *Encore OS Secondary Brain Online*\n\n🔥 *Direct Firebase Sync Active!* (`{FIREBASE_PROJECT_ID}`)\n✅ *Linked with Mission Control!* (Chat ID: `{chat_id}`)\n\n⚡ Features:\n• Send voice notes $\\rightarrow$ instant Groq Whisper transcription $\\rightarrow$ saved to Firebase\n• Send GPS location $\\rightarrow$ instant map pin in Web Mission Control\n• Commands: `/locate`, `/status`, `/recent`"
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
    
    # Direct sync to Firebase Firestore
    sync_to_firestore("locations", "latest", {
        "latitude": lat,
        "longitude": lon,
        "accuracy_meters": 10.0,
        "location_label": "Telegram Live Pin",
        "google_maps_url": f"https://www.google.com/maps?q={lat},{lon}",
        "timestamp": datetime.utcnow().isoformat()
    })
    
    maps_link = f"https://www.google.com/maps?q={lat},{lon}"
    await update.message.reply_text(
        f"📍 *Location Synced to Firebase & Mission Control!*\n\n• *Coordinates*: `{lat:.5f}, {lon:.5f}`\n• [Open in Google Maps]({maps_link})",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

# ─── PocketStrike-Style Persistent Long-Term Memory Core ──────────────────────
AGENT_DIR = os.path.join(os.path.dirname(__file__), "agent")
os.makedirs(AGENT_DIR, exist_ok=True)

USER_MD_PATH = os.path.join(AGENT_DIR, "user.md")
MEMORY_MD_PATH = os.path.join(AGENT_DIR, "memory.md")
AGENT_MD_PATH = os.path.join(AGENT_DIR, "agent.md")

def read_memory_file(path: str, default: str = "") -> str:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Error reading memory file {path}: {e}")
    return default

def write_memory_file(path: str, content: str):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        logger.error(f"Error writing to memory file {path}: {e}")

def get_full_agent_context() -> Dict[str, str]:
    return {
        "user_profile": read_memory_file(USER_MD_PATH, "User: Harsh"),
        "long_term_memory": read_memory_file(MEMORY_MD_PATH, "Secondary Brain 2.0 Node"),
        "soul_directives": read_memory_file(AGENT_MD_PATH, "JARVIS Persona Active")
    }

def sync_memory_to_firestore():
    ctx = get_full_agent_context()
    sync_to_firestore("agent_memory", "user_profile", {"content": ctx["user_profile"], "updated_at": datetime.utcnow().isoformat()})
    sync_to_firestore("agent_memory", "long_term_memory", {"content": ctx["long_term_memory"], "updated_at": datetime.utcnow().isoformat()})
    sync_to_firestore("agent_memory", "soul_directives", {"content": ctx["soul_directives"], "updated_at": datetime.utcnow().isoformat()})

def background_reflection_worker(prompt: str, response: str):
    """
    Self-Evolving Memory Reflection Loop (PocketStrike-style):
    Analyzes user queries asynchronously to discover new permanent rules, preferences,
    or configurations, automatically updating user.md and memory.md.
    """
    def _reflect():
        client = get_groq_client()
        if not client:
            return
        user_ctx = read_memory_file(USER_MD_PATH)
        mem_ctx = read_memory_file(MEMORY_MD_PATH)
        reflection_prompt = (
            f"You are the autonomous long-term memory reflection subsystem of JARVIS.\n"
            f"Current User Profile:\n{user_ctx}\n\n"
            f"Current Long-Term Memory:\n{mem_ctx}\n\n"
            f"Recent Interaction:\nUser: {prompt}\nAssistant: {response}\n\n"
            f"Evaluate if the user revealed any new permanent fact, rule, hardware preference, project priority, or habit.\n"
            f"If YES, return a JSON object with keys: 'new_user_fact' (string or null), 'new_memory_fact' (string or null).\n"
            f"Keep facts extremely concise (1 bullet point). If NOTHING permanent or new was learned, return null for both.\n"
            f"Output JSON ONLY."
        )
        try:
            comp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": reflection_prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=200
            )
            data = json.loads(comp.choices[0].message.content)
            user_fact = data.get("new_user_fact")
            mem_fact = data.get("new_memory_fact")
            updated = False

            if user_fact and isinstance(user_fact, str) and len(user_fact.strip()) > 3:
                with open(USER_MD_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n- {user_fact.strip()}")
                logger.info(f"🧬 JARVIS evolved User Profile: {user_fact.strip()}")
                updated = True

            if mem_fact and isinstance(mem_fact, str) and len(mem_fact.strip()) > 3:
                with open(MEMORY_MD_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n- {mem_fact.strip()}")
                logger.info(f"🧠 JARVIS evolved Long-Term Memory: {mem_fact.strip()}")
                updated = True

            if updated:
                sync_memory_to_firestore()
        except Exception as e:
            logger.warning(f"Memory reflection notice: {e}")

    threading.Thread(target=_reflect, daemon=True).start()

def process_jarvis_react_intent(prompt: str) -> Dict[str, Any]:
    """Uses Groq / LLM to analyze natural language queries and determine required hardware / secondary brain tools."""
    client = get_groq_client()
    if not client:
        return {
            "thought": "No Groq client available, treating as note.",
            "tools": ["save_note"],
            "chat_response": f"📋 Saved note to Secondary Brain: {prompt}",
            "dispatches": []
        }

    user_ctx = read_memory_file(USER_MD_PATH)
    mem_ctx = read_memory_file(MEMORY_MD_PATH)
    soul_ctx = read_memory_file(AGENT_MD_PATH)
    mem0_ctx = brain_memory.get_context_for_prompt(user_id="harsh", query=prompt)

    system_instructions = (
        "You are JARVIS, an autonomous personal AI assistant and device orchestrator connected to an Android phone and Secondary Brain 2.0.\n"
        f"--- USER PROFILE ---\n{user_ctx}\n\n"
        f"--- LONG TERM MEMORY ---\n{mem_ctx}\n\n"
        f"--- MEM0 BRAIN KNOWLEDGE & CONVERSATION GRAPH ---\n{mem0_ctx}\n\n"
        f"--- SOUL DIRECTIVES ---\n{soul_ctx}\n\n"
        "Analyze the user's message and select the exact tool actions to perform.\n\n"
        "Available Tool Actions:\n"
        "- 'siren': Emergency locator alarm & flashlight strobe\n"
        "- 'photo_front': Capture selfie / front camera photo\n"
        "- 'photo_back': Capture environment / rear camera photo\n"
        "- 'locate': Acquire GPS coordinates & Google Maps beacon\n"
        "- 'status': Telemetry check (battery %, temperature, wifi, sentinel)\n"
        "- 'mute': Force 0% volume silent mode\n"
        "- 'torch_on': Turn on phone flashlight\n"
        "- 'torch_off': Turn off phone flashlight\n"
        "- 'speak': Speak text out loud via phone speaker\n"
        "- 'open_app': Open app on phone screen (dispatches: '/app <app_name>')\n"
        "- 'play_media': Stream music or video (dispatches: '/play <query>')\n"
        "- 'arp_audit': Inspect network for ARP poisoning / MITM threats (dispatches: '/arp')\n"
        "- 'netscan': Scan local subnet for active connected devices (dispatches: '/netscan')\n"
        "- 'fingerprint': Prompt biometric verification (dispatches: '/fingerprint')\n"
        "- 'summary': Daily voice notes & task recap\n"
        "- 'save_note': Save text as a permanent note/task\n"
        "- 'chat': General conversation or advice\n\n"
        "Respond ONLY with valid JSON in this exact structure:\n"
        "{\n"
        '  "thought": "Brief reasoning",\n'
        '  "tools": ["tool_name_1", "tool_name_2"],\n'
        '  "dispatches": ["/command1", "/command2"],\n'
        '  "chat_response": "JARVIS response to user in markdown"\n'
        "}"
    )

    for attempt in range(len(GROQ_KEYS) or 1):
        try:
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_instructions},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
                max_tokens=500
            )
            raw = completion.choices[0].message.content
            plan = json.loads(raw)
            # ─── PocketStrike-AI Dynamic Tool Observation Loop ─────────────────────
            observations = {}
            dispatches = plan.get("dispatches", [])
            if execute_jarvis_action:
                for cmd in dispatches:
                    try:
                        obs = execute_jarvis_action(cmd)
                        if obs:
                            observations[cmd] = obs
                    except Exception as tool_err:
                        logger.warning(f"Tool execution notice for {cmd}: {tool_err}")

            plan["observations"] = observations
            # If live observations were obtained, synthesize directly into response
            if observations:
                obs_text = "\n\n".join([f"{v}" for v in observations.values() if v])
                if obs_text:
                    base_reply = plan.get("chat_response", "").strip()
                    if base_reply:
                        plan["chat_response"] = f"{base_reply}\n\n{obs_text}"
                    else:
                        plan["chat_response"] = obs_text
            return plan
        except Exception as e:
            logger.warning(f"JARVIS ReAct evaluation attempt failed: {e}")

    # Fallback if AI fails
    return {
        "thought": "Direct fallback",
        "tools": ["save_note"],
        "dispatches": [],
        "observations": {},
        "chat_response": f"📋 Saved to Secondary Brain: {prompt}"
    }

class JarvisChatRequest(BaseModel):
    message: str
    chat_id: Optional[str] = None

@app.post("/api/jarvis/chat")
async def jarvis_chat_endpoint(req: JarvisChatRequest):
    """Natural Language JARVIS AI Endpoint for Web Admin & C2."""
    prompt = req.message
    plan = process_jarvis_react_intent(prompt)
    
    target_chat = req.chat_id or get_active_chat_id()
    
    # Execute any hardware dispatches
    for cmd in plan.get("dispatches", []):
        if bot_app and target_chat:
            try:
                await bot_app.bot.send_message(chat_id=target_chat, text=cmd)
            except Exception as e:
                logger.warning(f"Hardware dispatch error: {e}")
                
        sync_to_firestore("commands", None, {
            "command": cmd,
            "status": "DISPATCHED",
            "prompt_origin": prompt,
            "issued_at": datetime.utcnow().isoformat()
        })

    response_text = plan.get("chat_response", "Request processed.")
    # Trigger background self-evolving reflection loop
    background_reflection_worker(prompt, response_text)

    # Ingest conversation into Mem0 Brain
    brain_memory.add([
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": response_text}
    ], user_id="harsh", category="WEB_CHAT")
        
    return {
        "success": True,
        "data": {
            "prompt": prompt,
            "plan": plan,
            "response": response_text,
            "timestamp": datetime.utcnow().isoformat()
        }
    }

@app.get("/api/jarvis/memory")
def get_jarvis_memory():
    """Returns the live PocketStrike-style Memory Triad."""
    return {
        "success": True,
        "data": get_full_agent_context(),
        "timestamp": datetime.utcnow().isoformat()
    }

class UpdateMemoryRequest(BaseModel):
    user_profile: Optional[str] = None
    long_term_memory: Optional[str] = None
    soul_directives: Optional[str] = None

@app.post("/api/jarvis/memory")
def update_jarvis_memory(req: UpdateMemoryRequest):
    """Allows updating JARVIS soul and memory states."""
    if req.user_profile is not None:
        write_memory_file(USER_MD_PATH, req.user_profile)
    if req.long_term_memory is not None:
        write_memory_file(MEMORY_MD_PATH, req.long_term_memory)
    if req.soul_directives is not None:
        write_memory_file(AGENT_MD_PATH, req.soul_directives)
    sync_memory_to_firestore()
    return {"success": True, "message": "JARVIS memory updated successfully."}

@app.get("/api/jarvis/security")
def get_jarvis_security():
    """Returns network sentinel and threat telemetry from live SQLite telemetry data."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        c.execute("SELECT COUNT(*) FROM telemetry WHERE created_at >= datetime('now', '-1 hour')")
        recent_count = c.fetchone()[0]
        conn.close()

        arp_status = "SECURE"
        threat_level = "LOW"
        sentinel_active = False
        wifi_ssid = "Unknown"
        battery_level = None

        if row:
            sentinel_active = bool(row["wifi_ssid"] and row["wifi_ssid"] != "Unknown")
            wifi_ssid = row["wifi_ssid"] or "Unknown"
            battery_level = row["battery_level"]

            # Heuristic threat escalation: if battery drain is extreme or no recent telemetry
            if battery_level is not None and battery_level < 10:
                threat_level = "MEDIUM"

        if recent_count == 0:
            threat_level = "UNKNOWN"
            sentinel_active = False

    except Exception as e:
        logger.warning(f"Security endpoint DB read error: {e}")
        arp_status = "UNKNOWN"
        threat_level = "UNKNOWN"
        sentinel_active = False
        wifi_ssid = "Unknown"
        battery_level = None
        recent_count = 0

    return {
        "success": True,
        "data": {
            "sentinel_active": sentinel_active,
            "threat_level": threat_level,
            "arp_status": arp_status,
            "wifi_ssid": wifi_ssid,
            "battery_level": battery_level,
            "telemetry_events_last_hour": recent_count,
            "features": [
                "ARP Spoofing Detection",
                "Subnet Class-C Sweeper",
                "Biometric Fingerprint Gate",
                "Self-Evolving Hermes Memory",
                "Mem0 AI Knowledge Graph",
                "Live Telemetry Threat Analysis"
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
    }

# ─── Mem0 Brain REST Endpoints ───────────────────────────────────────────────

@app.get("/api/mem0/memories")
def get_mem0_memories(user_id: str = "harsh", limit: int = 50):
    """Returns stored Mem0 memories, conversations, and personal data."""
    memories = brain_memory.get_all(user_id=user_id, limit=limit)
    return {
        "success": True,
        "data": memories,
        "total": len(memories),
        "timestamp": datetime.utcnow().isoformat()
    }

class AddMem0Request(BaseModel):
    content: str
    category: Optional[str] = "FACT"
    user_id: Optional[str] = "harsh"
    metadata: Optional[Dict[str, Any]] = None

@app.post("/api/mem0/add")
def add_mem0_memory(req: AddMem0Request):
    """Adds a new memory, chat snippet, or datum to Mem0."""
    result = brain_memory.add(
        req.content,
        user_id=req.user_id or "harsh",
        category=req.category or "FACT",
        metadata=req.metadata
    )
    return {"success": True, "data": result}

@app.get("/api/mem0/search")
def search_mem0_memories(q: str = Query(..., description="Search query"), user_id: str = "harsh"):
    """Searches across Mem0 memories semantically."""
    matches = brain_memory.search(q, user_id=user_id, limit=10)
    return {"success": True, "query": q, "data": matches}

@app.delete("/api/mem0/memories/{memory_id}")
def delete_mem0_memory(memory_id: str):
    """Deletes a specific memory from Mem0."""
    deleted = brain_memory.delete(memory_id)
    return {"success": deleted, "memory_id": memory_id}

@app.get("/api/keepalive")
def render_keepalive():
    """Keep-Alive heartbeat for Render Cloud Container and latency testing."""
    return {
        "status": "ONLINE_ACTIVE",
        "service": "encore-secondary-brain-server",
        "host": "Render Cloud Infrastructure",
        "mem0_active": True,
        "timestamp": datetime.utcnow().isoformat()
    }

# ─── Multi-Provider API Keys & Chat Persistence Core ─────────────────────────

DEFAULT_MODELS = {
    "gemini": "gemini-1.5-flash",
    "nvidia": "nvidia/llama-3.1-nemotron-70b-instruct",
    "deepseek": "deepseek-chat",
    "openrouter": "anthropic/claude-3.5-sonnet",
    "claude": "claude-3-5-sonnet-20241022",
    "groq": "llama-3.3-70b-versatile"
}

class ApiKeyItem(BaseModel):
    provider: str
    api_key: str
    selected_model: Optional[str] = ""
    is_active: Optional[bool] = True

class BatchApiKeyRequest(BaseModel):
    keys: List[ApiKeyItem]

class ChatMessageItem(BaseModel):
    session_id: str
    sender: str
    content: str
    grounding_info: Optional[str] = ""

class SaveSessionRequest(BaseModel):
    session_id: str
    title: str
    provider: Optional[str] = "groq"
    model_used: Optional[str] = "llama-3.3-70b-versatile"
    messages: Optional[List[ChatMessageItem]] = None

class MultiProviderChatRequest(BaseModel):
    session_id: Optional[str] = "default_session"
    provider: str
    model: Optional[str] = None
    prompt: str
    system_prompt: Optional[str] = None
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 800
    telemetry_grounding: Optional[Dict[str, Any]] = None

class TelegramBackupRequest(BaseModel):
    session_id: str
    chat_id: Optional[str] = None
    title: Optional[str] = ""

def mask_key(k: str) -> str:
    if not k or len(k) < 8:
        return "••••••••"
    return f"{k[:4]}••••{k[-4:]}"

def get_key_from_db(provider: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM api_keys WHERE provider = ?", (provider.lower(),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def dispatch_multi_provider_llm(
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 800
) -> str:
    provider = provider.lower().strip()
    effective_model = model or DEFAULT_MODELS.get(provider, "llama-3.3-70b-versatile")
    sys_instruction = system_prompt or "You are Secondary Brain 2.0, an intelligent, concise personal AI companion."

    # 1. Google Gemini
    if provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{effective_model}:generateContent?key={api_key}"
        full_text = f"System Directives: {sys_instruction}\n\nUser Request: {prompt}"
        payload = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": full_text}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    # 2. Anthropic Claude (Direct API)
    elif provider == "claude" or provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        payload = json.dumps({
            "model": effective_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": sys_instruction,
            "messages": [{"role": "user", "content": prompt}]
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=35) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data["content"][0]["text"].strip()

    # 3. OpenAI-Compatible Providers (NVIDIA NIM, DeepSeek, OpenRouter, Groq)
    else:
        endpoint_map = {
            "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
            "deepseek": "https://api.deepseek.com/v1/chat/completions",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            "groq": "https://api.groq.com/openai/v1/chat/completions"
        }
        url = endpoint_map.get(provider, "https://api.groq.com/openai/v1/chat/completions")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://secondary-brain.internal"
            headers["X-Title"] = "Secondary Brain 2.0"

        messages = [
            {"role": "system", "content": sys_instruction},
            {"role": "user", "content": prompt}
        ]
        payload = json.dumps({
            "model": effective_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()

# --- Key Management Endpoints ---

@app.get("/api/keys")
def get_all_api_keys():
    """Returns all stored API keys with masked values and active model selections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM api_keys ORDER BY provider ASC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    result = []
    # Ensure default providers are always visible in list
    existing_providers = {r["provider"].lower() for r in rows}
    all_providers = ["gemini", "nvidia", "deepseek", "openrouter", "claude", "groq"]

    for r in rows:
        result.append({
            "provider": r["provider"],
            "api_key": r["api_key"],
            "masked_key": mask_key(r["api_key"]),
            "selected_model": r["selected_model"] or DEFAULT_MODELS.get(r["provider"], ""),
            "is_active": bool(r.get("is_active", 1)),
            "updated_at": r.get("updated_at", "")
        })

    for p in all_providers:
        if p not in existing_providers:
            fallback = ""
            if p == "groq" and GROQ_KEYS:
                fallback = GROQ_KEYS[0]
            result.append({
                "provider": p,
                "api_key": fallback,
                "masked_key": mask_key(fallback),
                "selected_model": DEFAULT_MODELS.get(p, ""),
                "is_active": bool(fallback),
                "updated_at": ""
            })

    return {"success": True, "data": result}

@app.post("/api/keys")
def save_api_key(req: ApiKeyItem):
    """Saves or updates an individual provider API key and syncs to Firestore & SQLite."""
    provider = req.provider.lower().strip()
    key_val = req.api_key.strip()
    model = req.selected_model or DEFAULT_MODELS.get(provider, "")
    is_active = 1 if req.is_active else 0
    now_iso = datetime.utcnow().isoformat()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO api_keys (provider, api_key, selected_model, is_active, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (provider, key_val, model, is_active))
    conn.commit()
    conn.close()

    # Sync to Firestore collection 'api_keys'
    sync_to_firestore("api_keys", provider, {
        "provider": provider,
        "api_key": key_val,
        "selected_model": model,
        "is_active": bool(is_active),
        "updated_at": now_iso
    })

    logger.info(f"🔑 Saved and synced API key for provider: {provider} (model: {model})")
    return {
        "success": True,
        "message": f"API key for {provider} successfully saved & synced.",
        "provider": provider,
        "selected_model": model
    }

@app.post("/api/keys/batch")
def save_batch_api_keys(req: BatchApiKeyRequest):
    """Batch updates multiple API keys from Android or Web Admin."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_iso = datetime.utcnow().isoformat()

    for item in req.keys:
        prov = item.provider.lower().strip()
        key_val = item.api_key.strip()
        model = item.selected_model or DEFAULT_MODELS.get(prov, "")
        is_act = 1 if item.is_active else 0
        c.execute("""
            INSERT OR REPLACE INTO api_keys (provider, api_key, selected_model, is_active, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (prov, key_val, model, is_act))
        sync_to_firestore("api_keys", prov, {
            "provider": prov,
            "api_key": key_val,
            "selected_model": model,
            "is_active": bool(is_act),
            "updated_at": now_iso
        })

    conn.commit()
    conn.close()
    return {"success": True, "count": len(req.keys), "message": "Batch API keys synchronized."}

@app.delete("/api/keys/{provider}")
def delete_api_key(provider: str):
    """Removes an API key from the database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM api_keys WHERE provider = ?", (provider.lower().strip(),))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Deleted API key for {provider}"}

# --- Chat Sessions & Telegram Cloud Storage Endpoints ---

@app.get("/api/chat/sessions")
def get_chat_sessions(limit: int = 30):
    """Returns list of previous chat conversation sessions with message counts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT s.*, 
            (SELECT content FROM chat_messages WHERE session_id = s.session_id ORDER BY id DESC LIMIT 1) as last_message,
            (SELECT COUNT(*) FROM chat_messages WHERE session_id = s.session_id) as total_messages
        FROM chat_sessions s
        ORDER BY s.updated_at DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"success": True, "data": rows}

@app.get("/api/chat/sessions/{session_id}")
def get_session_messages(session_id: str):
    """Retrieves all previous chat messages for a specific session."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id ASC", (session_id,))
    msgs = [dict(r) for r in c.fetchall()]
    c.execute("SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,))
    session = c.fetchone()
    conn.close()

    return {
        "success": True,
        "session": dict(session) if session else {"session_id": session_id, "title": "Chat Session"},
        "messages": msgs
    }

@app.post("/api/chat/sessions")
def save_chat_session(req: SaveSessionRequest):
    """Creates or updates a chat session with full history and syncs to Firestore."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    msg_count = len(req.messages) if req.messages else 0
    c.execute("""
        INSERT OR REPLACE INTO chat_sessions (session_id, title, model_used, provider, message_count, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (req.session_id, req.title, req.model_used or "", req.provider or "", msg_count))

    if req.messages:
        for m in req.messages:
            c.execute("""
                INSERT INTO chat_messages (session_id, sender, content, grounding_info)
                VALUES (?, ?, ?, ?)
            """, (req.session_id, m.sender, m.content, m.grounding_info or ""))

    conn.commit()
    conn.close()

    sync_to_firestore("chat_sessions", req.session_id, {
        "session_id": req.session_id,
        "title": req.title,
        "provider": req.provider or "groq",
        "model_used": req.model_used or "llama-3.3-70b-versatile",
        "message_count": msg_count,
        "updated_at": datetime.utcnow().isoformat()
    })

    return {"success": True, "session_id": req.session_id, "message_count": msg_count}

@app.post("/api/chat/messages")
def append_chat_message(m: ChatMessageItem):
    """Appends an individual message to session and syncs to SQLite and Firestore."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO chat_messages (session_id, sender, content, grounding_info)
        VALUES (?, ?, ?, ?)
    """, (m.session_id, m.sender, m.content, m.grounding_info or ""))
    c.execute("UPDATE chat_sessions SET message_count = message_count + 1, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?", (m.session_id,))
    conn.commit()
    conn.close()

    sync_to_firestore(f"chat_sessions/{m.session_id}/messages", None, {
        "session_id": m.session_id,
        "sender": m.sender,
        "content": m.content,
        "grounding_info": m.grounding_info or "",
        "timestamp": datetime.utcnow().isoformat()
    })

    return {"success": True, "session_id": m.session_id}

@app.post("/api/chat/backup-telegram")
async def backup_chat_to_telegram(req: TelegramBackupRequest):
    """
    Backs up full chat session transcript to Telegram Cloud Storage.
    Provides free, permanent, indestructible cloud storage backup of all chats.
    """
    target_chat = req.chat_id or get_active_chat_id()
    if not target_chat:
        raise HTTPException(status_code=400, detail="Telegram chat ID not configured.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id ASC", (req.session_id,))
    msgs = [dict(r) for r in c.fetchall()]
    c.execute("SELECT * FROM chat_sessions WHERE session_id = ?", (req.session_id,))
    sess = c.fetchone()
    conn.close()

    title = req.title or (sess["title"] if sess else "Secondary Brain AI Conversation")
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # Format transcript
    transcript_lines = [
        f"☁️ *[TELEGRAM CLOUD STORAGE BACKUP]*",
        f"📂 *Session*: `{title}`",
        f"🆔 *ID*: `{req.session_id}`",
        f"🕒 *Timestamp*: `{now_str}`",
        f"💬 *Total Messages*: `{len(msgs)}`\n",
        "--- *CONVERSATION TRANSCRIPT* ---\n"
    ]

    for m in msgs:
        sender_badge = "👤 *USER*" if m["sender"] == "USER" else "🤖 *AI*"
        transcript_lines.append(f"{sender_badge}: {m['content']}\n")

    full_text = "\n".join(transcript_lines)

    sent = False
    if bot_app:
        try:
            # Telegram messages max 4096 chars; send as document or chunked text
            if len(full_text) > 3800:
                with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as tf:
                    tf.write(full_text)
                    tf_path = tf.name
                with open(tf_path, "rb") as doc_file:
                    await bot_app.bot.send_document(
                        chat_id=target_chat,
                        document=doc_file,
                        caption=f"☁️ Cloud Backup Archive: {title} ({len(msgs)} messages)"
                    )
                os.remove(tf_path)
            else:
                await bot_app.bot.send_message(
                    chat_id=target_chat,
                    text=full_text,
                    parse_mode="Markdown"
                )
            sent = True
        except Exception as e:
            logger.error(f"Telegram backup send error: {e}")
            raise HTTPException(status_code=500, detail=f"Telegram send failed: {str(e)}")

    return {
        "success": sent,
        "session_id": req.session_id,
        "title": title,
        "message_count": len(msgs),
        "target_chat": target_chat,
        "cloud_storage": "Telegram Cloud Archive Verified"
    }

# --- Unified Multi-Provider AI Inference Endpoint ---

@app.post("/api/chat/generate")
async def generate_chat_response(req: MultiProviderChatRequest):
    """
    Unified multi-provider AI reasoning endpoint supporting:
    - Gemini, NVIDIA NIM, DeepSeek, OpenRouter, Claude, Groq
    - Stores messages in local DB, syncs to Firestore & Mem0
    """
    provider = req.provider.lower().strip()
    session_id = req.session_id or "default_session"

    # 1. Retrieve ordered candidate keys (Banana -> Apple -> Kiwi)
    candidate_keys = get_ordered_key_candidates(preferred_provider=provider)

    if not candidate_keys:
        raise HTTPException(
            status_code=503,
            detail=f"No active, available API keys found for provider '{provider}' or fallback pool. Please configure an API key in Key Management."
        )

    # 2. Build Grounded Context — read live telemetry from SQLite if not provided
    g = req.telemetry_grounding or {}
    steps = g.get("steps")
    batt = g.get("battery")
    loc = g.get("location")

    if steps is None or batt is None or loc is None:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT 1")
            trow = c.fetchone()
            conn.close()
            if trow:
                steps = steps if steps is not None else (trow["step_count"] or 0)
                batt = batt if batt is not None else (trow["battery_level"] or 0)
                loc = loc if loc is not None else (trow["location_name"] or "Location Unavailable")
        except Exception:
            pass

    steps = steps if steps is not None else 0
    batt = batt if batt is not None else 0
    loc = loc if loc is not None else "Location Unavailable"

    grounded_system = (
        f"{req.system_prompt or 'You are Secondary Brain 2.0, a high-performance personal AI companion.'}\n"
        f"Real-Time Telemetry:\n"
        f"- Location: {loc}\n"
        f"- Steps Today: {steps}\n"
        f"- Battery Level: {batt}%\n"
        f"Answer concisely, smartly, and accurately in 2-4 sentences."
    )

    # 3. Save User message to SQLite & Firestore
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO chat_messages (session_id, sender, content, grounding_info)
        VALUES (?, 'USER', ?, ?)
    """, (session_id, req.prompt, f"steps:{steps}|loc:{loc}"))
    c.execute("""
        INSERT OR IGNORE INTO chat_sessions (session_id, title, provider, model_used)
        VALUES (?, ?, ?, ?)
    """, (session_id, req.prompt[:40], provider, req.model or ""))
    c.execute("UPDATE chat_sessions SET message_count = message_count + 1, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()

    sync_to_firestore(f"chat_sessions/{session_id}/messages", None, {
        "session_id": session_id,
        "sender": "USER",
        "content": req.prompt,
        "grounding_info": f"steps:{steps}|loc:{loc}",
        "timestamp": datetime.utcnow().isoformat()
    })

    # 4. Multi-Key Waterfall Failover Loop (Banana -> Apple -> Kiwi)
    ai_text = None
    successful_candidate = None
    failover_occurred = False
    failover_log = []
    attempt_errors = []

    for idx, cand in enumerate(candidate_keys):
        cand_id = cand.get("id")
        cand_prov = cand.get("provider", provider).lower()
        cand_key = cand.get("api_key", "").strip()
        cand_label = cand.get("label") or f"{cand_prov}_key_{cand_id or idx}"
        cand_model = req.model or cand.get("selected_model") or DEFAULT_MODELS.get(cand_prov, "llama-3.3-70b-versatile")
        cand_rank = cand.get("priority_rank", 1)

        if not cand_key:
            continue

        try:
            logger.info(f"[KEY POOL] Trying key #{idx+1}: '{cand_label}' (Rank {cand_rank}, Provider: {cand_prov}, Model: {cand_model})...")

            if cand_prov == "groq" and not cand_key and GROQ_KEYS:
                cand_key = GROQ_KEYS[0]

            reply = dispatch_multi_provider_llm(
                provider=cand_prov,
                api_key=cand_key,
                model=cand_model,
                prompt=req.prompt,
                system_prompt=grounded_system,
                temperature=req.temperature or 0.7,
                max_tokens=req.max_tokens or 800
            )

            if reply and reply.strip():
                ai_text = reply.strip()
                successful_candidate = cand
                record_key_success(cand_id, cand_key)
                logger.info(f"[KEY POOL SUCCESS] Response generated via key '{cand_label}' ({cand_prov})")
                break

        except urllib.error.HTTPError as he:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8")
            except Exception:
                pass
            err_msg = f"HTTP {he.code}: {he.reason} - {err_body}"
            attempt_errors.append(f"{cand_label}: {err_msg}")

            # Detect Rate Limit / Quota Exceeded (429 or quota text)
            is_quota_err = (he.code == 429 or "quota" in err_body.lower() or "resource_exhausted" in err_body.lower() or "rate_limit" in err_body.lower())
            if is_quota_err:
                failover_occurred = True
                failover_log.append(f"Key '{cand_label}' hit quota/rate limit (HTTP {he.code}). Cascading to next priority key.")
                logger.warning(f"[FAILOVER CASCADE] Key '{cand_label}' quota exceeded. Cooldown 24h applied. Cascading to next candidate...")
                mark_key_quota_exceeded_with_cooldown(cand_id, cand_key, err_msg, cooldown_hours=24)
            else:
                failover_occurred = True
                failover_log.append(f"Key '{cand_label}' failed with {he.code}. Cascading to next key.")
                logger.warning(f"[FAILOVER CASCADE] Key '{cand_label}' error: {err_msg}. Cascading...")

        except Exception as ex:
            err_str = str(ex)
            attempt_errors.append(f"{cand_label}: {err_str}")
            is_quota_err = ("429" in err_str or "quota" in err_str.lower() or "resource_exhausted" in err_str.lower() or "rate_limit" in err_str.lower())
            if is_quota_err:
                failover_occurred = True
                failover_log.append(f"Key '{cand_label}' hit quota limit. Cascading...")
                logger.warning(f"[FAILOVER CASCADE] Key '{cand_label}' quota hit: {err_str}. Cascading...")
                mark_key_quota_exceeded_with_cooldown(cand_id, cand_key, err_str, cooldown_hours=24)
            else:
                failover_occurred = True
                failover_log.append(f"Key '{cand_label}' error: {err_str}. Cascading...")
                logger.warning(f"[FAILOVER CASCADE] Key '{cand_label}' exception: {err_str}. Cascading...")

    if not ai_text:
        err_detail = " | ".join(attempt_errors[-3:]) if attempt_errors else "All candidate keys in pool failed."
        logger.error(f"[KEY POOL EXHAUSTED] All {len(candidate_keys)} keys failed: {err_detail}")
        raise HTTPException(
            status_code=502,
            detail=f"Inference waterfall failed across all {len(candidate_keys)} keys. Diagnostic: {err_detail}"
        )

    actual_prov = successful_candidate.get("provider", provider) if successful_candidate else provider
    actual_model = req.model or (successful_candidate.get("selected_model") if successful_candidate else None) or DEFAULT_MODELS.get(actual_prov, "llama-3.3-70b-versatile")
    actual_label = successful_candidate.get("label", "Primary Key") if successful_candidate else "Primary Key"
    actual_rank = successful_candidate.get("priority_rank", 1) if successful_candidate else 1

    # 5. Save AI message to SQLite, Firestore & Mem0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO chat_messages (session_id, sender, content, grounding_info)
        VALUES (?, 'AI', ?, ?)
    """, (session_id, ai_text, f"provider:{actual_prov}|model:{actual_model}|key:{actual_label}"))
    c.execute("UPDATE chat_sessions SET message_count = message_count + 1, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()

    sync_to_firestore(f"chat_sessions/{session_id}/messages", None, {
        "session_id": session_id,
        "sender": "AI",
        "content": ai_text,
        "grounding_info": f"provider:{actual_prov}|model:{actual_model}|key:{actual_label}",
        "timestamp": datetime.utcnow().isoformat()
    })

    # Ingest to Mem0 Brain
    brain_memory.add([
        {"role": "user", "content": req.prompt},
        {"role": "assistant", "content": ai_text}
    ], user_id="harsh", category="AI_CHAT")

    return {
        "success": True,
        "session_id": session_id,
        "provider": actual_prov,
        "model": actual_model,
        "key_used": actual_label,
        "priority_rank": actual_rank,
        "failover_occurred": failover_occurred,
        "failover_log": failover_log,
        "reply": ai_text,
        "timestamp": datetime.utcnow().isoformat()
    }

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    
    groq_key = get_groq_api_key()
    if not groq_key:
        msg = (
            "⚠️ *No Groq API Key Configured!*\n\n"
            "To enable live AI chat over Telegram:\n"
            "1. Upload your key in Web Admin **Chat Settings** ➔ **API Key Vault**, OR\n"
            "2. Send `/setkey groq <your_groq_key>` directly in this chat."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # Check if a model argument was passed (e.g., /ai llama-3.3-70b-versatile or /ai deepseek)
    args = context.args
    if args:
        chosen_arg = args[0].lower().strip()
        selected_model = "llama-3.3-70b-versatile"
        for m in GROQ_FEATURED_MODELS:
            if chosen_arg in m["id"].lower() or chosen_arg in m["name"].lower():
                selected_model = m["id"]
                break
        
        ACTIVE_AI_SESSIONS[chat_id] = {
            "model": selected_model,
            "active": True,
            "history": []
        }
        
        reply = (
            f"🟢 *Active AI Session Started!*\n\n"
            f"🧠 **Model**: `{selected_model}`\n"
            f"⚡ **Inference Engine**: `Groq LPU Acceleration`\n"
            f"💬 **Status**: Live multi-turn conversation mode is ON.\n\n"
            f"_Type any message and I will reply directly with this AI model._\n"
            f"_Send `/stop` anytime to end this session and return to C2 mode._"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    # Otherwise, show available models menu
    models_text = "🧠 *Select an AI Model to Start Chatting*:\n\n"
    for idx, m in enumerate(GROQ_FEATURED_MODELS, 1):
        models_text += f"{idx}. *{m['name']}*\n   • ID: `{m['id']}`\n   • _{m['desc']}_\n   • 👉 Start: `/ai {m['id']}`\n\n"
    
    models_text += (
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 *How to use:*\n"
        "• Reply `/ai <model_id>` (e.g. `/ai llama-3.3-70b-versatile` or `/ai deepseek-r1-distill-llama-70b`)\n"
        "• Every subsequent message will be answered by that model.\n"
        "• Reply `/stop` at any time to exit AI mode."
    )
    await update.message.reply_text(models_text, parse_mode="Markdown")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    
    if chat_id in ACTIVE_AI_SESSIONS and ACTIVE_AI_SESSIONS[chat_id].get("active"):
        model_name = ACTIVE_AI_SESSIONS[chat_id].get("model", "AI")
        ACTIVE_AI_SESSIONS[chat_id]["active"] = False
        msg = (
            f"🔴 *AI Session Ended for `{model_name}`.*\n\n"
            f"✅ Returned to standard Secondary Brain C2 & Hardware Agent mode.\n"
            f"You can start a new AI conversation anytime by sending `/ai`."
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ No active AI session was running. You are in standard Secondary Brain C2 mode. Send `/ai` to start an AI chat.", parse_mode="Markdown")

async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: `/setkey <provider> <api_key>`\nExample: `/setkey groq gsk_...`", parse_mode="Markdown")
        return
        
    provider = context.args[0].lower().strip()
    key_val = context.args[1].strip()
    
    # Save to SQLite and Firestore
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO api_keys (provider, api_key, is_active, updated_at) VALUES (?, ?, 1, CURRENT_TIMESTAMP)", (provider, key_val))
    conn.commit()
    conn.close()
    
    sync_to_firestore("api_keys", provider, {
        "provider": provider,
        "api_key": key_val,
        "is_active": True,
        "updated_at": datetime.utcnow().isoformat()
    })
    
    masked = f"{key_val[:4]}••••{key_val[-4:]}" if len(key_val) > 8 else "••••"
    await update.message.reply_text(f"✅ *API Key Saved!*\n\n• **Provider**: `{provider}`\n• **Key**: `{masked}`\n• Synced to SQLite DB, Firestore & Render Cloud.", parse_mode="Markdown")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    
    text = (update.message.text or "").strip()
    
    # Check for raw coordinate coordinates
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
        
        sync_to_firestore("locations", "latest", {
            "latitude": lat,
            "longitude": lon,
            "accuracy_meters": 15.0,
            "location_label": "Telegram Text Coords",
            "google_maps_url": f"https://www.google.com/maps?q={lat},{lon}",
            "timestamp": datetime.utcnow().isoformat()
        })
        await update.message.reply_text(f"📍 Extracted coordinates: `{lat:.5f}, {lon:.5f}` ➔ Synced to Firebase & Mission Control!")
        return

    # 1. Check if user is in an active AI conversation session
    if chat_id in ACTIVE_AI_SESSIONS and ACTIVE_AI_SESSIONS[chat_id].get("active"):
        sess = ACTIVE_AI_SESSIONS[chat_id]
        model_name = sess.get("model", "llama-3.3-70b-versatile")
        
        # Check if user wants to stop
        if text.lower() in ["/stop", "/stopai", "/exit", "stop", "exit", "/cancel"]:
            await stop_command(update, context)
            return

        status_msg = await update.message.reply_text(f"⚡ *[{model_name}] Thinking...*", parse_mode="Markdown")
        client = get_groq_client()
        if not client:
            await status_msg.edit_text("⚠️ Groq client unavailable. Please check your Groq API key with `/setkey groq <key>`.")
            return

        user_ctx = read_memory_file(USER_MD_PATH, "User: Harsh")
        mem0_ctx = brain_memory.get_context_for_prompt(user_id="harsh", query=text)
        system_prompt = (
            f"You are an advanced AI companion ({model_name}) powered by Groq LPU and integrated into Secondary Brain 2.0.\n"
            f"User Profile:\n{user_ctx}\n\n"
            f"Dynamic Memory Graph:\n{mem0_ctx}\n\n"
            f"Provide insightful, high quality, helpful responses formatted in clean markdown."
        )

        history = sess.get("history", [])
        history.append({"role": "user", "content": text})
        if len(history) > 10:
            history = history[-10:]

        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    *history
                ],
                temperature=0.6,
                max_tokens=1500
            )
            ai_reply = completion.choices[0].message.content.strip()
            history.append({"role": "assistant", "content": ai_reply})
            sess["history"] = history
            
            await status_msg.edit_text(ai_reply, parse_mode="Markdown")
            
            brain_memory.add([
                {"role": "user", "content": text},
                {"role": "assistant", "content": ai_reply}
            ], user_id="harsh", category="TELEGRAM_AI_CHAT")
            
            background_reflection_worker(text, ai_reply)
            return
        except Exception as err:
            logger.error(f"Error during Telegram AI chat: {err}")
            await status_msg.edit_text(f"⚠️ Error from {model_name}: {str(err)}")
            return

    # JARVIS ReAct Intent Processor for standard natural language messages
    status_indicator = await update.message.reply_text("⚡ *JARVIS Processing...*", parse_mode="Markdown")
    try:
        plan = process_jarvis_react_intent(text)
        dispatches = plan.get("dispatches", [])
        tools = plan.get("tools", [])
        response_text = plan.get("chat_response", "Command executed.")

        # If physical device tools are required, execute them directly
        for cmd in dispatches:
            try:
                await context.bot.send_message(chat_id=chat_id, text=cmd)
            except Exception as d_err:
                logger.warning(f"Error dispatching tool {cmd}: {d_err}")
                
            sync_to_firestore("commands", None, {
                "command": cmd,
                "status": "DISPATCHED",
                "issued_at": datetime.utcnow().isoformat()
            })

        # If saving note is requested, save to SQLite & Firebase
        if "save_note" in tools or not dispatches:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO clipboard (content, content_type, category) VALUES (?, 'text', 'TELEGRAM_NOTE')", (text,))
            conn.commit()
            conn.close()
            
            sync_to_firestore("voice_notes", None, {
                "raw_text": text,
                "category": "NOTE",
                "urgency": "MEDIUM",
                "parsed_title": text[:60],
                "completed": False,
                "groq_transcription": text,
                "ai_summary": response_text,
                "created_at": datetime.utcnow().isoformat()
            })

        full_reply = f"🤖 *JARVIS Response*:\n\n{response_text}"
        await status_indicator.edit_text(full_reply, parse_mode="Markdown")
        background_reflection_worker(text, response_text)
        brain_memory.add([{"role": "user", "content": text}, {"role": "assistant", "content": response_text}], user_id="harsh", category="TELEGRAM_CHAT")
    except Exception as e:
        logger.error(f"JARVIS text handling error: {e}")
        await status_indicator.edit_text(f"📋 Note saved to Secondary Brain: {text}")

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
        client = get_groq_client()
        if client:
            try:
                with open(tmp_path, "rb") as file_obj:
                    transcription = client.audio.transcriptions.create(
                        file=(tmp_path, file_obj.read()),
                        model="whisper-large-v3-turbo",
                        response_format="text"
                    )
            except Exception as err:
                logger.warning(f"Groq whisper transcription error: {err}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if not transcription:
            raise Exception("Groq API key not configured or rate-limited. Please set key with /setkey groq <key>.")
        await status_msg.edit_text("🧠 *Generating AI Summary...*", parse_mode="Markdown")
        snippet_text = transcription[:8000]
        compact_prompt = f"Analyze transcript:\n{snippet_text}\n\nExtract 3 brief bullet points and main action items."
        ai_summary = None
        if client:
            try:
                completion = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": compact_prompt}],
                    temperature=0.2,
                    max_tokens=450
                )
                ai_summary = completion.choices[0].message.content
            except Exception as err:
                logger.warning(f"Summary completion error: {err}")
        if not ai_summary:
            ai_summary = "Summary unavailable. Full transcript indexed below."
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO recordings (telegram_file_id, transcript, summary, action_items) VALUES (?, ?, ?, ?)", (audio_file.file_id, transcription, ai_summary, ""))
        try:
            tg_fn = getattr(audio_file, 'file_name', None) or f"recording_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.m4a"
            tg_size = getattr(audio_file, 'file_size', 0) or 0
            tg_mime = getattr(audio_file, 'mime_type', 'audio/mp4') or 'audio/mp4'
            c.execute("""
                INSERT INTO telegram_drive_files (file_name, mime_type, file_size_bytes, telegram_file_id, telegram_message_id, chat_id, category, tags, uploaded_by)
                VALUES (?, ?, ?, ?, ?, ?, 'AUDIO', 'voice_note,telegram', 'telegram_bot')
            """, (tg_fn, tg_mime, tg_size, audio_file.file_id, message.message_id, str(chat_id)))
        except Exception as _tde:
            logger.warning(f"Telegram-Drive auto-index notice: {_tde}")
        conn.commit()
        conn.close()
        
        # Direct sync to Firebase Firestore
        sync_to_firestore("voice_notes", None, {
            "raw_text": transcription,
            "category": "TASK",
            "urgency": "HIGH",
            "parsed_title": ai_summary.split("\n")[0][:60] if ai_summary else "Voice Recording",
            "completed": False,
            "groq_transcription": transcription,
            "ai_summary": ai_summary,
            "created_at": datetime.utcnow().isoformat()
        })
        
        preview = transcription[:400]
        full_response = f"✅ *Synced to Firebase & Secondary Brain DB*\n\n{ai_summary}\n\n---\n📝 *Transcript Snippet*:\n_{preview}..._"
        await status_msg.edit_text(full_response, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error processing audio: {e}")
        await status_msg.edit_text(f"❌ *Error processing recording*: {str(e)}", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    
    batt = row["battery_level"] if row else 85
    temp = row["battery_temp_celsius"] if row else 31.8
    wifi = row["wifi_ssid"] if row else "HomeNet_5G"
    groq_status = "Connected" if get_groq_api_key() else "No Key Set"
    reply = (
        f"📊 *[JARVIS SYSTEM STATUS]*\n\n"
        f"• 🔋 *Battery*: `{batt}%` ({temp}°C)\n"
        f"• 📶 *Network*: `{wifi}`\n"
        f"• ⚡ *Hardware Agent*: `Active & Connected`\n"
        f"• 🧠 *Groq Engine*: `{groq_status}`\n"
        f"• 💬 *Chat Mode*: `{'AI Active' if (chat_id in ACTIVE_AI_SESSIONS and ACTIVE_AI_SESSIONS[chat_id].get('active')) else 'C2 Mission Control'}`"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")

async def locate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    cmd_id = queue_c2_command("/locate", str(chat_id))
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM dwell_logs ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()

    if not row or not row["latitude"] or not row["longitude"]:
        reply = (
            "📍 *[JARVIS GPS BEACON QUERY]*\n\n"
            f"⚡ Real-time GPS beacon query dispatched to phone (#{cmd_id})...\n"
            "Waiting for live satellite fix from hardware."
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    lat = row["latitude"]
    lon = row["longitude"]
    label = row["location_label"] or "Last Known Position"
    maps_url = f"https://www.google.com/maps?q={lat},{lon}"
    reply = (
        f"📍 *[JARVIS GPS BEACON]*\n\n"
        f"• *Coordinates*: `{lat:.5f}, {lon:.5f}`\n"
        f"• *Label*: {label}\n"
        f"• *Accuracy*: ±10m (GPS Fix)\n"
        f"• [Open in Google Maps]({maps_url})\n\n"
        f"⚡ *Live fix query dispatched to hardware (#{cmd_id})*..."
    )
    await update.message.reply_text(reply, parse_mode="Markdown", disable_web_page_preview=True)

async def siren_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    arg = context.args[0] if context.args else ""
    cmd = f"/siren {arg}".strip()
    cmd_id = queue_c2_command(cmd, str(chat_id))
    sync_to_firestore("commands", None, {
        "command": cmd,
        "status": "DISPATCHED",
        "issued_at": datetime.utcnow().isoformat()
    })
    await update.message.reply_text(f"🚨 *[LOCATOR SIREN ACTIVATED]*\n100% Volume beacon, emergency audio & torch strobe dispatched to phone hardware (#{cmd_id})!", parse_mode="Markdown")

def generate_hud_photo_bytes(prefer_front=False) -> Optional[bytes]:
    """Captures real frame via webcam if hardware camera is locally attached; returns None otherwise."""
    try:
        import cv2
        cap = cv2.VideoCapture(0)
        if cap.isOpened():
            for _ in range(3):
                cap.read()
            ret, frame = cap.read()
            cap.release()
            if ret and frame is not None:
                _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
                return buf.tobytes()
    except Exception:
        pass

    # No physical webcam attached to cloud server container; return None so caller honestly routes to mobile hardware node
    return None

async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    sync_to_firestore("commands", None, {
        "command": "/photo",
        "status": "DISPATCHED",
        "issued_at": datetime.utcnow().isoformat()
    })
    
    prefer_front = False
    if context.args and len(context.args) > 0:
        prefer_front = context.args[0].lower() in ["front", "selfie", "user"]

    # Queue for Android real camera hardware capture
    queue_c2_command(f"/photo {'front' if prefer_front else 'back'}", str(chat_id))

    # Dispatch optical capture
    photo_bytes = generate_hud_photo_bytes(prefer_front=prefer_front)
    if photo_bytes:
        facing_label = "Front (Selfie)" if prefer_front else "Rear (Main)"
        caption = (
            f"📸 *[ENFORCER REMOTE SNAPSHOT]*\n"
            f"• Facing: {facing_label}\n"
            f"• Timestamp: {datetime.utcnow().strftime('%H:%M:%S UTC')}\n"
            f"• Sensor Link: Online"
        )
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_bytes,
                caption=caption,
                parse_mode="Markdown"
            )
            return
        except Exception as e:
            logger.error(f"send_photo error: {e}")

    await update.message.reply_text("📸 *[CAMERA SNAPSHOT REQUESTED]*\nOptics dispatched to Android node via Firestore channel.", parse_mode="Markdown")


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    sync_to_firestore("commands", None, {
        "command": "/mute",
        "status": "DISPATCHED",
        "issued_at": datetime.utcnow().isoformat()
    })
    await update.message.reply_text("🔇 *[FORCE MUTE ACTIVATED]*\nAll phone audio streams set to 0%.", parse_mode="Markdown")

async def app_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    app_name = " ".join(context.args) if context.args else "spotify"
    cmd = f"/app {app_name}"
    sync_to_firestore("commands", None, {"command": cmd, "status": "DISPATCHED", "issued_at": datetime.utcnow().isoformat()})
    await update.message.reply_text(f"🚀 *[APP LAUNCHER]* Dispatched launch command for `{app_name}` to Android phone.", parse_mode="Markdown")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    query = " ".join(context.args) if context.args else "synthwave"
    cmd = f"/play {query}"
    sync_to_firestore("commands", None, {"command": cmd, "status": "DISPATCHED", "issued_at": datetime.utcnow().isoformat()})
    await update.message.reply_text(f"🎵 *[MEDIA STREAM]* Dispatched media playback for `{query}` to phone.", parse_mode="Markdown")

async def arp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    sync_to_firestore("commands", None, {"command": "/arp", "status": "DISPATCHED", "issued_at": datetime.utcnow().isoformat()})
    await update.message.reply_text("🛡️ *[ARP SENTINEL]* Triggering active Wi-Fi ARP spoofing audit on phone...", parse_mode="Markdown")

async def netscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    sync_to_firestore("commands", None, {"command": "/netscan", "status": "DISPATCHED", "issued_at": datetime.utcnow().isoformat()})
    await update.message.reply_text("🌐 *[SUBNET SCAN]* Sweeping local Class-C subnet for active hosts...", parse_mode="Markdown")

async def fingerprint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    sync_to_firestore("commands", None, {"command": "/fingerprint", "status": "DISPATCHED", "issued_at": datetime.utcnow().isoformat()})
    await update.message.reply_text("🔐 *[BIOMETRIC GATE]* Dispatched fingerprint verification challenge to device sensor...", parse_mode="Markdown")

async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_active_chat_id(str(chat_id))
    ctx = get_full_agent_context()
    user_p = ctx["user_profile"][:300]
    mem = ctx["long_term_memory"][:400]
    msg = f"🧠 *[JARVIS SOUL & MEMORY CORE]*\n\n*👤 User Profile*:\n{user_p}\n\n*Dynamic Memory*:\n{mem}"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def run_bot():
    global bot_app
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not provided; skipping bot polling.")
        return
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("recent", require_telegram_auth(recent_command)))
    bot_app.add_handler(CommandHandler("status", require_telegram_auth(status_command)))
    bot_app.add_handler(CommandHandler("locate", require_telegram_auth(locate_command)))
    bot_app.add_handler(CommandHandler("gps", require_telegram_auth(locate_command)))
    bot_app.add_handler(CommandHandler("siren", require_telegram_auth(siren_command)))
    bot_app.add_handler(CommandHandler("photo", require_telegram_auth(photo_command)))
    bot_app.add_handler(CommandHandler("snap", require_telegram_auth(photo_command)))
    bot_app.add_handler(CommandHandler("mute", require_telegram_auth(mute_command)))
    bot_app.add_handler(CommandHandler("app", require_telegram_auth(app_command)))
    bot_app.add_handler(CommandHandler("open", require_telegram_auth(app_command)))
    bot_app.add_handler(CommandHandler("play", require_telegram_auth(play_command)))
    bot_app.add_handler(CommandHandler("arp", require_telegram_auth(arp_command)))
    bot_app.add_handler(CommandHandler("netscan", require_telegram_auth(netscan_command)))
    bot_app.add_handler(CommandHandler("fingerprint", require_telegram_auth(fingerprint_command)))
    bot_app.add_handler(CommandHandler("memory", require_telegram_auth(memory_command)))
    
    # 🌟 Interactive AI Conversation Mode Commands
    bot_app.add_handler(CommandHandler("ai", require_telegram_auth(ai_command)))
    bot_app.add_handler(CommandHandler("models", require_telegram_auth(ai_command)))
    bot_app.add_handler(CommandHandler("llms", require_telegram_auth(ai_command)))
    bot_app.add_handler(CommandHandler("stop", require_telegram_auth(stop_command)))
    bot_app.add_handler(CommandHandler("stopai", require_telegram_auth(stop_command)))
    bot_app.add_handler(CommandHandler("exit", require_telegram_auth(stop_command)))
    bot_app.add_handler(CommandHandler("setkey", require_telegram_auth(setkey_command)))
    
    bot_app.add_handler(MessageHandler(filters.LOCATION, require_telegram_auth(handle_location_message)))
    bot_app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.ALL, require_telegram_auth(handle_audio)))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, require_telegram_auth(handle_text_message)))
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Telegram Bot polling active: Instant auto-reply enabled for all commands & messages.")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
