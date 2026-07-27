#!/usr/bin/env python3
"""
CareerOps — Proxy Manager
=========================
9 kaynaktan ücretsiz proxy toplar, 80 thread ile paralel doğrular,
sadece çalışanları önbelleğe alır ve dönen havuzla tüm HTTP isteklerine
anonim erişim sağlar.

Windows uyumlu. Ek paket gerekmez (yalnızca requests — Anaconda'da mevcut).

Kullanım:
    from proxy_manager import ProxyPool, safe_get

    pool = ProxyPool()              # önbellekten yükler / yoksa taze çeker
    resp = safe_get(url, pool)      # proxy + UA rotasyonu + retry

CLI:
    python proxy_manager.py         # havuzu yenile ve istatistik göster
    python proxy_manager.py --clear # önbelleği sil ve sıfırdan yenile
"""

import io
import json
import logging
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import requests

# ── Windows UTF-8 düzeltmesi ──────────────────────────────────────────────────
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

log = logging.getLogger("proxy_manager")

# ── Sabitler ──────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
CACHE_FILE   = BASE_DIR / "data" / "proxy-cache.json"
CACHE_TTL    = 1800       # saniye — 30 dakika
TEST_URL     = "https://httpbin.org/ip"
TEST_TIMEOUT = 8          # saniye — tek proxy testi için timeout
MAX_WORKERS  = 80         # eş zamanlı doğrulama thread sayısı
MIN_POOL     = 15         # bu sayının altında kalan havuz otomatik yenilenir
MAX_POOL     = 150        # en hızlı N proxy'yi sakla
FETCH_TIMEOUT = 20        # kaynak listelerini çekerken timeout

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# Sık güncellenen, yüksek kaliteli ücretsiz proxy listeleri
PROXY_SOURCES = [
    # GitHub repo listeleri (gün içinde birden çok kez güncellenir)
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    # API kaynakları
    (
        "https://api.proxyscrape.com/v2/"
        "?request=displayproxies&protocol=http&timeout=10000"
        "&country=all&ssl=all&anonymity=all"
    ),
    "https://www.proxy-list.download/api/v1/get?type=http",
]


# ── Toplama ───────────────────────────────────────────────────────────────────

def _fetch_source(url: str) -> list[str]:
    """Tek bir kaynaktan ham IP:Port satırlarını çeker."""
    try:
        r = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": USER_AGENTS[0]},
        )
        if r.status_code == 200:
            return [
                ln.strip()
                for ln in r.text.splitlines()
                if ":" in ln.strip() and ln.strip().count(".") == 3
            ]
    except Exception:
        pass
    return []


def collect_raw_proxies(verbose: bool = True) -> set[str]:
    """Tüm kaynaklardan paralel olarak proxy toplar."""
    if verbose:
        print(f"   🌐 {len(PROXY_SOURCES)} kaynaktan proxy listesi çekiliyor...", flush=True)
    proxies: set[str] = set()
    with ThreadPoolExecutor(max_workers=len(PROXY_SOURCES)) as ex:
        for batch in ex.map(_fetch_source, PROXY_SOURCES):
            proxies.update(batch)
    if verbose:
        print(f"   📋 Ham proxy sayısı: {len(proxies)}", flush=True)
    return proxies


# ── Doğrulama ─────────────────────────────────────────────────────────────────

def _test_proxy(proxy: str) -> dict | None:
    """
    Tek bir proxy'yi TEST_URL'ye istek atarak doğrular.
    Çalışırsa {proxy, latency_ms, checked_at} döner; aksi hâlde None.
    """
    try:
        start = time.monotonic()
        r = requests.get(
            TEST_URL,
            proxies={"http": f"http://{proxy}", "https": f"http://{proxy}"},
            timeout=TEST_TIMEOUT,
            headers={"User-Agent": random.choice(USER_AGENTS)},
        )
        if r.status_code == 200:
            return {
                "proxy":      proxy,
                "latency_ms": round((time.monotonic() - start) * 1000),
                "checked_at": time.time(),
            }
    except Exception:
        pass
    return None


