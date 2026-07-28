#!/usr/bin/env python3
"""
CareerOps — JD Metin Temizleyici (token verimliliği)
=====================================================
Ham ilan HTML'ini/metnini LLM'e göndermeden önce arındırır:

  1. HTML → düz metin (BeautifulSoup + lxml), script/style/nav/footer/aside atılır
  2. Boilerplate satırlar (cookie, EEO, "Apply now", sosyal medya, telif) silinir
  3. Tekrarlanan satırlar ve boşluk gürültüsü sıkıştırılır
  4. Karakter bütçesine göre akıllı kısaltma (baş + kuyruk korunur)

Tipik kazanç: 40-60 KB'lık bir LinkedIn/Greenhouse sayfası → ~3.5 KB metin,
yani ilan başına ~12-15k input token yerine ~900 token.

Kullanım:
    from text_clean import clean_jd
    desc = clean_jd(raw_html, max_chars=3500)
"""

from __future__ import annotations

import re

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

# Varsayılan karakter bütçesi — ~1 token ≈ 4 karakter, yani ~875 token
DEFAULT_MAX_CHARS = 3500

# HTML olduğunu ele veren işaretler
_HTML_HINT = re.compile(r"<(html|body|div|p|br|li|span|script)\b", re.I)

# Tamamen atılacak etiketler (görsel/navigasyon/script gürültüsü)
_DROP_TAGS = (
    "script", "style", "noscript", "svg", "iframe", "form", "button",
    "nav", "header", "footer", "aside", "picture", "video", "audio",
)

# class/id'sinde bu geçen bloklar sidebar/footer/öneri panelidir
_DROP_ATTR = re.compile(
    r"cookie|consent|banner|sidebar|side-bar|footer|header|nav|menu|breadcrumb|"
    r"related|similar|recommend|suggest|share|social|newsletter|subscribe|"
    r"advert|promo|popup|modal|chat|feedback|skip-link|screen-?reader",
    re.I,
)

# Satır bazlı boilerplate — eşleşen satır tamamen düşer
_BOILERPLATE_LINE = re.compile(
    r"^\s*("
    r"apply\s*(now|for this job|with)?|başvur|hemen başvur|"
    r"save (this )?job|kaydet|share (this )?(job|post)|paylaş|"
    r"(sign|log)\s*(in|up)|giriş yap|üye ol|"
    r"cookie|çerez|privacy|gizlilik|terms|kullanım koşulları|"
    r"©|copyright|all rights reserved|tüm hakları saklıdır|"
    r"follow us|bizi takip|linkedin|twitter|facebook|instagram|youtube|"
    r"show more|show less|daha fazla göster|read more|"
    r"back to (jobs|search)|see all jobs|tüm ilanlar|"
    r"posted \d+|\d+ (days?|hours?|weeks?) ago|\d+ (gün|saat|hafta) önce"
    r")\s*[:.!]?\s*$",
    re.I,
)

# Uzun EEO/legal paragrafları — LLM için sıfır sinyal, yüksek token
_BOILERPLATE_BLOCK = re.compile(
    r"(equal\s+(employment\s+)?opportunity|"
    r"we are an equal opportunity employer|"
    r"regardless of race|without regard to race|"
    r"reasonable accommodation|"
    r"e-verify|"
    r"applicants? with disabilities|"
    r"fırsat eşitliği|ayrım gözetmeksizin|"
    r"kişisel verilerin korunması|kvkk)",
    re.I,
)

_WS_RUNS   = re.compile(r"[ \t ]{2,}")
_NL_RUNS   = re.compile(r"\n{3,}")
_DECOR     = re.compile(r"^[\s\-–—=*_·•|]{3,}$")


def _strip_html(raw: str) -> str:
    """HTML'i düz metne çevirir; gürültü bloklarını DOM seviyesinde atar."""
    if not _BS4_AVAILABLE:
        # bs4 yoksa kaba etiket temizliği (yine de script/style içeriğini at)
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        return re.sub(r"(?s)<[^>]+>", " ", raw)

    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:
        soup = BeautifulSoup(raw, "html.parser")

    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()

    # class/id ile işaretlenmiş sidebar/footer/promo blokları
    for el in soup.find_all(attrs={"class": _DROP_ATTR}):
        el.decompose()
    for el in soup.find_all(attrs={"id": _DROP_ATTR}):
        el.decompose()

    return soup.get_text("\n")


def _drop_noise_lines(text: str) -> str:
    """Boilerplate satırları, dekoratif ayraçları ve tekrarları siler."""
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = _WS_RUNS.sub(" ", line).strip()
        if not line or _DECOR.match(line):
            if out and out[-1] != "":
                out.append("")
            continue
        if _BOILERPLATE_LINE.match(line) or _BOILERPLATE_BLOCK.search(line):
            continue
        # Tek kelimelik navigasyon artıkları
        if len(line) < 3:
            continue
        # Aynı satır tekrarı (menü/footer kalıntısı) — uzun satırlarda koru
        key = line.lower()
        if len(line) < 120:
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return _NL_RUNS.sub("\n\n", "\n".join(out)).strip()


def truncate_smart(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """
    Bütçeyi aşan metni baş (%65) + kuyruk (%35) olarak kırpar.

    Sebep: ilanın başında rol/şirket bağlamı, sonunda "requirements" ve
    "qualifications" bölümleri bulunur; ortadaki benefits/kültür anlatısı
    puanlama için en düşük sinyalli kısımdır.
    """
    if len(text) <= max_chars:
        return text

    marker    = "\n\n[...orta bölüm kısaltıldı...]\n\n"
    budget    = max_chars - len(marker)
    head_len  = int(budget * 0.65)
    tail_len  = budget - head_len

    head = text[:head_len]
    head = head[:head.rfind("\n")] if "\n" in head[head_len // 2:] else head

    tail = text[-tail_len:]
    nl   = tail.find("\n")
    tail = tail[nl + 1:] if 0 <= nl < tail_len // 2 else tail

    return f"{head.rstrip()}{marker}{tail.lstrip()}"


def clean_jd(raw: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """
    Ham JD (HTML veya metin) → LLM'e hazır, kısaltılmış düz metin.

    Boş/None girdide "" döner — çağıran taraf JD'siz akışa düşebilir.
    """
    if not raw:
        return ""
    text = str(raw)
    if _HTML_HINT.search(text) or "</" in text:
        text = _strip_html(text)
    text = _drop_noise_lines(text)
    return truncate_smart(text, max_chars)


def clean_stats(raw: str, cleaned: str) -> dict:
    """Tasarruf ölçümü — log/telemetri için (yaklaşık token = kar/4)."""
    raw_len, new_len = len(raw or ""), len(cleaned or "")
    saved = raw_len - new_len
    return {
        "raw_chars":     raw_len,
        "clean_chars":   new_len,
        "saved_chars":   saved,
        "saved_pct":     round(100 * saved / raw_len, 1) if raw_len else 0.0,
        "approx_tokens": new_len // 4,
    }


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    data = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    out = clean_jd(data)
    print(out)
    print(f"\n--- {clean_stats(data, out)} ---", file=sys.stderr)
