#!/usr/bin/env python3
"""
CareerOps — Ağ Yönlendirme Politikası
======================================
Premium (Bright Data ISP) proxy GB başına ücretlendirilir. Greenhouse/Lever/
Ashby/Remotive gibi açık JSON API'ler doğrudan bağlantıyla sorunsuz çalışır —
onları proxy'den geçirmek saf bant genişliği israfıdır.

Politika üç kademelidir:

  1. STRICT host (kariyer.net, indeed.*, glassdoor)  → ilk istekten premium
  2. Diğer tüm host'lar                              → doğrudan bağlantı
  3. Doğrudan denemede blok yenirse (403/429/503)    → o host için premium'a
     yükselt ve kalan istekleri oradan geçir (escalation, süreç ömrü boyunca)

Böylece proxy trafiği yalnızca gerçekten gereken yerde harcanır.
"""

from __future__ import annotations

import os
import threading
from urllib.parse import urlparse, urlunparse

# ── Baştan premium gerektiren host'lar ───────────────────────────────────────
# Kariyer.net: PerimeterX + TR IP zorunlu
# Indeed:      datacenter IP'lerine 403, geo-gate
# Glassdoor:   Cloudflare
STRICT_HOSTS: tuple[str, ...] = (
    "kariyer.net",
    "indeed.com",
    "glassdoor.com",
    "glassdoor.nl",
    "yenibiris.com",
    "secretcv.com",
)

# Host → gereken exit ülkesi (WAF fingerprint'i IP ile tutarlı olmalı)
GEO_HOSTS: tuple[tuple[str, str], ...] = (
    ("kariyer.net",    "tr"),
    ("tr.indeed.com",  "tr"),
    ("yenibiris.com",  "tr"),
    ("secretcv.com",   "tr"),
    ("nl.indeed.com",  "nl"),
    ("glassdoor.nl",   "nl"),
    ("uk.indeed.com",  "gb"),
    ("linkedin.com",   None),   # guest API hafif — proxy gerekmez
)

# Host → curl_cffi impersonation profili. ÖLÇÜMLE seçildi: kariyer.net'te
# chrome110/116/120 → 403, chrome124/131 → 200. chrome110 eskimiş.
HOST_PROFILE: dict[str, str] = {
    "kariyer.net":   "chrome131",
    "indeed.com":    "chrome120",
    "glassdoor.com": "chrome120",
}

# Arama sayfasından önce ana sayfa ziyaretiyle çerez toplanacak host'lar
WARMUP_HOSTS: dict[str, str] = {
    "kariyer.net":   "https://www.kariyer.net/",
    "tr.indeed.com": "https://tr.indeed.com/",
    "nl.indeed.com": "https://nl.indeed.com/",
}

# Blok görülen host'lar — süreç ömrü boyunca premium'a yükseltilir
_escalated: set[str] = set()
_lock = threading.Lock()


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _matches(host: str, needles) -> bool:
    return any(n in host for n in needles)


def is_strict(url: str) -> bool:
    """Host baştan premium gerektiriyor mu?"""
    return _matches(host_of(url), STRICT_HOSTS)


def escalate(url: str) -> None:
    """Doğrudan denemede blok yendi — bu host'u premium'a yükselt."""
    h = host_of(url)
    if h:
        with _lock:
            _escalated.add(h)


def is_escalated(url: str) -> bool:
    h = host_of(url)
    with _lock:
        return any(h == e or h.endswith("." + e) or e in h for e in _escalated)


def escalated_hosts() -> list[str]:
    with _lock:
        return sorted(_escalated)


def premium_configured() -> bool:
    return bool(os.environ.get("PREMIUM_PROXY_URL", "").strip())


def needs_premium(url: str) -> bool:
    """Bu URL premium proxy'den mi geçmeli? (Bant genişliği kararı)"""
    if not premium_configured():
        return False
    return is_strict(url) or is_escalated(url)


def geo_for_url(url: str) -> str | None:
    """URL host'una göre gereken exit ülkesi (yoksa None)."""
    low = (url or "").lower()
    for host, cc in GEO_HOSTS:
        if host in low:
            return cc
    return None


def profile_for_url(url: str) -> str | None:
    """Host'a özel curl_cffi impersonation profili (yoksa None → rastgele)."""
    h = host_of(url)
    for host, prof in HOST_PROFILE.items():
        if host in h:
            return prof
    return None


def warmup_url(url: str) -> str | None:
    """Çerez ısıtması için önce ziyaret edilecek ana sayfa (yoksa None)."""
    h = host_of(url)
    for host, home in WARMUP_HOSTS.items():
        if host in h:
            return home
    return None


def premium_proxy(country: str | None = None) -> str | None:
    """
    PREMIUM_PROXY_URL'i (opsiyonel geo eki ile) tam proxy URL'i olarak döner.
    Bright Data geo hedefleme: kullanıcı adı sonuna -country-tr eklenir.
    """
    raw = os.environ.get("PREMIUM_PROXY_URL", "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    if not u.hostname:
        return None
    user = u.username or ""
    if country and user and "-country-" not in user:
        user = f"{user}-country-{country.lower()}"
    netloc = u.hostname if not user else f"{user}:{u.password or ''}@{u.hostname}"
    if u.port:
        netloc += f":{u.port}"
    return urlunparse((u.scheme or "http", netloc, "", "", "", ""))


def proxy_for_url(url: str) -> str | None:
    """Politika + geo birleşimi: bu URL için kullanılacak proxy (yoksa None)."""
    if not needs_premium(url):
        return None
    return premium_proxy(geo_for_url(url))


def mask(proxy: str | None) -> str:
    """Log'a parola yazmamak için kimlik bilgisini maskeler."""
    if not proxy:
        return "direct"
    if "://" not in proxy:
        return proxy
    u = urlparse(proxy)
    return f"{u.scheme}://***@{u.hostname}:{u.port}" if u.username else proxy


if __name__ == "__main__":
    os.environ.setdefault("PREMIUM_PROXY_URL", "http://user:pass@gw.example.com:22225")
    for u in [
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs",
        "https://api.lever.co/v0/postings/spotify",
        "https://www.kariyer.net/is-ilanlari?q=veri",
        "https://tr.indeed.com/is?q=nlp",
        "https://nl.indeed.com/vacatures?q=data",
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
    ]:
        print(f"{u[:58]:60s} premium={needs_premium(u)!s:5s} "
              f"geo={geo_for_url(u)} profil={profile_for_url(u)} "
              f"proxy={mask(proxy_for_url(u))}")
