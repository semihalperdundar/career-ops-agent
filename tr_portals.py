#!/usr/bin/env python3
"""
CareerOps — Türkiye Portal Ekosistemi (T1 yurt içi pazar)
==========================================================
Kariyer.net ve Indeed TR portal_scrapers.py'de; bu modül TR kapsamını
kalan büyük portallara genişletir.

Uygulanan (Next.js __NEXT_DATA__ üzerinden, yapı canlı doğrulandı):
    techcareer.net   props.pageProps.initialJobList.jobListItems[]
    isinolsun.com    props.pageProps.jobs[]

Erişilebilir ama HTML seçici gerektiren (PORTAL_SPECS'te tanımlı):
    yenibiris.com, secretcv.com, eleman.net, youthall.com, toptalent.co

ÖNEMLİ — CI notu: bu portallar Türkiye IP'sinden doğrudan 200 döner.
GitHub Actions runner'ı ABD/AB datacenter IP'si kullandığı için TR exit
şarttır; fetch_fn premium proxy üzerinden gelmelidir (net_policy geo=tr).

Her fonksiyon ortak formatı döner:
    [{"title","url","company","location","source","posted_at"}]
Hata durumunda sessizce [] döner — ana akışı kesmez.
"""

from __future__ import annotations

import json
import re
import time

_JOB = dict

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)

_TR_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}


def _next_data(html: str) -> dict:
    """__NEXT_DATA__ JSON bloğunu çıkarır; yoksa boş sözlük."""
    if not html:
        return {}
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return {}


def _page_props(html: str) -> dict:
    return (_next_data(html).get("props", {}) or {}).get("pageProps", {}) or {}


# ─────────────────────────────────────────────────────────────────────────────
# 1. TECHCAREER.NET — teknoloji odaklı, P1+P2 için yüksek sinyal
# ─────────────────────────────────────────────────────────────────────────────

TECHCAREER_BASE = "https://www.techcareer.net"
TECHCAREER_SEARCH = f"{TECHCAREER_BASE}/jobs"


def fetch_techcareer(fetch_fn, max_pages: int = 3, verbose: bool = False) -> list[_JOB]:
    """techcareer.net iş listesini Next.js veri bloğundan çeker."""
    jobs: list[_JOB] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        r = fetch_fn(
            TECHCAREER_SEARCH,
            params={"page": page} if page > 1 else None,
            headers={**_TR_HEADERS, "Referer": TECHCAREER_BASE},
            timeout=25,
        )
        if not r or getattr(r, "status_code", 0) != 200:
            break

        items = ((_page_props(r.text).get("initialJobList") or {})
                 .get("jobListItems") or [])
        if not items:
            break

        for it in items:
            slug = it.get("slug") or ""
            url = it.get("applyLink") or (
                f"{TECHCAREER_BASE}/jobs/{slug}" if slug else ""
            )
            title = (it.get("title") or it.get("jobTitle") or "").strip()
            if not title or not url or url in seen:
                continue
            seen.add(url)

            # workPlaces bazen liste, bazen string
            wp = it.get("workPlaces") or it.get("location") or ""
            if isinstance(wp, (list, tuple)):
                wp = ", ".join(str(x.get("name", x) if isinstance(x, dict) else x)
                               for x in wp)
            loc = str(wp).strip() or "Türkiye"
            if "türkiye" not in loc.lower() and "turkey" not in loc.lower():
                loc = f"{loc}, Türkiye"

            company = it.get("hiddenCompanyInfo") or ""
            if isinstance(company, dict):
                company = company.get("name", "")
            if it.get("isCompanyHidden"):
                company = company or "Gizli Şirket"

            jobs.append({
                "title": title,
                "url": url,
                "company": str(company) or "techcareer.net",
                "location": loc,
                "source": "techcareer",
                "posted_at": it.get("publishDate") or it.get("createdDate") or "",
            })

        time.sleep(1.2)

    if verbose:
        print(f"   ✓ techcareer.net: {len(jobs)} ilan", flush=True)
    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# 2. ISINOLSUN.COM — geniş hacim, mavi/beyaz yaka karışık
# ─────────────────────────────────────────────────────────────────────────────

ISINOLSUN_BASE = "https://www.isinolsun.com"
ISINOLSUN_SEARCH = f"{ISINOLSUN_BASE}/is-ilanlari"

# Portal geneli çok geniş; anahtar kelimeyle daraltmak hem alaka hem bant
# genişliği açısından zorunlu.
ISINOLSUN_QUERIES = [
    "veri analisti", "veri bilimci", "yapay zeka", "yazilim gelistirici",
    "raporlama uzmani", "is analisti",
]


