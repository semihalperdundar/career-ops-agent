#!/usr/bin/env python3
"""
CareerOps — CF Scraper (Cloudflare Bypass Çekirdeği)
=====================================================
curl_cffi kullanarak TLS parmak izini gerçek bir Chrome/Firefox gibi
taklit eder. Cloudflare TLS Fingerprinting ve JS Challenge'ı standart
Python requests'in aksine geçer.

Temel prensipler:
  1. TLS Parmak İzi — curl_cffi.Session(impersonate="chrome124") ile
     Chrome/Firefox'un tam TLS握手 yapısı taklit edilir.
  2. Oturum Kalıcılığı — _SessionCache, aynı proxy için tek session
     tutar; CF clearance çerezi yeniden istek yapıldığında kaybolmaz.
  3. Proxy Entegrasyonu — ProxyPool (proxy_manager.py) ile tam uyumlu;
     bad proxy'leri işaretler, fallback olarak proxysiz dener.
  4. İnsanlaştırma — rastgele gecikme, Referer zinciri, Accept-Language,
     sec-ch-ua, sec-fetch-* header seti.
  5. Drop-in Uyumluluk — make_cf_fetch() fonksiyonu portal_scrapers.py
     FetchFn tipine birebir uygundur.

Kurulum:
    pip install curl-cffi

Modül olarak kullanım:
    from cf_scraper import make_cf_fetch, safe_cf_get

    pool     = ProxyPool()
    fetch_fn = make_cf_fetch(pool)          # portal_scrapers ile uyumlu
    resp     = safe_cf_get(url, pool=pool)  # doğrudan kullanım

CLI:
    python cf_scraper.py --url https://www.linkedin.com/jobs
    python cf_scraper.py --url https://example.com --profile chrome120 --verbose
    python cf_scraper.py --url https://example.com --proxy 1.2.3.4:8080
    python cf_scraper.py --url https://example.com --no-pool --save-html out.html
"""

from __future__ import annotations

import io
import random
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# ── Windows UTF-8 düzeltmesi ──────────────────────────────────────────────────
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── curl_cffi — TLS impersonation motoru ──────────────────────────────────────
try:
    from curl_cffi import requests as _cf_req
    _CF_AVAILABLE = True
except ImportError:
    import requests as _cf_req  # type: ignore[no-redef]
    _CF_AVAILABLE = False

# ── proxy_manager opsiyonel ───────────────────────────────────────────────────
try:
    from proxy_manager import ProxyPool
    _POOL_AVAILABLE = True
except ImportError:
    _POOL_AVAILABLE = False
    ProxyPool = None  # type: ignore[assignment,misc]

# ─────────────────────────────────────────────────────────────────────────────
# Tarayıcı profilleri
# ─────────────────────────────────────────────────────────────────────────────

# curl_cffi'nin desteklediği impersonation etiketleri (en güncel önce)
BROWSER_PROFILES: list[str] = [
    "chrome124",
    "chrome120",
    "chrome116",
    "chrome110",
    "chrome107",
    "firefox122",
    "firefox117",
]

# Her profile karşılık gelen User-Agent
_UA: dict[str, str] = {
    "chrome124": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "chrome120": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "chrome116": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
    ),
    "chrome110": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
    ),
    "chrome107": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36"
    ),
    "firefox122": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) "
        "Gecko/20100101 Firefox/122.0"
    ),
    "firefox117": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:117.0) "
        "Gecko/20100101 Firefox/117.0"
    ),
}

# Chrome sec-ch-ua header'ları (Firefox bu header'ı göndermez)
_SEC_CH_UA: dict[str, str | None] = {
    "chrome124": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "chrome120": '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
    "chrome116": '"Chromium";v="116", "Google Chrome";v="116", "Not-A.Brand";v="99"',
    "chrome110": '"Chromium";v="110", "Google Chrome";v="110", "Not-A.Brand";v="99"',
    "chrome107": '"Chromium";v="107", "Google Chrome";v="107", "Not-A.Brand";v="99"',
    "firefox122": None,
    "firefox117": None,
}

# Gerçekçi Accept-Language değerleri
_ACCEPT_LANGUAGES: list[str] = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,nl;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.9,de;q=0.8",
    "en-US,en;q=0.9,tr;q=0.8",
    "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
]

