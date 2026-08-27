#!/usr/bin/env python3
"""
CareerOps — Dinamik Gemini Model Çözümleyici
=============================================
Model adını SABİTLEMEZ. Çalışma anında `client.models.list()` ile canlı
katalogdan öncelik listesindeki ilk uygun modeli seçer ve önbelleğe alır.

Neden: sabit model adı, sağlayıcı modeli kullanımdan kaldırdığında tüm
LLM katmanını 404 ile düşürüyor. Katalog sorgusu bu sınıf hatayı yapısal
olarak ortadan kaldırır.

Dayanıklılık katmanları:
  1. GEMINI_MODEL env değişkeni — açıkça verilmişse öncelikli, ancak
     yine de katalogda DOĞRULANIR (yanlış yazım sessizce 404 olmasın).
  2. Öncelik listesi — tam eşleşme, sonra önek eşleşmesi
     ("gemini-2.5-flash" → "gemini-2.5-flash-002" da kabul).
  3. Aile yedeği — listedeki hiçbiri yoksa katalogdaki herhangi bir
     "flash" modeli, o da yoksa generateContent destekleyen ilk model.
  4. Disk önbelleği (TTL) — CI'da her run katalog çekmesin.
  5. Çağrı anında 404/NOT_FOUND → önbellek geçersizleştirilir, yeniden
     çözümlenir ve istek BİR kez tekrarlanır.

AFC (Automatic Function Calling) KULLANILMAZ: tek atışlık yapılandırılmış
puanlama yapıyoruz. `response_mime_type="application/json"` ile standart
içerik üretimi hem gecikmeyi hem token maliyetini minimumda tutar.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    genai = None            # type: ignore[assignment]
    genai_types = None      # type: ignore[assignment]
    genai_errors = None     # type: ignore[assignment]

BASE_DIR = Path(__file__).parent
CACHE_PATH = BASE_DIR / "data" / "model-cache.json"
CACHE_TTL_SEC = int(os.environ.get("GEMINI_MODEL_CACHE_TTL", "86400"))  # 24s

# Öncelik listesi — soldan sağa ilk MEVCUT olan seçilir.
MODEL_PRIORITY: tuple[str, ...] = tuple(
    m.strip() for m in os.environ.get(
        "GEMINI_MODEL_PRIORITY",
        "gemini-3.6-flash,gemini-3.5-flash,gemini-2.5-flash,gemini-2.0-flash",
    ).split(",") if m.strip()
)

_GENERATE_ACTION = "generateContent"

_lock = threading.Lock()
_client = None
_resolved: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# İstemci
# ─────────────────────────────────────────────────────────────────────────────

def get_client():
    """Tek istemci örneği. API anahtarı ortamdan okunur (GEMINI_API_KEY)."""
    global _client
    if not SDK_AVAILABLE:
        return None
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            return None
        with _lock:
            if _client is None:
                _client = genai.Client(api_key=api_key)
    return _client


# ─────────────────────────────────────────────────────────────────────────────
# Önbellek
# ─────────────────────────────────────────────────────────────────────────────

def _read_cache() -> str | None:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if time.time() - float(data.get("at", 0)) > CACHE_TTL_SEC:
        return None
    # Öncelik listesi değiştiyse önbellek geçersizdir
    if data.get("priority") != list(MODEL_PRIORITY):
        return None
    model = data.get("model")
    return model if isinstance(model, str) and model else None


def _write_cache(model: str) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"model": model, "at": time.time(),
                       "priority": list(MODEL_PRIORITY)},
                      fh, separators=(",", ":"))
    except OSError:
        pass


def invalidate_cache() -> None:
    """Çözümlenmiş modeli unut — 404 sonrası yeniden keşif için."""
    global _resolved
    with _lock:
        _resolved = None
    try:
        CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Katalog
# ─────────────────────────────────────────────────────────────────────────────

def _short(name: str) -> str:
    """'models/gemini-2.5-flash' → 'gemini-2.5-flash'."""
    return (name or "").split("/")[-1]


def list_available(client=None) -> list[str]:
    """
    generateContent destekleyen model adlarını döner (kısa ad).
    Hata durumunda boş liste — çağıran taraf yedeğe düşer.
    """
    client = client or get_client()
    if client is None:
        return []
    out: list[str] = []
    try:
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            # Bazı sürümler supported_actions'ı boş bırakır; boşsa eleme.
            if actions and _GENERATE_ACTION not in actions:
                continue
            name = _short(getattr(m, "name", ""))
            if name:
                out.append(name)
    except Exception:
        return []
    return out


def _pick(available: list[str], priority: tuple[str, ...]) -> str | None:
    """
    Öncelik listesinden ilk uygun modeli seçer.

    Eşleşme sırası:
      1. Tam ad
      2. Önek ("gemini-2.5-flash" → "gemini-2.5-flash-002") — kararlılık için
         "preview"/"exp" içerenler en sona atılır
    """
    if not available:
        return None
    pool = set(available)

    for want in priority:
        if want in pool:
            return want
        prefixed = [a for a in available if a.startswith(want)]
        if prefixed:
            prefixed.sort(key=lambda a: (("preview" in a or "exp" in a), len(a)))
            return prefixed[0]
    return None


def _family_fallback(available: list[str]) -> str | None:
    """Öncelik listesinin tamamı yoksa: en ucuz aile ('flash') → herhangi biri."""
    if not available:
        return None
    flashes = [a for a in available
               if "flash" in a and "preview" not in a and "exp" not in a]
    if flashes:
        # Sürüm numarası büyük olan önce (gemini-3.x > gemini-2.x)
        flashes.sort(reverse=True)
        return flashes[0]
    return sorted(available)[0]


# ─────────────────────────────────────────────────────────────────────────────
# Çözümleme
# ─────────────────────────────────────────────────────────────────────────────

def resolve_model(force: bool = False, verbose: bool = False) -> str | None:
    """
    Kullanılacak model adını döner. Sıra:
        bellek → disk önbelleği → GEMINI_MODEL (doğrulanır) →
        öncelik listesi → aile yedeği
    Katalog çekilemezse öncelik listesinin ilk elemanına düşer (iyimser).
    """
    global _resolved

    if not force:
        if _resolved:
            return _resolved
        cached = _read_cache()
        if cached:
            with _lock:
                _resolved = cached
            if verbose:
                print(f"   model (önbellek): {cached}", flush=True)
            return cached

    client = get_client()
    if client is None:
        return None

    available = list_available(client)

    if not available:
        # Katalog erişilemedi (ağ/kota). Sabitlemek yerine ilk tercihi dene;
        # gerçekten 404 olursa çağrı katmanı invalidate edip tekrar dener.
        fallback = MODEL_PRIORITY[0] if MODEL_PRIORITY else None
        if verbose:
            print(f"   ⚠ model listesi alınamadı — {fallback} deneniyor",
                  flush=True)
        return fallback

    forced = os.environ.get("GEMINI_MODEL", "").strip()
    chosen = None
    if forced:
        chosen = _pick(available, (forced,))
        if chosen is None and verbose:
            print(f"   ⚠ GEMINI_MODEL='{forced}' katalogda yok — "
                  f"öncelik listesine düşülüyor", flush=True)

    chosen = chosen or _pick(available, MODEL_PRIORITY) or _family_fallback(available)

    if chosen:
        with _lock:
            _resolved = chosen
        _write_cache(chosen)
        if verbose:
            print(f"   model çözümlendi: {chosen} "
                  f"({len(available)} model katalogda)", flush=True)
    return chosen


def _is_not_found(exc: Exception) -> bool:
    """404 / NOT_FOUND / desteklenmeyen model hatası mı?"""
    if genai_errors is not None and isinstance(exc, genai_errors.ClientError):
        if getattr(exc, "code", None) == 404:
            return True
    text = str(exc).lower()
    return ("not found" in text or "404" in text
            or "is not supported" in text or "does not exist" in text)


# ─────────────────────────────────────────────────────────────────────────────
# Üretim: tek atışlık yapılandırılmış puanlama (AFC YOK)
# ─────────────────────────────────────────────────────────────────────────────

def generate_json(
    prompt: str,
    temperature: float = 0.2,
    max_output_tokens: int = 900,
    thinking_budget: int = 0,
    verbose: bool = False,
):
    """
    Tek atışlık JSON üretimi. (metin, kullanım, model) döner; hata → (None,...).

    AFC KULLANILMAZ: araç çağrısı gerekmiyor, çok turlu döngü gecikme ve
    token ekler. `response_mime_type="application/json"` markdown çiti ve
    açıklama metni üretimini engeller; `thinking_budget=0` reasoning
    token'ını sıfırlar.

    Model 404 verirse önbellek geçersizleştirilir, yeniden çözümlenir ve
    istek BİR kez tekrarlanır.
    """
    client = get_client()
    if client is None:
        return None, None, None

    config = genai_types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        thinking_config=genai_types.ThinkingConfig(thinking_budget=thinking_budget),
    )

    for attempt in (1, 2):
        model = resolve_model(force=(attempt == 2), verbose=verbose)
        if not model:
            return None, None, None
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt, config=config)
            return (resp.text or ""), getattr(resp, "usage_metadata", None), model
        except Exception as exc:
            if attempt == 1 and _is_not_found(exc):
                if verbose:
                    print(f"   ⚠ '{model}' bulunamadı — katalog yenileniyor",
                          flush=True)
                invalidate_cache()
                continue
            if verbose:
                print(f"   ✗ üretim hatası: {type(exc).__name__}: "
                      f"{str(exc)[:90]}", flush=True)
            return None, None, model
    return None, None, None


if __name__ == "__main__":
    import sys

    if not SDK_AVAILABLE:
        print("google-genai kurulu değil"); sys.exit(1)
    if get_client() is None:
        print("GEMINI_API_KEY tanımlı değil"); sys.exit(1)

    print(f"öncelik listesi : {list(MODEL_PRIORITY)}")
    models = list_available()
    print(f"katalogda       : {len(models)} model")
    for m in models[:20]:
        print(f"   {m}")
    chosen = resolve_model(force=True, verbose=True)
    print(f"SEÇİLEN         : {chosen}")

    if "--call" in sys.argv:
        text, usage, model = generate_json(
            'Return exactly {"ok":true} as JSON.', verbose=True)
        print(f"yanıt ({model}): {text}")
        if usage:
            print(f"token: in={getattr(usage,'prompt_token_count',0)} "
                  f"out={getattr(usage,'candidates_token_count',0)}")