def fetch_isinolsun(fetch_fn, max_queries: int = 4, verbose: bool = False) -> list[_JOB]:
    """isinolsun.com iş listesini Next.js veri bloğundan çeker."""
    jobs: list[_JOB] = []
    seen: set[str] = set()

    for q in ISINOLSUN_QUERIES[:max_queries]:
        r = fetch_fn(
            ISINOLSUN_SEARCH,
            params={"q": q},
            headers={**_TR_HEADERS, "Referer": ISINOLSUN_BASE},
            timeout=25,
        )
        if not r or getattr(r, "status_code", 0) != 200:
            continue

        for it in (_page_props(r.text).get("jobs") or []):
            url = it.get("shareUrl") or ""
            title = (it.get("positionName") or "").strip()
            if not title or not url or url in seen:
                continue
            seen.add(url)

            city = it.get("cityName") or ""
            town = it.get("townName") or ""
            loc = ", ".join(x for x in (town, city) if x) or "Türkiye"
            if "türkiye" not in loc.lower():
                loc = f"{loc}, Türkiye"

            # durationDay = ilanın kaç gündür yayında olduğu → tazelik metni
            days = it.get("durationDay")
            posted = f"{days} gün önce" if isinstance(days, int) else (
                it.get("durationDayText") or "")

            jobs.append({
                "title": title,
                "url": url,
                "company": (it.get("companyName") or "").strip(),
                "location": loc,
                "source": "isinolsun",
                "posted_at": posted,
            })

        time.sleep(1.5)

    if verbose:
        print(f"   ✓ isinolsun.com: {len(jobs)} ilan", flush=True)
    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# Portal kayıt defteri — genişletme yol haritası
# ─────────────────────────────────────────────────────────────────────────────
# status: "live"    → uygulandı ve çalışıyor
#         "planned" → erişilebilir (HTTP 200 doğrulandı), ayrıştırıcı bekliyor
#         "blocked" → koruma katmanı nedeniyle premium TR exit + curl_cffi şart

PORTAL_SPECS: dict[str, dict] = {
    "kariyer.net": {
        "status": "blocked", "impl": "portal_scrapers.fetch_kariyer",
        "protection": "PerimeterX", "needs": "TR exit + chrome110 + çerez ısıtması",
        "extract": "__NEXT_DATA__ / JSON-LD / HTML fallback",
    },
    "indeed.tr": {
        "status": "blocked", "impl": "portal_scrapers.fetch_indeed_tr",
        "protection": "Cloudflare", "needs": "TR exit + curl_cffi",
        "extract": "mosaic-provider JSON / HTML kart",
    },
    "techcareer.net": {
        "status": "live", "impl": "tr_portals.fetch_techcareer",
        "protection": None, "needs": "TR exit (CI'da)",
        "extract": "__NEXT_DATA__ props.pageProps.initialJobList.jobListItems",
    },
    "isinolsun.com": {
        "status": "live", "impl": "tr_portals.fetch_isinolsun",
        "protection": None, "needs": "TR exit (CI'da)",
        "extract": "__NEXT_DATA__ props.pageProps.jobs",
    },
    "yenibiris.com": {
        "status": "planned", "impl": None, "protection": None,
        "needs": "TR exit", "extract": "HTML kart seçici (div.list-items)",
    },
    "secretcv.com": {
        "status": "planned", "impl": None, "protection": None,
        "needs": "TR exit", "extract": "HTML kart seçici",
    },
    "eleman.net": {
        "status": "planned", "impl": None, "protection": None,
        "needs": "TR exit", "extract": "HTML kart seçici",
    },
    "youthall.com": {
        "status": "planned", "impl": None, "protection": None,
        "needs": "TR exit", "extract": "HTML kart / XHR API",
    },
    "toptalent.co": {
        "status": "planned", "impl": None, "protection": None,
        "needs": "TR exit", "extract": "HTML kart seçici",
    },
    "linkedin.com/TR": {
        "status": "live", "impl": "playwright_scrapers.fetch_linkedin",
        "protection": None, "needs": "yok (guest API)",
        "extract": "location=Turkey/Istanbul/Ankara/Izmir sorguları",
    },
}


def fetch_all_tr(fetch_fn, enable_techcareer: bool = True,
                 enable_isinolsun: bool = True, verbose: bool = True) -> list[_JOB]:
    """Uygulanmış TR portallarını sırayla tarar; hatada diğerlerine devam eder."""
    out: list[_JOB] = []
    steps = (
        ("techcareer.net", enable_techcareer, lambda: fetch_techcareer(fetch_fn, verbose=False)),
        ("isinolsun.com", enable_isinolsun, lambda: fetch_isinolsun(fetch_fn, verbose=False)),
    )
    if verbose:
        active = sum(1 for _, en, _ in steps if en)
        print(f"\n🇹🇷 TR portal taraması ({active} kaynak)...", flush=True)

    for name, enabled, fn in steps:
        if not enabled:
            continue
        try:
            res = fn()
            out.extend(res)
            if verbose:
                print(f"   ✓ {name}: {len(res)} ilan", flush=True)
        except Exception as exc:
            if verbose:
                print(f"   ✗ {name}: hata ({type(exc).__name__}: {exc})", flush=True)

    if verbose:
        print(f"   📦 TR portallarından toplam: {len(out)} ilan", flush=True)
    return out


if __name__ == "__main__":
    import requests

    def _fetch(url, **kw):
        kw.pop("timeout", None)
        try:
            return requests.get(url, timeout=25, **kw)
        except Exception:
            return None

    jobs = fetch_all_tr(_fetch)
    for j in jobs[:15]:
        print(f"  {j['source']:12s} {j['title'][:46]:48s} {j['location'][:26]:28s} "
              f"{j['posted_at']}")
    print(f"\ntoplam {len(jobs)}")
    print("\nkayit defteri:")
    for name, spec in PORTAL_SPECS.items():
        print(f"  {name:20s} {spec['status']:8s} {spec.get('protection') or '-':12s} "
              f"{spec['needs']}")