# Gerçekçi Referer zinciri (arama motorundan gelen trafik simülasyonu)
_REFERERS: list[str | None] = [
    "https://www.google.com/",
    "https://www.google.com/search?q=data+scientist+jobs",
    "https://www.google.com/search?q=machine+learning+engineer+jobs+netherlands",
    "https://www.bing.com/search?q=data+engineer+jobs",
    "https://duckduckgo.com/?q=ai+jobs+europe",
    None,  # doğrudan ziyaret
]

# ─────────────────────────────────────────────────────────────────────────────
# Gecikme sabitleri (saniye)
# ─────────────────────────────────────────────────────────────────────────────
_DELAY_NORMAL = (1.5, 3.8)   # başarılı istekler arası
_DELAY_403    = (8.0, 18.0)  # 403: proxy değiştir + bekle
_DELAY_429    = (25.0, 50.0) # 429: rate-limit, uzun bekle
_DELAY_503    = (12.0, 30.0) # 503: CF challenge, bekle
_DELAY_ERROR  = (1.0, 3.5)   # bağlantı hatası sonrası kısa bekle


# ─────────────────────────────────────────────────────────────────────────────
# Header oluşturucu
# ─────────────────────────────────────────────────────────────────────────────

def _build_headers(
    profile:  str,
    referer:  str | None = None,
    extra:    dict | None = None,
) -> dict[str, str]:
    """
    Verilen tarayıcı profiline göre gerçekçi HTTP başlık seti oluşturur.

    Chrome profillerinde sec-ch-ua, sec-fetch-* gibi modern başlıklar
    eklenir. Firefox profillerinde bu başlıklar atlanır (gerçek davranış).
    """
    ua        = _UA.get(profile, _UA["chrome124"])
    sec_ua    = _SEC_CH_UA.get(profile)
    lang      = random.choice(_ACCEPT_LANGUAGES)
    is_chrome = profile.startswith("chrome")

    headers: dict[str, str] = {
        "User-Agent":               ua,
        "Accept":                   (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language":          lang,
        "Accept-Encoding":          "gzip, deflate, br",
        "Connection":               "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "DNT":                      "1",
    }

    if is_chrome and sec_ua:
        # Chrome'a özgü hint başlıkları — CF bunları bekler
        headers["sec-ch-ua"]          = sec_ua
        headers["sec-ch-ua-mobile"]   = "?0"
        headers["sec-ch-ua-platform"] = '"Windows"'
        headers["Sec-Fetch-Dest"]     = "document"
        headers["Sec-Fetch-Mode"]     = "navigate"
        headers["Sec-Fetch-Site"]     = "cross-site" if referer else "none"
        headers["Sec-Fetch-User"]     = "?1"
        headers["Cache-Control"]      = "max-age=0"

    if referer:
        headers["Referer"] = referer

    if extra:
        headers.update(extra)

    return headers


# ─────────────────────────────────────────────────────────────────────────────
# CFSession — tek oturum (bir proxy, bir tarayıcı profili)
# ─────────────────────────────────────────────────────────────────────────────

class CFSession:
    """
    curl_cffi tabanlı oturum. Bir proxy + bir tarayıcı profili taşır.

    CF clearance çerezi oturuma bağlıdır: aynı proxy üzerinden yapılan
    sonraki isteklerde çerez otomatik gönderilir. Bu yüzden aynı proxy
    için session'ı yeniden kullanmak CF bypass başarı oranını artırır.

    Context manager olarak kullanılabilir:
        with CFSession(proxy="1.2.3.4:8080") as s:
            resp = s.get("https://example.com")
    """

    def __init__(
        self,
        proxy:   str | None = None,  # "ip:port" veya None (doğrudan)
        profile: str | None = None,  # None → rastgele seç
    ) -> None:
        self.proxy   = proxy
        self.profile = profile or random.choice(BROWSER_PROFILES)
        self._sess   = self._make_session()

    # ─── iç yardımcılar ─────────────────────────────────────────────────────

    def _make_session(self):
        if _CF_AVAILABLE:
            sess = _cf_req.Session(impersonate=self.profile)
        else:
            # Fallback: curl_cffi yok, standard requests kullan
            sess = _cf_req.Session()

        if self.proxy:
            purl = f"http://{self.proxy}"
            sess.proxies = {"http": purl, "https": purl}

        return sess

    # ─── genel API ──────────────────────────────────────────────────────────

    def get(
        self,
        url:           str,
        referer:       str | None  = None,
        extra_headers: dict | None = None,
        timeout:       int         = 20,
        **kwargs,
    ):
        """
        GET isteği gönderir. Çerezler session'da birikir (clearance dahil).
        """
        hdrs = _build_headers(self.profile, referer=referer, extra=extra_headers)
        return self._sess.get(url, headers=hdrs, timeout=timeout, **kwargs)

    def close(self) -> None:
        try:
            self._sess.close()
        except Exception:
            pass

    def __enter__(self) -> "CFSession":
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
# _SessionCache — proxy başına oturum önbelleği
# ─────────────────────────────────────────────────────────────────────────────

class _SessionCache:
    """
    Her proxy için bir CFSession saklar; aynı IP'den gelen CF clearance
    çerezini korur. Kapasite aşılınca en eski girdiyi tahliye eder.

    Thread-safe: hem proxy döngüsü hem de paralel fetch thread'leri
    aynı önbelleğe erişebilir.
    """

    def __init__(self, maxsize: int = 20) -> None:
        self._cache:   dict[str | None, CFSession] = {}
        self._order:   list[str | None]             = []
        self._lock:    threading.Lock               = threading.Lock()
        self._maxsize: int                          = maxsize

    def get_or_create(
        self,
        proxy:   str | None,
        profile: str | None = None,
    ) -> CFSession:
        with self._lock:
            if proxy in self._cache:
                # Mevcut oturumu döndür — clearance çerezi korunur
                return self._cache[proxy]

            # Yeni oturum oluştur
            sess = CFSession(proxy=proxy, profile=profile)
            self._cache[proxy] = sess
            self._order.append(proxy)

            # Kapasite aşıldıysa en eski oturumu kapat
            while len(self._cache) > self._maxsize:
                oldest = self._order.pop(0)
                old    = self._cache.pop(oldest, None)
                if old:
                    old.close()

            return sess

    def evict(self, proxy: str | None) -> None:
        """Başarısız proxy'nin oturumunu önbellekten çıkarır."""
        with self._lock:
            sess = self._cache.pop(proxy, None)
            if proxy in self._order:
                self._order.remove(proxy)
            if sess:
                sess.close()

    def clear(self) -> None:
        with self._lock:
            for s in self._cache.values():
                s.close()
            self._cache.clear()
            self._order.clear()


# Modül düzeyinde paylaşılan önbellek (import başına bir kez oluşur)
_session_cache = _SessionCache(maxsize=20)


# ─────────────────────────────────────────────────────────────────────────────
# safe_cf_get — proxy rotasyonlu, insanlaştırılmış GET
# ─────────────────────────────────────────────────────────────────────────────

def safe_cf_get(
    url:     str,
    pool=None,               # ProxyPool | None
    retries: int  = 4,
    timeout: int  = 20,
    delay:   bool = True,
    referer: str | None   = None,
    headers: dict | None  = None,
    verbose: bool = False,
):
    """
    curl_cffi tabanlı GET isteği. proxy_manager.safe_get() ile aynı
    imzayı kullanır — drop-in olarak kullanılabilir.

    Dönüş:
      - 200/30x/40x için Response nesnesi
      - Tüm denemeler başarısız olursa None

    Yeniden deneme stratejisi:
      - 403 → proxy değiştir, _session_cache.evict(), bekle
      - 429 → uzun bekle (rate-limit)
      - 503 → CF challenge bekleme süresi, bekle
      - Hata → proxy'yi bad işaretle, kısa bekle
      - Son deneme → proxysiz (doğrudan) dene
    """
    if referer is None:
        referer = random.choice(_REFERERS)

    for attempt in range(retries):
        # Son denemede proxysiz dene (fallback — kendi IP'imizle CF dene)
        is_last   = (attempt == retries - 1)
        proxy_str = None

        if pool and not is_last:
            proxy_str = pool.get() if hasattr(pool, "get") else None

        profile = random.choice(BROWSER_PROFILES[:4])  # tercihan en güncel profiller

        try:
            sess = _session_cache.get_or_create(proxy_str, profile=profile)
            resp = sess.get(url, referer=referer, extra_headers=headers, timeout=timeout)
            code = resp.status_code

            if verbose:
                tag = f"proxy={proxy_str or 'direct'} profile={profile}"
                print(f"   [CF] {code} {url[:65]}  ({tag})")

            # ── Başarılı ────────────────────────────────────────────────────
            if code == 200:
                if delay:
                    time.sleep(random.uniform(*_DELAY_NORMAL))
                return resp

            # ── CF / erişim engeli ──────────────────────────────────────────
            elif code == 403:
                if verbose:
                    print(f"   [CF] 403 — oturum kapatılıyor, proxy değiştiriliyor ({attempt+1}/{retries})")
                _session_cache.evict(proxy_str)
                if proxy_str and pool:
                    pool.mark_bad(proxy_str)
                if delay:
                    time.sleep(random.uniform(*_DELAY_403))

            # ── Rate-limit ──────────────────────────────────────────────────
            elif code == 429:
                wait = random.uniform(*_DELAY_429)
                if verbose:
                    print(f"   [CF] 429 rate-limit — {wait:.0f}s bekleniyor")
                time.sleep(wait)

            # ── CF JS challenge / geçici hata ───────────────────────────────
            elif code == 503:
                wait = random.uniform(*_DELAY_503)
                if verbose:
                    print(f"   [CF] 503 challenge — {wait:.0f}s bekleniyor")
                time.sleep(wait)

            # ── Diğer HTTP kodları (2xx/3xx/4xx) — döndür ──────────────────
            elif code < 500:
                return resp

        except Exception as exc:
            if verbose:
                print(f"   [CF] Hata (deneme {attempt+1}): {type(exc).__name__}: {exc}")
            _session_cache.evict(proxy_str)
            if proxy_str and pool:
                pool.mark_bad(proxy_str)
            if delay and not is_last:
                time.sleep(random.uniform(*_DELAY_ERROR))

    return None


# ─────────────────────────────────────────────────────────────────────────────
# FetchFn fabrikası — portal_scrapers.py ile tam uyumlu
# ─────────────────────────────────────────────────────────────────────────────

FetchFn = Callable[..., object | None]


def make_cf_fetch(pool=None, verbose: bool = False) -> FetchFn:
    """
    curl_cffi tabanlı fetch fonksiyonu döner.

    portal_scrapers.py'daki FetchFn tipine uygundur:
        fetch_fn(url, **kwargs) -> Response | None

    telegram-daily.py'de _make_fetch() yerine kullanılabilir:
        fetch_fn = make_cf_fetch(pool)
        extra_raw = fetch_all_extra(fetch_fn=fetch_fn, ...)
    """
    def _fetch(url: str, **kwargs) -> object | None:
        timeout = kwargs.pop("timeout", 20)
        headers = kwargs.pop("headers", None)
        return safe_cf_get(
            url,
            pool=pool,
            timeout=timeout,
            delay=True,
            headers=headers,
            verbose=verbose,
        )
    return _fetch


# ─────────────────────────────────────────────────────────────────────────────
# fetch_pages — çoklu URL döngüsü (insanlaştırılmış)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_pages(
    urls:    list[str],
    pool=None,
    timeout: int  = 20,
    verbose: bool = False,
) -> list[tuple[str, object | None]]:
    """
    URL listesini sırasıyla çeker. İstekler arası rastgele bekleme uygular.
    Sayfa döngüleri (örn. Glassdoor slug URL'leri) için uygundur.

    Dönüş: [(url, response_or_None), ...]
    """
    results: list[tuple[str, object | None]] = []
    for i, url in enumerate(urls):
        resp = safe_cf_get(url, pool=pool, timeout=timeout, verbose=verbose)
        results.append((url, resp))
        if i < len(urls) - 1:
            wait = random.uniform(*_DELAY_NORMAL)
            if verbose:
                print(f"   [CF] Sonraki URL'ye geçmeden {wait:.1f}s bekleniyor...")
            time.sleep(wait)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# CLI — geliştirici test arayüzü
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="CareerOps CF Scraper — Cloudflare bypass test aracı",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Örnekler:
  python cf_scraper.py --url https://www.linkedin.com/jobs --verbose
  python cf_scraper.py --url https://example.com --profile chrome120
  python cf_scraper.py --url https://example.com --proxy 1.2.3.4:8080
  python cf_scraper.py --url https://example.com --no-pool --save-html out.html
        """,
    )
    parser.add_argument("--url",       required=True,              help="Test edilecek URL")
    parser.add_argument(
        "--profile",
        choices=BROWSER_PROFILES,
        default=None,
        help="Tarayıcı profili (varsayılan: rastgele seçilir)",
    )
    parser.add_argument("--proxy",     default=None,               help="Sabit proxy: ip:port")
    parser.add_argument("--retries",   type=int, default=3,        help="Yeniden deneme sayısı")
    parser.add_argument("--timeout",   type=int, default=20,       help="İstek zaman aşımı (s)")
    parser.add_argument("--no-pool",   action="store_true",        help="Proxy havuzunu kullanma")
    parser.add_argument("--no-delay",  action="store_true",        help="Gecikmesiz mod (test)")
    parser.add_argument("--verbose",   action="store_true",        help="Ayrıntılı çıktı")
    parser.add_argument("--save-html", metavar="DOSYA", default=None, help="HTML'yi dosyaya kaydet")
    args = parser.parse_args()

    # ── Durum özeti ──────────────────────────────────────────────────────────
    print("=" * 60)
    print("  CareerOps CF Scraper")
    print("=" * 60)
    print(f"🔐 curl_cffi  : {'✓ mevcut — TLS impersonation aktif' if _CF_AVAILABLE else '✗ bulunamadı — fallback: requests'}")
    print(f"🔌 ProxyPool  : {'✓ mevcut' if _POOL_AVAILABLE else '✗ bulunamadı'}")

    if not _CF_AVAILABLE:
        print("\n⚠️  UYARI: curl_cffi yüklü değil. Cloudflare bypass çalışmayacak!")
        print("   Kurulum: pip install curl-cffi\n")

    # ── Proxy kaynağı ────────────────────────────────────────────────────────
    pool = None

    if args.proxy:
        print(f"🌐 Proxy      : {args.proxy} (sabit)")
    elif not args.no_pool and _POOL_AVAILABLE:
        print("📦 Proxy havuzu yükleniyor...")
        pool = ProxyPool(verbose=True)
        print(f"   Aktif proxy: {pool.size()}")
    else:
        print("🌐 Proxy      : yok (doğrudan bağlantı)")

    # ── Profil seç ───────────────────────────────────────────────────────────
    profile = args.profile or random.choice(BROWSER_PROFILES[:3])
    print(f"🎭 Profil     : {profile}")
    print(f"🔗 URL        : {args.url}")
    print()

    # ── İstek gönder ─────────────────────────────────────────────────────────
    start = time.monotonic()

    if args.proxy:
        # Sabit proxy ile tek oturum (clearance cookie testi için)
        with CFSession(proxy=args.proxy, profile=profile) as sess:
            try:
                resp = sess.get(args.url, timeout=args.timeout)
                elapsed = time.monotonic() - start
                print(f"✅ HTTP {resp.status_code}  ({elapsed:.2f}s)")
                print(f"   İçerik boyutu : {len(resp.content):,} byte")
                print(f"   Content-Type  : {resp.headers.get('content-type', 'bilinmiyor')}")
                cf_cookie = "; ".join(
                    f"{c}=…" for c in resp.cookies.keys()
                    if "clearance" in c.lower() or "cf_" in c.lower()
                )
                if cf_cookie:
                    print(f"   CF çerezleri  : {cf_cookie}")
                preview = resp.text[:600].replace("\n", " ")
                print(f"\n   Önizleme: {preview}\n")
                if args.save_html:
                    Path(args.save_html).write_text(resp.text, encoding="utf-8")
                    print(f"   💾 HTML kaydedildi → {args.save_html}")
            except Exception as exc:
                print(f"❌ Hata: {type(exc).__name__}: {exc}")
    else:
        resp = safe_cf_get(
            args.url,
            pool=pool,
            retries=args.retries,
            timeout=args.timeout,
            delay=not args.no_delay,
            verbose=args.verbose,
        )
        elapsed = time.monotonic() - start
        if resp:
            print(f"✅ HTTP {resp.status_code}  ({elapsed:.2f}s)")
            print(f"   İçerik boyutu : {len(resp.content):,} byte")
            print(f"   Content-Type  : {resp.headers.get('content-type', 'bilinmiyor')}")
            cf_cookies = [k for k in resp.cookies.keys() if "clearance" in k.lower() or "cf_" in k.lower()]
            if cf_cookies:
                print(f"   CF çerezleri  : {', '.join(cf_cookies)}")
            preview = resp.text[:600].replace("\n", " ")
            print(f"\n   Önizleme: {preview}\n")
            if args.save_html:
                Path(args.save_html).write_text(resp.text, encoding="utf-8")
                print(f"   💾 HTML kaydedildi → {args.save_html}")
        else:
            print(f"❌ Tüm denemeler başarısız ({elapsed:.2f}s)")
            print("   İpuçları:")
            print("   • curl-cffi yüklü mü?  →  pip install curl-cffi")
            print("   • Proxy havuzu dolu mu? →  python proxy_manager.py")
            print("   • --verbose ile detayları görebilirsin")
