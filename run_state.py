#!/usr/bin/env python3
"""
CareerOps — Kalıcı Çalışma Durumu
==================================
GitHub Actions runner'ı geçicidir: her tetiklemede yeni bir makine gelir.
Bu modül `data/scraper-state.json` üzerinden iki soruyu cevaplar:

  1. "En son ne zaman başarıyla tarama yaptık?"
     → tazelik penceresi buradan hesaplanır. Cron 20 dk gecikirse veya bir
       run atlanırsa pencere otomatik genişler; aradaki ilanlar kaybolmaz.
  2. "Kaç ilan gönderdik, son çalıştırmada ne oldu?"
     → telemetri + hata ayıklama.

Dosya workflow tarafından repoya geri commit edilir (telegram-sent.tsv ile
aynı adımda). Bozuk/eksik dosya durumunda güvenli varsayılana düşer.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(__file__).parent / "data" / "scraper-state.json"

# Pencere sınırları (dakika)
MIN_WINDOW     = 60      # saatlik cron'un nominal aralığı
MAX_WINDOW     = 2880    # 48 saat — uzun kesintiden sonra arşivi boşaltmamak için
WINDOW_BUFFER  = 15      # cron gecikmesi + kaynak indeksleme payı

_DEFAULT: dict = {
    "last_run_at":     None,
    "last_success_at": None,
    "total_runs":      0,
    "total_sent":      0,
    "last_result":     {},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_state(path: Path | None = None) -> dict:
    """Durumu okur; dosya yok/bozuksa varsayılanı döner (asla exception atmaz)."""
    p = path or STATE_PATH
    if not p.exists():
        return dict(_DEFAULT)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(_DEFAULT)
        return {**_DEFAULT, **data}
    except (json.JSONDecodeError, OSError, ValueError):
        return dict(_DEFAULT)


def save_state(state: dict, path: Path | None = None) -> None:
    """Atomik yazar — runner yarıda kesilirse dosya bozulmaz."""
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def freshness_window(state: dict | None = None) -> tuple[int, str]:
    """
    Bu çalıştırmada kabul edilecek maksimum ilan yaşını (dakika) döner.

    İlk çalıştırma          → MIN_WINDOW
    Normal saatlik tetikleme → ~60-75 dk
    2 saat gecikme/atlama    → ~135 dk (aradaki ilanlar kaçmaz)
    3 gün kesinti            → MAX_WINDOW'da sınırlanır (arşiv boşaltmaz)

    Dönüş: (dakika, açıklama)
    """
    st = state if state is not None else load_state()
    last = st.get("last_success_at")
    if not last:
        return MIN_WINDOW, "ilk çalıştırma"
    try:
        prev = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
    except ValueError:
        return MIN_WINDOW, "durum okunamadı"

    elapsed = int((_now() - prev).total_seconds() / 60)
    window  = max(MIN_WINDOW, elapsed + WINDOW_BUFFER)
    if window > MAX_WINDOW:
        return MAX_WINDOW, f"son başarı {elapsed // 60}s önce — {MAX_WINDOW}dk'da sınırlandı"
    return window, f"son başarı {elapsed}dk önce (+{WINDOW_BUFFER}dk tampon)"


def record_run(
    sent: int,
    scanned: int = 0,
    stale: int = 0,
    blocked: int = 0,
    success: bool = True,
    path: Path | None = None,
) -> dict:
    """Çalıştırma sonucunu işler ve diske yazar."""
    st  = load_state(path)
    now = _now().isoformat()
    st["last_run_at"] = now
    st["total_runs"]  = int(st.get("total_runs", 0)) + 1
    st["total_sent"]  = int(st.get("total_sent", 0)) + max(0, sent)
    st["last_result"] = {
        "at": now, "sent": sent, "scanned": scanned,
        "stale": stale, "blocked": blocked, "success": success,
    }
    # Yalnızca başarılı taramada pencere ilerler: başarısız run'dan sonra
    # pencere geniş kalır ve kaçan ilanlar bir sonraki turda yakalanır
    if success:
        st["last_success_at"] = now
    save_state(st, path)
    return st


if __name__ == "__main__":
    s = load_state()
    w, why = freshness_window(s)
    print(json.dumps({"state": s, "window_minutes": w, "reason": why},
                     ensure_ascii=False, indent=2))