def validate_proxies(raw: set[str], verbose: bool = True) -> list[dict]:
    """
    Ham proxy setini doğrular.
    Çalışanları gecikmeye göre sıralı döner.
    """
    if verbose:
        print(
            f"   🔬 {len(raw)} proxy doğrulanıyor "
            f"({MAX_WORKERS} eş zamanlı thread, {TEST_TIMEOUT}s timeout)...",
            flush=True,
        )
    working: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_test_proxy, p): p for p in raw}
        for fut in as_completed(futs):
            result = fut.result()
            if result:
                working.append(result)

    working.sort(key=lambda x: x["latency_ms"])
    working = working[:MAX_POOL]

    if verbose:
        if working:
            print(
                f"   ✅ {len(working)} çalışan proxy "
                f"(en hızlı: {working[0]['latency_ms']}ms, "
                f"medyan: {working[len(working)//2]['latency_ms']}ms)",
                flush=True,
            )
        else:
            print("   ⚠️  Hiç çalışan proxy bulunamadı — doğrudan bağlantı kullanılacak", flush=True)
    return working


# ── Yenileme ──────────────────────────────────────────────────────────────────

def refresh_pool(verbose: bool = True) -> list[dict]:
    """
    Tüm kaynakları tara, doğrula, önbelleğe yaz ve sıralı listeyi döndür.
    """
    if verbose:
        print("\n🔄 Proxy havuzu yenileniyor...", flush=True)
    raw     = collect_raw_proxies(verbose=verbose)
    working = validate_proxies(raw, verbose=verbose)

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(
            {"refreshed_at": time.time(), "proxies": working},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if verbose:
        print(f"   💾 Önbelleğe kaydedildi → {CACHE_FILE}", flush=True)
    return working


# ── ProxyPool sınıfı ──────────────────────────────────────────────────────────

class ProxyPool:
    """
    Thread-safe dönen proxy havuzu.

    - Başlangıçta geçerli önbellekten yükler (TTL < CACHE_TTL ise).
    - Havuz MIN_POOL'un altına düşerse veya önbellek yoksa otomatik yeniler.
    - mark_bad() ile kötü proxy'leri anında çıkarır.
    """

    def __init__(self, auto_refresh: bool = True, verbose: bool = True):
        self._pool:    list[dict] = []
        self._idx:     int        = 0
        self._lock:    Lock       = Lock()
        self._verbose: bool       = verbose
        self._load_cache()
        if auto_refresh and len(self._pool) < MIN_POOL:
            new = refresh_pool(verbose=verbose)
            with self._lock:
                self._pool = new
                self._idx  = 0

    # ── iç yardımcılar ──────────────────────────────────────────────────────

    def _load_cache(self):
        if not CACHE_FILE.exists():
            return
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            age  = time.time() - data.get("refreshed_at", 0)
            if age < CACHE_TTL:
                self._pool = data.get("proxies", [])
                if self._verbose and self._pool:
                    print(
                        f"   📦 Proxy önbelleği: {len(self._pool)} proxy "
                        f"({int(age // 60)}dk önce doğrulandı)",
                        flush=True,
                    )
        except Exception:
            pass

    @staticmethod
    def _make_dict(proxy_str: str) -> dict:
        url = f"http://{proxy_str}"
        return {"http": url, "https": url}

    # ── genel API ───────────────────────────────────────────────────────────

    def get(self) -> str | None:
        """Sıradaki proxy'nin `ip:port` dizesini döner."""
        with self._lock:
            if not self._pool:
                return None
            entry = self._pool[self._idx % len(self._pool)]
            self._idx += 1
            return entry["proxy"]

    def get_dict(self) -> dict | None:
        """requests için proxy sözlüğü döner: {"http": ..., "https": ...}"""
        p = self.get()
        return self._make_dict(p) if p else None

    def mark_bad(self, proxy_str: str):
        """Başarısız proxy'yi havuzdan çıkarır."""
        with self._lock:
            self._pool = [p for p in self._pool if p["proxy"] != proxy_str]

    def size(self) -> int:
        return len(self._pool)

    def needs_refresh(self) -> bool:
        if not CACHE_FILE.exists():
            return True
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return (time.time() - data.get("refreshed_at", 0)) >= CACHE_TTL
        except Exception:
            return True

    def __bool__(self) -> bool:
        return bool(self._pool)

    def __len__(self) -> int:
        return self.size()


# ── Yardımcı istek fonksiyonları ──────────────────────────────────────────────

def _build_headers(extra: dict | None = None) -> dict:
    h = {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT":             "1",
        "Connection":      "keep-alive",
    }
    if extra:
        h.update(extra)
    return h


def safe_get(
    url: str,
    pool: ProxyPool | None = None,
    retries: int = 3,
    timeout: int = 15,
    headers: dict | None = None,
    **kwargs,
) -> requests.Response | None:
    """
    Proxy rotasyonu + UA rotasyonu + retry ile GET isteği.

    Son denemede proxy olmadan doğrudan bağlantı dener (fallback).
    Tüm denemeler başarısız olursa None döner.
    """
    for attempt in range(retries):
        use_proxy  = bool(pool) and attempt < retries - 1
        proxies    = pool.get_dict() if use_proxy else None
        proxy_str  = proxies["http"].replace("http://", "") if proxies else None
        req_headers = _build_headers(headers)

        try:
            r = requests.get(
                url,
                proxies=proxies,
                timeout=timeout,
                headers=req_headers,
                **kwargs,
            )
            if r.status_code < 500:
                return r
        except Exception:
            if proxy_str and pool:
                pool.mark_bad(proxy_str)
        time.sleep(0.3 * attempt)  # artan bekleme

    return None


def safe_post(
    url: str,
    pool: ProxyPool | None = None,
    retries: int = 3,
    timeout: int = 15,
    headers: dict | None = None,
    **kwargs,
) -> requests.Response | None:
    """Proxy rotasyonu + retry ile POST isteği."""
    for attempt in range(retries):
        use_proxy  = bool(pool) and attempt < retries - 1
        proxies    = pool.get_dict() if use_proxy else None
        proxy_str  = proxies["http"].replace("http://", "") if proxies else None
        req_headers = _build_headers({"Content-Type": "application/json"})
        if headers:
            req_headers.update(headers)

        try:
            r = requests.post(
                url,
                proxies=proxies,
                timeout=timeout,
                headers=req_headers,
                **kwargs,
            )
            if r.status_code < 500:
                return r
        except Exception:
            if proxy_str and pool:
                pool.mark_bad(proxy_str)
        time.sleep(0.3 * attempt)

    return None


# ── CLI modu ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="CareerOps Proxy Manager")
    parser.add_argument("--clear", action="store_true", help="Önbelleği sil ve sıfırdan yenile")
    parser.add_argument("--test", metavar="URL", help="Belirli URL'yi proxy ile test et")
    args = parser.parse_args()

    if args.clear and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print("🗑️  Önbellek silindi")

    print("=" * 55)
    print("  CareerOps Proxy Manager")
    print("=" * 55)

    pool = ProxyPool(verbose=True)

    print(f"\n📊 Havuz: {pool.size()} proxy hazır")
    if pool:
        # İlk 5 proxy'yi göster
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        top5 = data["proxies"][:5]
        print("\nEn hızlı 5 proxy:")
        for i, p in enumerate(top5, 1):
            print(f"   {i}. {p['proxy']:>22}  {p['latency_ms']:>5}ms")

    if args.test:
        print(f"\n🔗 Test isteği: {args.test}")
        resp = safe_get(args.test, pool, retries=3, timeout=15)
        if resp:
            print(f"   ✅ HTTP {resp.status_code} — {len(resp.content)} byte")
        else:
            print("   ❌ İstek başarısız")
