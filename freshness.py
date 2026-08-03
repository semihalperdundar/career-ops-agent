#!/usr/bin/env python3
"""
CareerOps — Yayın Zamanı Ayrıştırıcı
=====================================
İlanın yayın zamanını "kaç dakika önce" değerine çevirir. Kaynaklar üç
farklı formatta zaman verir:

  • ISO-8601      → Greenhouse (first_published), Ashby (publishedAt),
                     Remotive (publication_date), LinkedIn (<time datetime>)
  • Epoch         → Lever (createdAt, milisaniye)
  • Göreli metin  → Kariyer.net / Indeed / Glassdoor ("3 saat önce", "2 days ago")

`minutes_since()` hepsini tek arayüzde toplar; ayrıştıramazsa None döner
(çağıran taraf "bilinmiyor" politikasını kendi uygular).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# ── Göreli zaman kalıpları (TR / EN / NL) ────────────────────────────────────
# Kelime sınırıyla eşleşir — "minutes" içindeki "nu" yanlış pozitifini önler
_NOW_RE = re.compile(
    r"\b(just now|moments? ago|seconds? ago|today|new|"
    r"şimdi|az önce|biraz önce|bugün|yeni|"
    r"zojuist|vandaag|nu)\b",
    re.I,
)

# Uzun ekler önce gelmeli: "dagen" | "dag" sırası önemli
_UNITS: tuple[tuple[str, int], ...] = (
    (r"minutes?|minuten|minuut|dakika|dk|min", 1),
    (r"hours?|saat|uren|uur|hr", 60),
    (r"days?|günü?|gunu?|dagen|dag", 1440),
    (r"weeks?|hafta|weken|wk", 10080),
    (r"months?|maanden|maand|ay|mo", 43200),
)

_ISO_CLEAN  = re.compile(r"\.\d+")
_DATE_ONLY  = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_relative(text: str) -> int | None:
    """'3 saat önce' → 180 | 'just now' → 0 | ayrıştırılamazsa None."""
    if not text:
        return None
    t = str(text).lower().strip()
    # Sayılı kalıp önce denenir: "1 day ago" içinde "today" yoktur ama
    # "bugün 3 saat önce" gibi karma metinlerde sayı daha bilgilendiricidir
    for pattern, mult in _UNITS:
        m = re.search(rf"(\d+)\s*({pattern})\b", t)
        if m:
            return int(m.group(1)) * mult
    if _NOW_RE.search(t):
        # "today"/"bugün" gün granüler — iyimser sınır (0) alınır; tekrar
        # gönderimi zaten arşiv dedup'ı engelliyor
        return 0
    return None


def parse_absolute(value) -> datetime | None:
    """ISO-8601 string veya epoch (s/ms) → timezone-aware datetime."""
    if value is None or value == "":
        return None

    # Epoch (Lever createdAt = ms, bazı API'ler saniye)
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.isdigit() and len(value) >= 10
    ):
        num = float(value)
        if num > 1e11:          # milisaniye
            num /= 1000.0
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    s = str(value).strip()
    if not s:
        return None
    # "2026-07-14T18:35:00-04:00" / "...Z" / "2026-07-14 18:35" / "2026-07-14"
    s = s.replace("Z", "+00:00")
    s = _ISO_CLEAN.sub("", s)
    for candidate in (s, s.replace(" ", "T"), s[:19], s[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        # Naive timestamp'i UTC varsay — kaynaklar UTC döner
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def minutes_since(value) -> int | None:
    """
    Yayın zamanını dakikaya çevirir (mutlak veya göreli fark etmez).
    Gelecek tarihli damgalar 0 kabul edilir (saat farkı toleransı).
    """
    if value is None or value == "":
        return None

    # Yalnızca tarih (saat yok) → LinkedIn/Indeed kartları böyle. Gerçek yaş
    # 0 ile "gece yarısından beri geçen süre" arasında; iyimser sınır alınır:
    # bugünse 0, dünse 1 gün. Tekrar gönderimi arşiv dedup'ı engeller.
    s = str(value).strip()
    if _DATE_ONLY.match(s):
        dt = parse_absolute(s)
        if dt is None:
            return None
        days = (_now_utc().date() - dt.date()).days
        return max(0, days) * 1440

    dt = parse_absolute(value)
    if dt is not None:
        delta = (_now_utc() - dt).total_seconds() / 60.0
        return max(0, int(delta))
    return parse_relative(s)


def job_age_minutes(job: dict) -> int | None:
    """İş sözlüğündeki bilinen zaman alanlarını sırayla dener."""
    for field in ("posted_at", "published_at", "created_at", "timestamp", "age_text"):
        val = job.get(field)
        if val not in (None, ""):
            age = minutes_since(val)
            if age is not None:
                return age
    return None


def within_window(job: dict, window_minutes: int, unknown_ok: bool = False) -> bool:
    """
    İlan verilen pencerede mi?

    unknown_ok=False → zaman damgası yoksa ilan ELENİR (katı mod).
    Kaynak yapısal olarak zaman vermiyorsa çağıran taraf muafiyet uygular.
    """
    age = job_age_minutes(job)
    if age is None:
        return unknown_ok
    return age <= window_minutes


def iso(dt: datetime | None = None) -> str:
    return (dt or _now_utc()).isoformat()


def human_age(minutes: int | None) -> str:
    """Log/Telegram için okunabilir yaş."""
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes}dk"
    if minutes < 1440:
        return f"{minutes // 60}s"
    return f"{minutes // 1440}g"


if __name__ == "__main__":
    samples = [
        "2026-07-14T18:35:00-04:00", 1784569799619, "3 saat önce", "2 days ago",
        "just now", "vandaag", "45 minutes ago", str(_now_utc().isoformat()), "",
    ]
    for s in samples:
        print(f"{str(s)[:40]:42s} → {human_age(minutes_since(s))}")
