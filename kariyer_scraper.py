#!/usr/bin/env python3
"""
CareerOps — Kariyer.net Scraper (proxy destekli)
=================================================
TEŞHİS NOTU (ölçümle doğrulandı, 2026-08-27):
Kariyer.net bizi ENGELLEMİYORDU. Türkiye IP'sinden HTTP 200 + 580 KB
dönüyor ve 43-52 ilan linki sayfada mevcut. "0 ilan" sonucunun sebebi
ayrıştırıcıydı:

  • Eski kod __NEXT_DATA__ (Next.js) ve JSON-LD JobPosting arıyordu.
    Kariyer.net bir NUXT uygulaması — ikisi de sayfada YOK.
  • Arama `?q=` parametresiyle yapılıyordu; site slug tabanlı yol kullanıyor
    (/is-ilanlari/veri-bilimci). `?q=` alakasız genel liste döndürüyordu.
  • Slug TÜRKÇE olmalı: "data-scientist" alakasız sonuç verir.

Bu modül ilanları kart DOM'undaki HTML attribute'larından çeker
(positionname, cityname, countryname, time, worktypetext) — metin
ayrıştırmasına göre çok daha dayanıklı.

Proxy: datacenter IP'lerinde (GitHub Actions) site TR exit ister.
Kimlik bilgisi sırayla aranır:
    PREMIUM_PROXY_URL           tam URL  http://user:pass@host:port
    KARIYER_PROXY_URL           aynı format, yalnızca bu kaynak için
    PROXY_HOST/PORT/USER/PASS   ayrık değişkenler

Kullanım:
    from kariyer_scraper import fetch_kariyer_jobs
    jobs = fetch_kariyer_jobs(["veri bilimci", "yapay zeka"], verbose=True)

    python kariyer_scraper.py --check      # proxy + erişim testi
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
import unicodedata
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup
    _BS4 = True
    # bs4'un import edilmesi lxml'in KURULU olduğunu kanıtlamaz; parse_jobs
    # lxml'i zorunlu kılarsa FeatureNotFound döngüden kaçar ve "asla istisna
    # fırlatmaz" sözleşmesini bozar. Ayrıştırıcıyı burada bir kez seçeriz.
    try:
        BeautifulSoup("<p></p>", "lxml")
        _PARSER = "lxml"
    except Exception:
        _PARSER = "html.parser"
except ImportError:
    _BS4 = False
    _PARSER = "html.parser"

try:
    from curl_cffi import requests as cf_requests
    _CURL_CFFI = True
except ImportError:
    _CURL_CFFI = False

import requests as std_requests

BASE = "https://www.kariyer.net"
SEARCH = f"{BASE}/is-ilanlari"

# curl_cffi impersonation profili — ÖLÇÜMLE seçildi (2026-08-27 matrisi):
#   chrome110/116/120 → 403 (profil eskimiş / işaretlenmiş)
#   chrome124/131     → 200  ANCAK yalnızca User-Agent OVERRIDE EDİLMEZSE
#   firefox133/safari17_0 → 200
# Kritik kural: curl_cffi profiline uygun UA'yı kendisi gönderir. Üstüne
# kendi UA'mızı yazmak TLS parmak izi ile başlığı çelişkiye düşürüyor ve
# koruma katmanı tam olarak bunu yakalıyor → 403.
IMPERSONATE = os.getenv("KARIYER_IMPERSONATE", "chrome131")

# Türkçe slug ZORUNLU — İngilizce slug alakasız genel liste döndürür
DEFAULT_QUERIES = [
    "veri bilimci", "veri analisti", "veri mühendisi", "yapay zeka",
    "makine öğrenmesi", "iş analisti", "veri tabanı yöneticisi",
    "raporlama uzmanı", "doğal dil işleme", "yazılım mühendisi",
]

_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

_TR_MAP = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
})

_BLOCK_SIGNS = ("datadome", "captcha", "just a moment", "cf-browser-verification",
                "access denied", "erişim engellendi", "recaptcha")


# ─────────────────────────────────────────────────────────────────────────────
# Proxy çözümleme
# ─────────────────────────────────────────────────────────────────────────────

def resolve_proxy(country: str | None = "tr") -> str | None:
    """
    Ortam değişkenlerinden tam proxy URL'i üretir.

    Öncelik: KARIYER_PROXY_URL → PREMIUM_PROXY_URL → ayrık değişkenler.
    `country` verilirse Bright Data tarzı geo hedefleme için kullanıcı adına
    -country-XX eki eklenir (zaten varsa dokunulmaz).
    """
    raw = (os.getenv("KARIYER_PROXY_URL")
           or os.getenv("PREMIUM_PROXY_URL") or "").strip()

    if not raw:
        host = os.getenv("PROXY_HOST", "").strip()
        port = os.getenv("PROXY_PORT", "").strip()
        if not host or not port:
            return None
        user = os.getenv("PROXY_USER", "").strip()
        pwd = os.getenv("PROXY_PASS") or os.getenv("PROXY_KEY") or ""
        auth = f"{user}:{pwd}@" if user else ""
        raw = f"http://{auth}{host}:{port}"

    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    if not parsed.hostname:
        return None

    user = parsed.username or ""
    if country and user and "-country-" not in user:
        user = f"{user}-country-{country.lower()}"

    netloc = parsed.hostname
    if user:
        netloc = f"{user}:{parsed.password or ''}@{netloc}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return f"{parsed.scheme or 'http'}://{netloc}"


def mask(proxy: str | None) -> str:
    if not proxy:
        return "direct"
    p = urlparse(proxy)
    return f"{p.scheme}://***@{p.hostname}:{p.port}" if p.username else proxy


# ─────────────────────────────────────────────────────────────────────────────
# Oturum
# ─────────────────────────────────────────────────────────────────────────────

class KariyerSession:
    """
    curl_cffi TLS impersonation + proxy + çerez ısıtması taşıyan oturum.

    curl_cffi yoksa standart requests'e düşer (TLS parmak izi zayıflar ama
    Türkiye IP'sinden yine çalışır).
    """

    def __init__(self, proxy: str | None = None, impersonate: str = IMPERSONATE):
        self.proxy = proxy
        self.impersonate = impersonate
        self.ua = random.choice(_UA_POOL)
        self._warmed = False
        self._sess = self._build()

    def _build(self):
        if _CURL_CFFI:
            sess = cf_requests.Session(impersonate=self.impersonate)
        else:
            sess = std_requests.Session()
        if self.proxy:
            sess.proxies = {"http": self.proxy, "https": self.proxy}
        return sess

    def _headers(self, referer: str = BASE) -> dict:
        """
        curl_cffi kullanılıyorsa User-Agent GÖNDERİLMEZ: impersonation profili
        kendi tutarlı UA'sını koyar. Override etmek 403'e yol açıyor.
        Düz requests'te UA zorunlu (UA'sız istek de 403 alıyor).
        """
        h = {**_HEADERS, "Referer": referer}
        # Sec-Fetch-Site referer ile TUTARLI olmalı: harici bir referer'la
        # "same-origin" göndermek tarayıcıda imkânsız bir kombinasyon ve
        # tam olarak koruma katmanlarının aradığı türden bir çelişki.
        host = urlparse(referer).hostname or ""
        h["Sec-Fetch-Site"] = "same-origin" if host.endswith("kariyer.net") else "cross-site"
        if not _CURL_CFFI:
            h["User-Agent"] = self.ua
        return h

    def warm_up(self, timeout: int = 25) -> None:
        """
        Arama sayfasından önce ana sayfayı ziyaret ederek çerez toplar.
        Koruma katmanları ilk isteği arama sayfasına gelen ziyaretçiyi bot
        sayar; gerçek kullanıcı önce ana sayfayı görür.
        """
        if self._warmed:
            return
        self._warmed = True
        try:
            self._sess.get(BASE,
                           headers=self._headers("https://www.google.com/"),
                           timeout=timeout)
            time.sleep(random.uniform(1.2, 2.6))
        except Exception:
            pass

    def get(self, url: str, timeout: int = 30, referer: str = BASE):
        self.warm_up(timeout=timeout)
        return self._sess.get(url, headers=self._headers(referer), timeout=timeout)

    def close(self) -> None:
        try:
            self._sess.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Ayrıştırma
# ─────────────────────────────────────────────────────────────────────────────

def slugify(query: str) -> str:
    """'Veri Bilimci' → 'veri-bilimci' (Türkçe harf dönüşümü dahil)."""
    s = query.strip().lower().translate(_TR_MAP)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def detect_block(html: str, card_count: int = 0) -> str | None:
    """
    Koruma katmanı yanıtını tespit eder.

    KART SAYISI OTORİTEDİR: en az bir ilan kartı ayrıştırılabildiyse sayfa
    gerçektir, blok değildir. Yalnızca imza aramak yanlış pozitif üretiyordu —
    516 KB'lık meşru liste sayfası giriş formu için bir recaptcha script'i
    içeriyor ve saf imza taraması bunu "CAPTCHA bloğu" sanıyordu.
    """
    if card_count > 0:
        return None
    if not html or len(html) < 5000:
        return "EMPTY_OR_SHORT"
    # Uygulama kabuğu render olduysa sayfa gerçektir; kart yokluğu yalnızca
    # "bu sorguda sonuç yok" demektir. Bunu blok saymak yanlış teşhis üretir
    # ve gereksiz yere pahalı Playwright yedeğini tetikler.
    if 'id="__nuxt"' in html or "list-items-wrapper" in html:
        return None
    low = html[:20000].lower()
    for sign in _BLOCK_SIGNS:
        if sign in low:
            return sign.upper()
    return "NO_CARDS"


def parse_jobs(html: str) -> list[dict]:
    """
    İlan kartlarını HTML attribute'larından çıkarır.

    Kart yapısı (doğrulandı):
        <div class="job-list-card-item"
             positionname="Veri Analisti" cityname="Ankara"
             countryname="Türkiye" time="12 saat" worktypetext="Tam zamanlı">
          <a class="k-ad-card" href="/is-ilani/...">
          <img data-test="company-image" alt="ŞİRKET A.Ş"/>

    Attribute tabanlı okuma, metin/CSS seçicilerine göre tasarım
    değişikliklerine çok daha dayanıklı.
    """
    if not _BS4 or not html:
        return []
    soup = BeautifulSoup(html, _PARSER)
    cards = soup.select('div.job-list-card-item, [data-test="ad-card"]')
    jobs: list[dict] = []
    seen: set[str] = set()

    for card in cards:
        link = card.select_one('a[href^="/is-ilani/"]')
        if not link:
            continue
        href = link.get("href", "")
        url = href if href.startswith("http") else f"{BASE}{href}"
        if url in seen:
            continue

        title = (card.get("positionname") or link.get("title")
                 or link.get_text(" ", strip=True))[:200].strip()
        if not title:
            continue

        # Seçici listesi ("a, b") belge sırasındaki İLK img'i döndürür —
        # şirket logosu yerine rastgele bir ikon gelebiliyordu. Spesifik
        # seçici önce, ancak o başarısız olursa gevşek seçici.
        img = card.select_one('img[data-test="company-image"]')
        if img is None:
            img = card.select_one("img[alt]")
        company = (img.get("alt") if img else "") or ""

        city = card.get("cityname") or ""
        country = card.get("countryname") or "Türkiye"
        location = ", ".join(x for x in (city, country) if x) or "Türkiye"

        seen.add(url)
        jobs.append({
            "title": title,
            "url": url,
            "company": company.strip(),
            "location": location,
            "source": "kariyer.net",
            # time="12 saat" → freshness.py doğrudan ayrıştırır
            "posted_at": (card.get("time") or "").strip(),
            "work_type": (card.get("worktypetext") or "").strip(),
            "work_model": (card.get("workmodeltext") or "").strip(),
        })

    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# Genel API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_kariyer_jobs(
    queries: list[str] | None = None,
    max_queries: int = 6,
    country: str | None = "tr",
    budget_sec: float | None = 150.0,
    verbose: bool = False,
) -> list[dict]:
    """
    Kariyer.net'ten ilan çeker. Hata durumunda kısmi sonuç döner, asla
    istisna fırlatmaz — ana akışı kesmemesi gerekir.
    """
    queries = (queries or DEFAULT_QUERIES)[:max_queries]
    proxy = resolve_proxy(country)
    session = KariyerSession(proxy)
    started = time.monotonic()
    out: list[dict] = []
    seen: set[str] = set()

    if verbose:
        engine = f"curl_cffi/{IMPERSONATE}" if _CURL_CFFI else "requests"
        print(f"   kariyer.net — motor={engine} proxy={mask(proxy)}", flush=True)

    def pace() -> None:
        """
        Her denemeden SONRA bekle — hata yollarında da. Aksi halde bir 403
        fırtınası ücretli TR exit üzerinden gecikmesiz burst'e dönüşür ve
        tam da kaçınmaya çalıştığımız hız limitini tetikler.
        """
        time.sleep(random.uniform(1.5, 3.2))

    try:
        for query in queries:
            if budget_sec and (time.monotonic() - started) > budget_sec:
                if verbose:
                    print(f"   ⏱  bütçe doldu ({budget_sec:.0f}s) — kısmi sonuç",
                          flush=True)
                break

            url = f"{SEARCH}/{slugify(query)}"
            try:
                resp = session.get(url)
            except Exception as exc:
                if verbose:
                    print(f"   ✗ '{query}': {type(exc).__name__}", flush=True)
                pace()
                continue

            html = getattr(resp, "text", "") or ""
            if getattr(resp, "status_code", 0) != 200:
                if verbose:
                    print(f"   ✗ '{query}': HTTP {resp.status_code}", flush=True)
                pace()
                continue

            # parse_jobs kendi istisnalarını yutmalı; yine de sözleşmeyi
            # burada da güvenceye alıyoruz (ayrıştırıcı hatası toplanan
            # ilanları çöpe atmamalı).
            try:
                found = parse_jobs(html)
            except Exception as exc:
                if verbose:
                    print(f"   ✗ '{query}': ayrıştırma hatası "
                          f"{type(exc).__name__}", flush=True)
                pace()
                continue

            block = detect_block(html, card_count=len(found))
            if block:
                if verbose:
                    print(f"   🚨 '{query}': koruma katmanı [{block}]", flush=True)
                pace()
                continue

            new = [j for j in found if j["url"] not in seen]
            seen.update(j["url"] for j in new)
            out.extend(new)
            if verbose:
                print(f"   ✓ '{query}': {len(found)} kart, {len(new)} yeni",
                      flush=True)
            pace()
    finally:
        session.close()

    return out


def self_check() -> int:
    """Proxy + erişim + ayrıştırma sağlık kontrolü. 0 = sağlıklı."""
    proxy = resolve_proxy("tr")
    print("═" * 58)
    print("KARIYER.NET SAĞLIK KONTROLÜ")
    print("═" * 58)
    print(f"curl_cffi     : {'var (' + IMPERSONATE + ')' if _CURL_CFFI else 'YOK'}")
    print(f"bs4/lxml      : {'var' if _BS4 else 'YOK'}")
    print(f"proxy         : {mask(proxy)}")
    if not proxy:
        print("  ⚠ proxy yok — datacenter IP'sinde (CI) engellenme beklenir")

    if proxy:
        try:
            r = std_requests.get(
                "http://ip-api.com/json/?fields=query,countryCode",
                proxies={"http": proxy, "https": proxy}, timeout=25)
            d = r.json()
            cc = d.get("countryCode")
            print(f"çıkış IP      : {d.get('query')} → {cc} "
                  f"{'✓' if cc == 'TR' else '⚠ TR bekleniyordu'}")
        except Exception as exc:
            print(f"çıkış IP      : ✗ {type(exc).__name__}")

    jobs = fetch_kariyer_jobs(["veri bilimci"], max_queries=1, verbose=True)
    print("─" * 58)
    print(f"çekilen ilan  : {len(jobs)}")
    for j in jobs[:5]:
        print(f"  {j['title'][:40]:42s} {j['location'][:20]:22s} {j['posted_at']}")
    print("═" * 58)
    print("SONUÇ: " + ("SAĞLIKLI" if jobs else "İLAN ÇEKİLEMEDİ"))
    return 0 if jobs else 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(self_check())
    for j in fetch_kariyer_jobs(verbose=True):
        print(f"{j['title'][:44]:46s} {j['company'][:26]:28s} "
              f"{j['location'][:22]:24s} {j['posted_at']}")
