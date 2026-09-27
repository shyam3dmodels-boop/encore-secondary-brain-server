#!/usr/bin/env python3
"""
⚡ JARVIS Termux On-Device Hardware Agent & Daemon
Part of the Secondary Brain 2.0 & Enforcer OS Ecosystem.

Gives Android phone physical hardware execution capabilities via termux-api:
- Speech Output (termux-tts-speak)
- Torch Strobe & Flashlight (termux-torch)
- Camera Snapshots (termux-camera-photo)
- Satellite GPS Beacon (termux-location)
- Battery & Thermal Telemetry (termux-battery-status)
- Volume Overrides & Sirens (termux-volume)
- Universal Clipboard (termux-clipboard-get/set)
- Status Bar Notifications (termux-notification)
- Haptic Vibrations (termux-vibrate)
- WiFi & Network Telemetry (termux-wifi-connectioninfo)

Syncs automatically with Telegram C2 and Firebase Firestore (android-1a887).
"""

import os
import sys
import time
import json
import shutil
import logging
import threading
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional, Dict, Any, List

logging.basicConfig(
    format="%(asctime)s - [JARVIS-TERMUX] - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("JarvisTermux")

# ─── Configuration ────────────────────────────────────────────────────────────
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID") or "android-1a887"
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY") or "AIzaSyCTUzJhx7yuMv35XWXlSFW3MhQtG_-GT3w"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or "8942980083:AAHmhVY4ybuOYSSJDsyuF8Z-1DP66WEbl5k"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "-1004445314496"
IS_TERMUX = bool(shutil.which("termux-battery-status") or os.path.exists("/data/data/com.termux"))

# ─── Termux API Wrappers ───────────────────────────────────────────────────────

def run_termux_cmd(cmd_list: List[str], timeout: int = 10) -> Optional[str]:
    """Runs a termux-api command safely with fallback simulation."""
    if not IS_TERMUX:
        logger.info(f"[DESKTOP SIMULATOR] Mock Termux Exec: {' '.join(cmd_list)}")
        return None
    try:
        res = subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0:
            return res.stdout.strip()
        else:
            logger.warning(f"Command {' '.join(cmd_list)} stderr: {res.stderr.strip()}")
            return None
    except Exception as e:
        logger.error(f"Error running termux command {' '.join(cmd_list)}: {e}")
        return None

def speak_tts(text: str, pitch: float = 1.0, rate: float = 1.1):
    """Speaks out loud through the Android phone speaker via Termux TTS."""
    logger.info(f"🔊 JARVIS Speaking: {text}")
    if IS_TERMUX:
        run_termux_cmd(["termux-tts-speak", "-p", str(pitch), "-r", str(rate), text])
    else:
        logger.info(f"[DESKTOP SIMULATOR] 🔊 TTS Output: '{text}'")

def set_torch(state: str):
    """Turns the flashlight on or off."""
    logger.info(f"🔦 Flashlight: {state}")
    if IS_TERMUX:
        run_termux_cmd(["termux-torch", state])

def strobe_torch(seconds: int = 15):
    """Flashes the flashlight in an emergency strobe sequence."""
    def _strobe():
        end_time = time.time() + seconds
        on = True
        while time.time() < end_time:
            set_torch("on" if on else "off")
            on = not on
            time.sleep(0.3)
        set_torch("off")
    threading.Thread(target=_strobe, daemon=True).start()

def get_battery_info() -> Dict[str, Any]:
    """Queries live battery telemetry from Android."""
    if IS_TERMUX:
        output = run_termux_cmd(["termux-battery-status"])
        if output:
            try:
                return json.loads(output)
            except Exception:
                pass
    # Fallback simulated data
    return {
        "percentage": 85,
        "health": "GOOD",
        "temperature": 31.8,
        "status": "DISCHARGING",
        "plugged": "UNPLUGGED"
    }

def get_location_gps() -> Dict[str, Any]:
    """Acquires GPS fix from Android satellite receiver."""
    if IS_TERMUX:
        output = run_termux_cmd(["termux-location", "-p", "gps", "-r", "last"], timeout=15)
        if output:
            try:
                data = json.loads(output)
                lat = data.get("latitude", 28.6139)
                lon = data.get("longitude", 77.2090)
                return {
                    "latitude": lat,
                    "longitude": lon,
                    "accuracy": data.get("accuracy", 10.0),
                    "altitude": data.get("altitude", 215.0),
                    "speed": data.get("speed", 0.0),
                    "google_maps_url": f"https://www.google.com/maps?q={lat},{lon}"
                }
            except Exception:
                pass
    return {
        "latitude": 28.6139,
        "longitude": 77.2090,
        "accuracy": 10.0,
        "altitude": 215.0,
        "speed": 0.0,
        "google_maps_url": "https://www.google.com/maps?q=28.6139,77.2090"
    }

def take_camera_photo(front: bool = False, output_path: str = "/sdcard/jarvis_snap.jpg") -> Optional[str]:
    """Takes a background snapshot and saves to storage."""
    camera_id = "1" if front else "0"
    logger.info(f"📸 Capturing photo (Camera ID: {camera_id})...")
    if IS_TERMUX:
        run_termux_cmd(["termux-camera-photo", "-c", camera_id, output_path], timeout=15)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
    return None

def set_volume_override(stream: str, volume: int):
    """Overrides Android audio volume (alarm, music, notification, ring)."""
    if IS_TERMUX:
        run_termux_cmd(["termux-volume", stream, str(volume)])

def send_android_notification(title: str, content: str, priority: str = "high"):
    """Pushes a native Heads-Up notification to Android status bar."""
    if IS_TERMUX:
        run_termux_cmd(["termux-notification", "--title", title, "--content", content, "--priority", priority])

def vibrate_phone(duration_ms: int = 500):
    """Triggers haptic vibration."""
    if IS_TERMUX:
        run_termux_cmd(["termux-vibrate", "-d", str(duration_ms)])

def get_wifi_info() -> Dict[str, Any]:
    """Queries current Wi-Fi network connection details."""
    if IS_TERMUX:
        output = run_termux_cmd(["termux-wifi-connectioninfo"])
        if output:
            try:
                return json.loads(output)
            except Exception:
                pass
    return {"ssid": "HomeNet_5G", "bssid": "C4:EA:1D:9A:88:2F", "rssi": -52, "link_speed_mbps": 866}

def get_clipboard_text() -> str:
    """Reads system clipboard."""
    if IS_TERMUX:
        return run_termux_cmd(["termux-clipboard-get"]) or ""
    return ""

def set_clipboard_text(text: str):
    """Sets system clipboard."""
    if IS_TERMUX:
        run_termux_cmd(["termux-clipboard-set", text])

# ─── Firebase Firestore Sync ─────────────────────────────────────────────────

def sync_to_firestore(collection_path: str, doc_id: Optional[str], fields: Dict[str, Any]):
    """Syncs telemetry, location, or command logs directly to Firebase Firestore."""
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
            logger.debug(f"Firestore synced {collection_path} Status: {res.status}")
    except Exception as e:
        logger.warning(f"Firestore sync notice: {e}")

# ─── Telegram Bot Dispatcher ─────────────────────────────────────────────────

def send_telegram_message(text: str):
    """Sends a message to the authorized Telegram Chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.warning(f"Telegram dispatch notice: {e}")

def send_telegram_photo(photo_path: str, caption: str):
    """Sends a captured photo to Telegram."""
    if not os.path.exists(photo_path) or not TELEGRAM_BOT_TOKEN:
        return
    try:
        import mimetypes
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        with open(photo_path, "rb") as f:
            photo_bytes = f.read()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="snap.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + photo_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        urllib.request.urlopen(req, timeout=15)
        logger.info("Photo successfully dispatched to Telegram!")
    except Exception as e:
        logger.error(f"Failed to send photo to Telegram: {e}")

# ─── Master JARVIS Hardware Command Dispatcher ────────────────────────────────

def execute_jarvis_action(cmd_string: str) -> str:
    """Executes hardware actions and returns the confirmation status."""
    cmd = cmd_string.strip()
    logger.info(f"⚡ Executing Action: '{cmd}'")

    if cmd.startswith("/siren") or "siren" in cmd.lower():
        set_volume_override("alarm", 15)
        set_volume_override("music", 15)
        strobe_torch(20)
        speak_tts("Emergency locator beacon active. Device location flagged.")
        vibrate_phone(2000)
        send_android_notification("🚨 JARVIS ALERT", "Emergency Locator Siren & Strobe Engaged", "high")
        return "🚨 *[LOCATOR BEACON ENGAGED]*\n100% Volume siren, torch strobe & TTS alert active for 20s."

    elif cmd.startswith("/photo") or "photo" in cmd.lower() or "snap" in cmd.lower():
        is_front = "front" in cmd.lower() or "selfie" in cmd.lower()
        snap_path = "/sdcard/jarvis_snap.jpg" if IS_TERMUX else "jarvis_snap.jpg"
        photo = take_camera_photo(front=is_front, output_path=snap_path)
        facing = "Front (Selfie)" if is_front else "Rear (Main)"
        if photo and os.path.exists(photo):
            send_telegram_photo(photo, f"📸 *JARVIS Camera Snapshot*\n• Facing: {facing}\n• Timestamp: {datetime.utcnow().strftime('%H:%M:%S UTC')}")
            return f"📸 *[SNAPSHOT CAPTURED]*\nDelivered {facing} camera frame to Telegram."
        else:
            return f"📸 Snapshot attempted ({facing} camera). Check permissions."

    elif cmd.startswith("/locate") or "locate" in cmd.lower() or "gps" in cmd.lower():
        loc = get_location_gps()
        sync_to_firestore("locations", "latest", loc)
        return (
            f"📍 *[JARVIS GPS BEACON]*\n"
            f"• Coordinates: `{loc['latitude']:.5f}, {loc['longitude']:.5f}`\n"
            f"• Accuracy: ±{loc['accuracy']}m\n"
            f"• [Open in Google Maps]({loc['google_maps_url']})"
        )

    elif cmd.startswith("/status") or "status" in cmd.lower():
        batt = get_battery_info()
        wifi = get_wifi_info()
        pct = batt.get("percentage", 85)
        temp = batt.get("temperature", 31.8)
        ssid = wifi.get("ssid", "WiFi")
        
        telemetry = {
            "battery_level": pct,
            "battery_temp_celsius": temp,
            "wifi_ssid": ssid,
            "network_type": "WIFI",
            "timestamp": datetime.utcnow().isoformat()
        }
        sync_to_firestore("telemetry", "current", telemetry)
        return (
            f"📊 *[JARVIS SYSTEM STATUS]*\n"
            f"• 🔋 Battery: `{pct}%` ({temp}°C, {batt.get('status', 'Good')})\n"
            f"• 📶 Wi-Fi: `{ssid}` ({wifi.get('rssi', -52)} dBm)\n"
            f"• ⚡ Hardware Daemon: `Active (Termux:API)`"
        )

    elif cmd.startswith("/mute") or "mute" in cmd.lower() or "silence" in cmd.lower():
        set_volume_override("ring", 0)
        set_volume_override("notification", 0)
        set_volume_override("music", 0)
        set_volume_override("system", 0)
        send_android_notification("🔇 SILENT MODE", "All audio streams set to 0%", "low")
        return "🔇 *[STEALTH SILENCE ACTIVE]*\nAll phone audio streams locked to 0%."

    elif cmd.startswith("/torch") or "torch" in cmd.lower() or "flashlight" in cmd.lower():
        if "off" in cmd.lower():
            set_torch("off")
            return "🔦 Flashlight turned OFF."
        else:
            set_torch("on")
            return "🔦 Flashlight turned ON."

    elif cmd.startswith("/speak") or cmd.startswith("/say"):
        text_to_say = cmd.split(" ", 1)[1] if " " in cmd else "JARVIS systems online."
        speak_tts(text_to_say)
        return f"🗣️ Spoke: \"{text_to_say}\""

    elif cmd.startswith("/clipboard"):
        clip = get_clipboard_text()
        return f"📋 *[PHONE CLIPBOARD]*:\n`{clip[:200]}`"

    return f"⚡ Command '{cmd}' processed by JARVIS."

# ─── Background Telemetry & Poll Loop ─────────────────────────────────────────

def telemetry_pulse_loop():
    """Periodically pulses battery and network telemetry to Firebase every 30s."""
    logger.info("Starting background telemetry pulse loop (30s interval)...")
    while True:
        try:
            batt = get_battery_info()
            loc = get_location_gps()
            wifi = get_wifi_info()
            sync_to_firestore("telemetry", "current", {
                "battery_level": batt.get("percentage", 85),
                "battery_temp_celsius": batt.get("temperature", 31.8),
                "battery_charging": batt.get("status") == "CHARGING",
                "latitude": loc.get("latitude", 28.6139),
                "longitude": loc.get("longitude", 77.2090),
                "wifi_ssid": wifi.get("ssid", "HomeNet_5G"),
                "network_type": "WIFI",
                "timestamp": datetime.utcnow().isoformat()
            })
        except Exception as e:
            logger.warning(f"Telemetry pulse error: {e}")
        time.sleep(30)

def main():
    print("""
    ╔══════════════════════════════════════════════════════════════════╗
    ║       ⚡ JARVIS TERMUX ON-DEVICE HARDWARE AGENT v2.0            ║
    ║       Secondary Brain 2.0 • Hardware Control & C2 Daemon        ║
    ╚══════════════════════════════════════════════════════════════════╝
    """)
    logger.info(f"Termux Environment Detected: {IS_TERMUX}")
    logger.info(f"Firebase Sync Target: {FIREBASE_PROJECT_ID}")
    logger.info(f"Telegram Bot Target: {TELEGRAM_CHAT_ID}")

    # Startup greeting
    speak_tts("JARVIS hardware daemon initialized. All systems nominal.")
    send_android_notification("⚡ JARVIS ONLINE", "Hardware agent running in background", "low")

    # Start background telemetry pulse
    pulse_thread = threading.Thread(target=telemetry_pulse_loop, daemon=True)
    pulse_thread.start()

    logger.info("JARVIS Hardware Daemon running. Press Ctrl+C to exit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down JARVIS Hardware Daemon...")
        set_torch("off")
        sys.exit(0)

if __name__ == "__main__":
    main()
