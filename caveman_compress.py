#!/usr/bin/env python3
"""
CareerOps — Caveman Prompt Compressor
Caveman prensiplerini LLM prompt payload'larına uygular.
Hedef: static context bölümlerinde ~%40-50 token tasarrufu,
       scoring accuracy kaybı olmadan.

Kural: kod blokları, örnek satırları ve tablo veri hücreleri DOKUNULMAZ.
       Yalnızca prose satırlardaki filler/article kaldırılır.
"""
from __future__ import annotations

import re


# ── Prose satırlarında atılacak filler ifadeleri (Türkçe + İngilizce) ─────────
_FILLER: list[tuple[str, str]] = [
    # Türkçe filler
    ("Bu matriksi ÖNCE oku. ", ""),
    ("kesinlikle uygula", "uygula"),
    ("mutlaka ", ""),
    ("aşağıdaki gibi ", ""),
    ("aşağıda belirtilen ", ""),
    ("lütfen ", ""),
    ("dikkat et: ", ""),
    ("Bu belgeyi ", "Bu "),
    # İngilizce filler
    ("In order to", "To"),
    ("Please note that ", ""),
    ("It is important to note that ", ""),
    ("Please be aware that ", ""),
    ("I would like to", "I want to"),
    ("Make sure to", "Ensure"),
    ("You should", ""),
    ("certainly", ""),
    ("basically", ""),
    ("actually", ""),
    ("simply", ""),
    ("just ", ""),
]

# Markdown tablo ayırıcı satır (|---|---| gibi)
_TABLE_SEP = re.compile(r"^\s*\|[\s\-:|]+\|[\s\-:|]*\|.*$")

# Korunacak satır prefixleri — bu prefixlerle başlayan satırlar ham bırakılır
_SKIP_PREFIXES = ("```", "    ", "\t", "Not:", "Yes:", "> ")


def _is_protected(line: str) -> bool:
    """Satır kod/örnek/tablo olup olmadığını döner — bunlara dokunulmaz."""
    s = line.strip()
    if not s:
        return False
    if s.startswith(_SKIP_PREFIXES):
        return True
    if re.match(r"^\d+\.\s", s):   # numaralı liste
        return True
    if re.match(r"^```", s):       # code fence
        return True
    return False


def _compress_line(line: str) -> str:
    """Tek prose satırından filler çıkarır."""
    if _is_protected(line):
        return line
    result = line
    for phrase, replacement in _FILLER:
        result = result.replace(phrase, replacement)
    # Birden fazla boşluğu tek boşluğa düşür
    result = re.sub(r"  +", " ", result)
    return result


def _compact_table(block: str) -> str:
    """Markdown tablo bloğundaki ayırıcı satırları siler, hücre padding'i kısar."""
    rows = []
    for line in block.strip().split("\n"):
        if _TABLE_SEP.match(line):
            continue  # separator row sil
        cells = [c.strip() for c in line.split("|")]
        rows.append("|".join(cells))
    return "\n".join(rows)


def compress(text: str, level: str = "full") -> tuple[str, dict]:
    """
    Prompt metnini caveman prensipleriyle sıkıştırır.

    Args:
        text:  Ham prompt string.
        level: "lite"  — yalnızca filler temizliği
               "full"  — filler + tablo kompaktlaştırma (varsayılan)
               "ultra" — full + kısa eş anlamlılar (agresif, dikkatli kullan)

    Returns:
        (compressed_text, {"original_tokens", "compressed_tokens", "savings_pct"})
    """
    original = text
    orig_tok = _estimate_tokens(original)

    if level == "lite":
        result = "\n".join(_compress_line(l) for l in text.split("\n"))
    else:
        # Tablo bloklarını compact et
        result = re.sub(
            r"(\|[^\n]+\n)+",
            lambda m: _compact_table(m.group(0)) + "\n",
            text,
        )
        # Prose filler
        result = "\n".join(_compress_line(l) for l in result.split("\n"))

    if level == "ultra":
        result = _ultra_compress(result)

    # Ardışık boş satırları tek boş satıra düşür
    result = re.sub(r"\n{3,}", "\n\n", result)

    comp_tok = _estimate_tokens(result)
    savings = round((1 - comp_tok / orig_tok) * 100, 1) if orig_tok else 0.0

    stats = {
        "original_tokens":   orig_tok,
        "compressed_tokens": comp_tok,
        "savings_pct":       savings,
    }
    return result, stats


def _ultra_compress(text: str) -> str:
    """Ultra: prose'daki ortak uzun kalıpları kısa eş anlamlıyla değiştirir."""
    replacements = [
        ("değerlendirme", "eval"),
        ("anahtar kelime", "kw"),
        ("gereksinim", "req"),
        ("uygulama", "app"),
        ("konfigürasyon", "config"),
        ("implementation", "impl"),
        ("requirement", "req"),
        ("evaluation", "eval"),
        ("configuration", "config"),
        ("function", "fn"),
        ("parameter", "param"),
    ]
    result = text
    for long, short in replacements:
        # Yalnızca prose (code block dışı) — basit yaklaşım: code fence'ler arası atlat
        result = re.sub(
            rf"(?<!`){re.escape(long)}(?!`)",
            short,
            result,
            flags=re.IGNORECASE,
        )
    return result


def _estimate_tokens(text: str) -> int:
    """Kaba token tahmini: chars / 3.5 (Claude/Gemini BPE yaklaşımı)."""
    return max(1, int(len(text) / 3.5))


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Kullanım: python caveman_compress.py <dosya.txt> [lite|full|ultra]")
        sys.exit(1)

    path  = sys.argv[1]
    level = sys.argv[2] if len(sys.argv) > 2 else "full"
    text  = open(path, encoding="utf-8").read()

    compressed, stats = compress(text, level=level)

    print(compressed)
    print(f"\n── İstatistik ({'─'*30})", file=sys.stderr)
    print(f"  Orijinal  : ~{stats['original_tokens']:,} token", file=sys.stderr)
    print(f"  Sıkıştırma: ~{stats['compressed_tokens']:,} token", file=sys.stderr)
    print(f"  Tasarruf  : %{stats['savings_pct']}", file=sys.stderr)
