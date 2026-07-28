#!/usr/bin/env python3
"""
CareerOps — RLHF Feedback Listener
=====================================
Standalone long-running process that polls Telegram for inline-button
callback queries and logs "apply / pass" decisions to rlhf_feedback.json.

Run alongside telegram-daily.py (separate terminal / background process):
    python feedback-listener.py

Çalıştırma:
    python feedback-listener.py

Arka planda çalıştırma (Windows):
    Start-Process python -ArgumentList "feedback-listener.py" `
        -RedirectStandardOutput "data/listener.log" `
        -RedirectStandardError  "data/listener-err.log" -NoNewWindow

Veri akışı:
  telegram-daily.py  → data/telegram-pending.json   (job index by string id)
  feedback-listener  → data/rlhf_feedback.json       (RLHF training log)
  feedback-listener  → data/telegram-offset.txt      (Telegram update cursor)

Callback data format (set by send_job_message):
    "apply|0"  →  decision=apply,  job_id=0
    "pass|0"   →  decision=pass,   job_id=0
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ── Force UTF-8 on Windows ────────────────────────────────────────────────────
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Config (must match telegram-daily.py) ─────────────────────────────────────
BASE_DIR     = Path(__file__).parent
BOT_TOKEN    = "8766700487:AAHtlSJf_F1PIUuxQzh-uRdPsxLwm6--KOM"
API_BASE     = f"https://api.telegram.org/bot{BOT_TOKEN}"

PENDING_JOBS = BASE_DIR / "data" / "telegram-pending.json"
RLHF_LOG     = BASE_DIR / "data" / "rlhf_feedback.json"
OFFSET_FILE  = BASE_DIR / "data" / "telegram-offset.txt"

POLL_TIMEOUT  = 30   # long-poll seconds; Telegram holds the connection
RETRY_DELAY   = 5    # seconds to wait after a network error


# ── Telegram helpers ───────────────────────────────────────────────────────────

def get_updates(offset: int) -> list[dict]:
    """Long-poll for new updates. Returns empty list on error."""
    try:
        r = requests.get(
            f"{API_BASE}/getUpdates",
            params={
                "offset":          offset,
                "timeout":         POLL_TIMEOUT,
                "allowed_updates": ["callback_query"],
            },
            timeout=POLL_TIMEOUT + 10,
        )
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception as e:
        print(f"[POLL ERROR] {e}", flush=True)
        return []


def answer_callback(callback_id: str, text: str = "✓ Kaydedildi", alert: bool = False):
    """Dismisses the loading spinner on the pressed button."""
    try:
        requests.post(
            f"{API_BASE}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text, "show_alert": alert},
            timeout=8,
        )
    except Exception:
        pass


def replace_keyboard(chat_id: str | int, message_id: int, decision: str):
    """Replaces the 👍/👎 keyboard with a static confirmation label."""
    label = "✅ Başvuracaksın" if decision == "apply" else "⏭ Geçildi"
    try:
        requests.post(
            f"{API_BASE}/editMessageReplyMarkup",
            json={
                "chat_id":      chat_id,
                "message_id":   message_id,
                "reply_markup": {
                    "inline_keyboard": [[{"text": label, "callback_data": "done"}]]
                },
            },
            timeout=8,
        )
    except Exception:
        pass


# ── Persistence helpers ────────────────────────────────────────────────────────

def load_pending() -> dict:
    if not PENDING_JOBS.exists():
        return {}
    try:
        return json.loads(PENDING_JOBS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def append_feedback(entry: dict):
    """Thread-safe-enough single-process append to rlhf_feedback.json."""
    feedback: list = []
    if RLHF_LOG.exists():
        try:
            feedback = json.loads(RLHF_LOG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            feedback = []
    feedback.append(entry)
    RLHF_LOG.parent.mkdir(parents=True, exist_ok=True)
    RLHF_LOG.write_text(
        json.dumps(feedback, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip()) + 1
        except ValueError:
            pass
    return 0


def write_offset(update_id: int):
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(update_id))


# ── Callback processor ─────────────────────────────────────────────────────────

def process_update(update: dict):
    cb = update.get("callback_query")
    if not cb:
        return

    cb_id      = cb["id"]
    data       = cb.get("data", "")
    message    = cb.get("message", {})
    chat_id    = message.get("chat", {}).get("id", "")
    message_id = message.get("message_id")

    # Ignore "done" sentinel (already processed)
    if data == "done":
        answer_callback(cb_id, "")
        return

    # Parse "apply|0" / "pass|0"
    parts = data.split("|")
    if len(parts) != 2:
        answer_callback(cb_id, "⚠️ Bilinmeyen komut")
        return

    decision, job_id = parts[0].lower(), parts[1]

    pending = load_pending()
    job = pending.get(str(job_id))
    if not job:
        answer_callback(cb_id, "⚠️ İlan bulunamadı (eski oturum?)", alert=True)
        return

    # Build RLHF record
    entry = {
        "timestamp": datetime.now().isoformat(),
        "decision":  decision,          # "apply" | "pass"
        "job_id":    job_id,
        "job_url":   job.get("url", ""),
        "job_title": job.get("title", ""),
        "company":   job.get("company", ""),
        "score":     job.get("score", 0),
        "profile":   job.get("profile", ""),
    }
    append_feedback(entry)

    icon = "✅" if decision == "apply" else "⏭"
    answer_callback(cb_id, f"{icon} Kaydedildi!")
    if chat_id and message_id:
        replace_keyboard(chat_id, message_id, decision)

    verb = "APPLY" if decision == "apply" else "PASS "
    print(
        f"[RLHF] {verb} → {job.get('title','?'):40s} @ {job.get('company','?')}",
        flush=True,
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    print("🤖 CareerOps Feedback Listener başlatıldı.", flush=True)
    print(f"   Bekleyen ilanlar : {PENDING_JOBS}", flush=True)
    print(f"   RLHF log         : {RLHF_LOG}", flush=True)
    print("   Durdurmak için: Ctrl+C\n", flush=True)

    offset = read_offset()

    while True:
        updates = get_updates(offset)

        if not updates:
            # Long-poll returned empty → immediately retry (no sleep needed)
            time.sleep(0)
            continue

        for upd in updates:
            try:
                process_update(upd)
            except Exception as e:
                print(f"[ERROR] {e}", flush=True)
            finally:
                offset = upd["update_id"] + 1
                write_offset(upd["update_id"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Feedback Listener durduruldu.", flush=True)
