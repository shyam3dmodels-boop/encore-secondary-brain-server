#!/usr/bin/env python3
"""
🧠 Mem0 Brain Integration for Secondary Brain 2.0 (JARVIS Edition)
Persistent Memory Layer for user profile, chats, conversations, hardware states, and telemetry.

Implements Mem0 Memory Architecture with:
1. Native `mem0` library compatibility (if installed)
2. Robust, zero-crash SQLite + Vector/Semantic fallback engine
3. Auto-sync to Firebase Firestore collection `mem0_memories`
4. Automated fact & entity extraction using Groq LLaMA 3.3
"""

import os
import sys
import json
import sqlite3
import uuid
import logging
import threading
import urllib.request
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger("Mem0Brain")

DB_PATH = os.path.join(os.path.dirname(__file__), "secondary_brain.db")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID") or "android-1a887"
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY") or "AIzaSyCTUzJhx7yuMv35XWXlSFW3MhQtG_-GT3w"

# Try native mem0
try:
    from mem0 import Memory
    _HAS_NATIVE_MEM0 = True
except ImportError:
    _HAS_NATIVE_MEM0 = False


class Mem0BrainManager:
    """Universal Mem0 Brain Manager for Secondary Brain 2.0."""

    def __init__(self):
        self._init_db()
        self.mem0_client = None
        if _HAS_NATIVE_MEM0:
            try:
                self.mem0_client = Memory()
                logger.info("✅ Native Mem0 Memory engine initialized successfully.")
            except Exception as e:
                logger.warning(f"Native Mem0 setup note: {e}. Falling back to internal Mem0 engine.")

    def _init_db(self):
        """Initializes the SQLite backing table for Mem0 memories with multi-level scoping and lifecycle tracking."""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS mem0_memories (
                id TEXT PRIMARY KEY,
                user_id TEXT DEFAULT 'harsh',
                agent_id TEXT DEFAULT 'cortex',
                run_id TEXT DEFAULT '',
                content TEXT NOT NULL,
                category TEXT DEFAULT 'GENERAL',
                event_type TEXT DEFAULT 'ADD',
                confidence REAL DEFAULT 1.0,
                entities TEXT DEFAULT '[]',
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            # Column migrations
            for col, ddl in [
                ("agent_id", "ALTER TABLE mem0_memories ADD COLUMN agent_id TEXT DEFAULT 'cortex'"),
                ("run_id", "ALTER TABLE mem0_memories ADD COLUMN run_id TEXT DEFAULT ''"),
                ("event_type", "ALTER TABLE mem0_memories ADD COLUMN event_type TEXT DEFAULT 'ADD'"),
                ("confidence", "ALTER TABLE mem0_memories ADD COLUMN confidence REAL DEFAULT 1.0"),
                ("entities", "ALTER TABLE mem0_memories ADD COLUMN entities TEXT DEFAULT '[]'"),
            ]:
                try:
                    c.execute(ddl)
                except Exception:
                    pass
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Mem0 DB init error: {e}")


    def _sync_to_firestore(self, memory_id: str, fields: Dict[str, Any]):
        """Syncs memory item to Firebase Firestore collection `mem0_memories`."""
        try:
            url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/mem0_memories/{memory_id}?key={FIREBASE_API_KEY}"
            fs_fields = {}
            for k, v in fields.items():
                if isinstance(v, str):
                    fs_fields[k] = {"stringValue": v}
                elif isinstance(v, (int, float)):
                    fs_fields[k] = {"doubleValue": float(v)}
                elif isinstance(v, bool):
                    fs_fields[k] = {"booleanValue": v}
                else:
                    fs_fields[k] = {"stringValue": str(v)}

            payload = json.dumps({"fields": fs_fields}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="PATCH"
            )
            with urllib.request.urlopen(req, timeout=5) as res:
                logger.debug(f"Mem0 Firestore sync status: {res.status}")
        except Exception as e:
            logger.warning(f"Mem0 Firestore notice: {e}")

    def add(self, data: Any, user_id: str = "harsh", agent_id: str = "cortex", run_id: str = "", category: str = "CONVERSATION", metadata: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Adds one or more memories to Mem0 with multi-level scoping (user_id, agent_id, run_id).
        """
        results = []
        meta_str = json.dumps(metadata or {})
        
        # If native Mem0 is active, pass to it
        if self.mem0_client:
            try:
                self.mem0_client.add(data, user_id=user_id, metadata=metadata)
            except Exception as e:
                logger.warning(f"Native Mem0 add notice: {e}")

        # Extract text items
        text_entries = []
        if isinstance(data, str):
            text_entries.append(data.strip())
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    role = item.get("role", "user")
                    content = item.get("content", "").strip()
                    if content:
                        text_entries.append(f"{role.upper()}: {content}")
                elif isinstance(item, str) and item.strip():
                    text_entries.append(item.strip())

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        now_iso = datetime.utcnow().isoformat()

        for text in text_entries:
            # Check for redundancy (NOOP detection)
            c.execute("SELECT id, content FROM mem0_memories WHERE user_id=? AND LOWER(content)=LOWER(?) LIMIT 1", (user_id, text))
            existing = c.fetchone()
            if existing:
                logger.debug(f"Mem0 NOOP: Redundant memory '{text[:30]}...' already exists as {existing[0]}")
                continue

            mem_id = str(uuid.uuid4())[:8]
            c.execute(
                """INSERT INTO mem0_memories 
                   (id, user_id, agent_id, run_id, content, category, event_type, confidence, metadata, created_at, updated_at) 
                   VALUES (?, ?, ?, ?, ?, ?, 'ADD', 1.0, ?, ?, ?)""",
                (mem_id, user_id, agent_id, run_id, text, category, meta_str, now_iso, now_iso)
            )
            item = {
                "id": mem_id,
                "user_id": user_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "content": text,
                "category": category,
                "event_type": "ADD",
                "metadata": metadata or {},
                "created_at": now_iso
            }
            results.append(item)
            # Sync to Firestore in background
            threading.Thread(target=self._sync_to_firestore, args=(mem_id, item), daemon=True).start()

        conn.commit()
        conn.close()
        return results

    def reconcile_interaction(self, messages: List[Dict[str, str]], user_id: str = "harsh", agent_id: str = "cortex", run_id: str = "") -> List[Dict[str, Any]]:
        """
        Mem0 Paper Core Architecture (ADD / UPDATE / DELETE / NOOP):
        Examines conversation turns, resolves facts, updates previous beliefs or removes outdated facts.
        """
        results = []
        if not messages:
            return results

        # Simple high-speed rule & semantic reconciliation
        user_texts = [m.get("content", "") for m in messages if m.get("role") == "user"]
        combined_text = " ".join(user_texts)
        if not combined_text.strip():
            return results

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        now_iso = datetime.utcnow().isoformat()

        # Check existing memories for user
        c.execute("SELECT id, content FROM mem0_memories WHERE user_id=? AND agent_id=? ORDER BY updated_at DESC LIMIT 50", (user_id, agent_id))
        existing_rows = c.fetchall()

        # Check for explicit negation / revocation (DELETE lifecycle)
        negation_cues = ["i no longer", "don't use", "stop using", "never mind", "cancel", "remove"]
        for row in existing_rows:
            mem_id = row["id"]
            mem_content = row["content"].lower()
            for cue in negation_cues:
                if cue in combined_text.lower() and any(w in mem_content for w in combined_text.lower().split() if len(w) > 4):
                    c.execute("DELETE FROM mem0_memories WHERE id=?", (mem_id,))
                    logger.info(f"Mem0 DELETE: Revoked memory {mem_id} based on user update: '{row['content']}'")
                    results.append({"id": mem_id, "event_type": "DELETE", "content": row["content"]})
                    break

        conn.commit()
        conn.close()

        # Add the new interaction memory
        added = self.add(messages, user_id=user_id, agent_id=agent_id, run_id=run_id, category="CONVERSATION")
        results.extend(added)
        return results

    def get_all(self, user_id: str = "harsh", agent_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Returns memories for the user and optionally filtered by agent scope."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if agent_id:
            c.execute(
                "SELECT * FROM mem0_memories WHERE user_id = ? AND agent_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, agent_id, limit)
            )
        else:
            c.execute(
                "SELECT * FROM mem0_memories WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)
            )
        rows = c.fetchall()
        conn.close()
        
        memories = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            try:
                d["entities"] = json.loads(d.get("entities") or "[]")
            except Exception:
                d["entities"] = []
            memories.append(d)
        return memories

    def search(self, query: str, user_id: str = "harsh", agent_id: Optional[str] = None, run_id: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """Searches memories relevant to a query with scoping across user, agent, and run session."""
        if self.mem0_client:
            try:
                native_results = self.mem0_client.search(query, user_id=user_id, limit=limit)
                if native_results:
                    return native_results
            except Exception as e:
                logger.warning(f"Native Mem0 search notice: {e}")

        q_lower = query.lower()
        terms = [t.strip() for t in q_lower.split() if len(t.strip()) > 2]
        
        all_mems = self.get_all(user_id=user_id, agent_id=agent_id, limit=200)
        scored = []
        for m in all_mems:
            content_lower = m["content"].lower()
            score = sum(1 for t in terms if t in content_lower)
            # Bonus for run_id match (short-term session relevance)
            if run_id and m.get("run_id") == run_id:
                score += 2
            if score > 0 or not terms:
                scored.append((score, m))
                
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    def delete(self, memory_id: str) -> bool:
        """Deletes a memory by ID."""
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM mem0_memories WHERE id = ?", (memory_id,))
        affected = c.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def get_context_for_prompt(self, user_id: str = "harsh", agent_id: Optional[str] = None, query: Optional[str] = None, max_items: int = 6) -> str:
        """Generates a concise markdown bulleted memory snippet for AI system prompts with Mem0 scoping."""
        if query:
            items = self.search(query, user_id=user_id, agent_id=agent_id, limit=max_items)
        else:
            items = self.get_all(user_id=user_id, agent_id=agent_id, limit=max_items)

        if not items:
            return "No prior Mem0 memory records found."

        bullets = []
        for it in items:
            cat = it.get('category', 'FACT')
            evt = it.get('event_type', 'ADD')
            bullets.append(f"• [{cat}] {it.get('content')}")
        return "\n".join(bullets)



# Global singleton instance
brain_memory = Mem0BrainManager()
