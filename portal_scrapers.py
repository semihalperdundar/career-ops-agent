#!/usr/bin/env python3
"""
CareerOps — Portal Scrapers
============================
Greenhouse/Lever dışındaki iş portallarından ilan toplar:

  • Ashby       — API (POST non-user-facing endpoint)
  • Remotive    — Ücretsiz JSON API (uzaktan işler)
  • WeWorkRemotely — RSS/XML feed
  • Kariyer.net — HTML scraper (TR iş ilanları, proxy gerektirir)
  • Indeed TR   — HTML scraper (TR Indeed, proxy gerektirir)

Her fonksiyon aynı çıktı formatını döner:
    [{"title": str, "url": str, "company": str, "location": str, "source": str}]

Hata durumunda sessizce [] döner — ana akışı kesmez.

Bağımlılıklar:
    requests     — Anaconda'da mevcut
    beautifulsoup4 (bs4) — Anaconda'da mevcut (html.parser)
    xml.etree.ElementTree — Python stdlib
"""

import io
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import requests

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

# Windows UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ── Tip kısayolu ─────────────────────────────────────────────────────────────
# fetch_fn: (url, **kwargs) -> requests.Response | None
FetchFn = Callable[..., requests.Response | None]

_JOB = dict  # {"title", "url", "company", "location", "source"}


# ─────────────────────────────────────────────────────────────────────────────
# 1. ASHBY API
# ─────────────────────────────────────────────────────────────────────────────

# portals.yml'deki Ashby slug → şirket adı eşleşmesi
ASHBY_COMPANIES = [
    ("bitvavo",      "Bitvavo"),
    ("DeepL",        "DeepL"),
    ("AlephAlpha",   "Aleph Alpha"),
    ("cohere",       "Cohere"),
    ("langchain",    "LangChain"),
    ("scaleai",      "Scale AI"),
    ("mollie",       "Mollie"),
    ("photoroom",    "Photoroom"),
    ("perplexity",   "Perplexity"),
    ("synthesia",    "Synthesia"),
    ("amplemarket",  "Amplemarket"),
    ("snorkelai",    "Snorkel AI"),
    ("lynxanalytics","Lynx Analytics"),
]

_ASHBY_BOARD_URL = "https://jobs.ashbyhq.com/{slug}"
_ASHBY_JOB_URL   = "https://jobs.ashbyhq.com/{slug}/{job_id}"

# window.__appData embed data regex
_APPDATA_RE = re.compile(r"window\.__appData\s*=\s*(\{.*?\});", re.S)


def fetch_ashby(slug: str, company: str, fetch_fn: FetchFn) -> list[_JOB]:
    """
    Ashby job board sayfasındaki window.__appData'dan iş ilanlarını çeker.
    jobBoard.jobPostings listesini parse eder — public, login gerektirmez.
    """
    page_url = _ASHBY_BOARD_URL.format(slug=slug)
    r = fetch_fn(
        page_url,
        headers={
            "Accept":          "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://jobs.ashbyhq.com/",
        },
        timeout=20,
    )
    if not r or r.status_code != 200:
        return []

    m = _APPDATA_RE.search(r.text)
    if not m:
        return []

    try:
        data     = json.loads(m.group(1))
        job_board = data.get("jobBoard", {})
        postings  = job_board.get("jobPostings", [])
    except (json.JSONDecodeError, Exception):
        return []

    jobs: list[_JOB] = []
    for p in postings:
        if not p.get("isListed", True):
            continue
        title = p.get("title", "").strip()
        if not title:
            continue
        job_id      = p.get("id", "")
        ext_link    = p.get("externalLink") or ""
        url         = ext_link if ext_link else _ASHBY_JOB_URL.format(slug=slug, job_id=job_id)
        loc         = p.get("locationName") or p.get("locationExternalName", "")
        # İkincil konum listesinden de al
        if not loc and p.get("secondaryLocations"):
            loc = p["secondaryLocations"][0].get("locationName", "")
        jobs.append({
            "title":    title,
            "url":      url,
            "company":  company,
            "location": loc or "",
            "source":   f"ashby/{slug}",
            "posted_at": p.get("publishedAt") or p.get("updatedAt") or "",
        })
    return jobs


def fetch_all_ashby(fetch_fn: FetchFn, workers: int = 8) -> list[_JOB]:
    """Tüm ASHBY_COMPANIES'i paralel çeker."""
    all_jobs: list[_JOB] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(fetch_ashby, slug, name, fetch_fn): name
            for slug, name in ASHBY_COMPANIES
        }
        for fut in as_completed(futs):
            try:
                all_jobs.extend(fut.result())
            except Exception:
                pass
    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# 2. REMOTIVE — Ücretsiz remote iş API'si
# ─────────────────────────────────────────────────────────────────────────────

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
REMOTIVE_CATEGORIES = ["data", "machine-learning", "software-dev"]


def fetch_remotive(fetch_fn: FetchFn) -> list[_JOB]:
    """
    Remotive ücretsiz API'sinden data/ML işlerini çeker.
    https://remotive.com/api/remote-jobs?category=data&limit=100
    """
    jobs: list[_JOB] = []
    seen: set[str] = set()

    for cat in REMOTIVE_CATEGORIES:
        r = fetch_fn(
            REMOTIVE_URL,
            params={"category": cat, "limit": 100},
            headers={"Accept": "application/json"},
            timeout=20,
        )
        if not r or r.status_code != 200:
            continue
        try:
            for j in r.json().get("jobs", []):
                url = j.get("url", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                jobs.append({
                    "title":    j.get("title", ""),
                    "url":      url,
                    "company":  j.get("company_name", ""),
                    "location": j.get("candidate_required_location") or "Remote",
                    "source":   "remotive",
                    "posted_at": j.get("publication_date") or "",
                })
        except Exception:
            continue
        time.sleep(0.5)  # kibar davran

    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# 3. WE WORK REMOTELY — RSS/XML feed
# ─────────────────────────────────────────────────────────────────────────────

WWR_FEEDS = [
    # Ana feed — tüm remote ilanlar (büyük, sonradan filtreler)
    ("https://weworkremotely.com/remote-jobs.rss", "wwr/all"),
    # Backend feed — çalışan kategori URL'si
    (
        "https://weworkremotely.com/categories/"
        "remote-back-end-programming-jobs.rss",
        "wwr/backend",
    ),
]

# WWR RSS User-Agent: tarayıcı UA'sı 301'e yol açıyor; RSS reader UA kullan
_WWR_UA = "Mozilla/5.0 (compatible; RSS-Reader/1.0)"


def _parse_rss(xml_data, source_tag: str) -> list[_JOB]:
    """RSS XML (str veya bytes) iş listesine dönüştürür."""
    jobs: list[_JOB] = []
    try:
        # XML encoding bildirimi varsa bytes kullanmak zorunlu
        if isinstance(xml_data, str):
            xml_data = xml_data.encode("utf-8")
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return []

    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}

    for item in root.iter("item"):
        title_el   = item.find("title")
        link_el    = item.find("link")
        guid_el    = item.find("guid")
        region_el  = item.find("region")
        company_el = item.find("company")

        # ET element truth-value → child count; leaf nodes always False
        # → zorunlu olarak "is not None" kullan
        title = (title_el.text or "").strip() if title_el is not None else ""
        url = (
            (link_el.text or "").strip() if link_el is not None else ""
        ) or (
            (guid_el.text or "").strip() if guid_el is not None else ""
        )
        region  = (region_el.text  or "Remote").strip() if region_el  is not None else "Remote"
        company = (company_el.text or "").strip()        if company_el is not None else ""

        # WWR başlıklarında "Şirket: Başlık" formatı kullanır
        if ": " in title and not company:
            parts   = title.split(": ", 1)
            company = parts[0].strip()
            title   = parts[1].strip()

        if title and url:
            pub_el = item.find("pubDate")
            jobs.append({
                "title":    title,
                "url":      url,
                "company":  company,
                "location": region,
                "source":   source_tag,
                "posted_at": (pub_el.text or "").strip() if pub_el is not None else "",
            })
    return jobs


def fetch_weworkremotely(fetch_fn: FetchFn) -> list[_JOB]:
    """WeWorkRemotely RSS feed'lerini çeker."""
    jobs: list[_JOB] = []
    seen: set[str] = set()

    for feed_url, tag in WWR_FEEDS:
        r = fetch_fn(
            feed_url,
            headers={
                "User-Agent": _WWR_UA,
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
            timeout=20,
        )
        if not r or r.status_code != 200:
            continue
        for j in _parse_rss(r.text, tag):
            if j["url"] not in seen:
                seen.add(j["url"])
                jobs.append(j)
        time.sleep(0.3)

    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# 4. KARİYER.NET — Türkçe iş ilanları (HTML + JSON embedded)
# ─────────────────────────────────────────────────────────────────────────────

KARIYER_BASE = "https://www.kariyer.net"
KARIYER_SEARCH = f"{KARIYER_BASE}/is-ilanlari"

# Profil 1 (Data/AI/ML) ve Profil 2 (NLP/Araştırma) için Türkçe anahtar kelimeler
KARIYER_QUERIES = [
    # P1 — Junior Data/AI
    "veri bilimci",
    "data scientist",
    "makine ogrenmesi",
    "yapay zeka",
    "veri analisti",
    "veri muhendisi",
    "data engineer",
    "data analyst",
    # P2 — R&D / NLP / dil verisi (genişletildi)
    "arastirmaci",
    "nlp",
    "dil uzmani",
    "veri etiketleme",
    "dilbilimci",
    "dogal dil isleme",
    "yapay zeka egitmeni",
    "icerik uzmani",
    "olcme degerlendirme",
]


def _extract_kariyer_json(html: str) -> list[_JOB]:
    """
    Kariyer.net'in Next.js/React SPA'sı __NEXT_DATA__ veya
    window.__INITIAL_STATE__ olarak iş verilerini HTML'e gömer.
    """
    jobs: list[_JOB] = []

    # Yöntem 1: __NEXT_DATA__ JSON bloğu
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            # Kariyer.net Next.js yapısında iş listesi genellikle burada bulunur
            props = data.get("props", {}).get("pageProps", {})
            job_list = (
                props.get("jobs")
                or props.get("jobList")
                or props.get("searchResults", {}).get("jobs", [])
                or []
            )
            for j in job_list:
                title   = j.get("title") or j.get("jobTitle") or j.get("positionName", "")
                company = j.get("companyName") or j.get("company", {}).get("name", "")
                loc     = j.get("city") or j.get("location") or j.get("cityName", "")
                slug    = j.get("slug") or j.get("id") or j.get("jobId", "")
                url     = j.get("url") or (f"{KARIYER_BASE}/is-ilani/{slug}" if slug else "")
                if title and url:
                    jobs.append({
                        "title":    title,
                        "url":      url if url.startswith("http") else f"{KARIYER_BASE}{url}",
                        "company":  company,
                        "location": f"{loc}, Türkiye" if loc and "türkiye" not in loc.lower() else loc,
                        "source":   "kariyer.net",
                        "posted_at": (j.get("publishDate") or j.get("createdDate")
                                      or j.get("datePosted") or j.get("publishedDate") or ""),
                    })
            if jobs:
                return jobs
        except Exception:
            pass

    # Yöntem 2: JSON-LD yapılandırılmış verisi
    for m2 in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            ld = json.loads(m2.group(1))
            if isinstance(ld, list):
                items = ld
            elif isinstance(ld, dict):
                items = [ld] if ld.get("@type") == "JobPosting" else ld.get("@graph", [])
            else:
                continue
            for item in items:
                if item.get("@type") != "JobPosting":
                    continue
                title   = item.get("title", "")
                url     = item.get("url") or item.get("identifier", {}).get("url", "")
                company = item.get("hiringOrganization", {}).get("name", "")
                loc_obj = item.get("jobLocation", {})
                loc     = loc_obj.get("address", {}).get("addressLocality", "") if isinstance(loc_obj, dict) else ""
                if title and url:
                    jobs.append({
                        "title":    title,
                        "url":      url,
                        "company":  company,
                        "location": f"{loc}, Türkiye" if loc else "Türkiye",
                        "source":   "kariyer.net",
                        "posted_at": item.get("datePosted") or "",
                    })
            if jobs:
                return jobs
        except Exception:
            continue

    return []


def _extract_kariyer_html(html: str) -> list[_JOB]:
    """
    BeautifulSoup ile Kariyer.net HTML'inden iş ilanı kartlarını çıkarır.
    Birden fazla CSS seçici denemesi yapar (site tasarımı değiştiğinde).
    """
    if not _BS4_AVAILABLE:
        return []
    jobs: list[_JOB] = []
    soup = BeautifulSoup(html, "html.parser")

    # Olası kart seçicileri (Kariyer.net geçmiş + güncel tasarımları)
    card_selectors = [
        "li.job-post",
        "div.ilan-liste-item",
        "article.job-item",
        "div[class*='ilan']",
        "div[class*='job-card']",
        ".k-ilan-card",
        "a[class*='ilan-link']",
        "div[data-test='job-item']",
    ]

    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards:
        # Başlık
        title_el = card.select_one("h2, h3, .job-title, .ilan-title, [class*='title']")
        title    = title_el.get_text(strip=True) if title_el else ""

        # Şirket
        co_el  = card.select_one(".company, .firma-adi, [class*='company'], [class*='firma']")
        company = co_el.get_text(strip=True) if co_el else ""

        # Konum
        loc_el   = card.select_one(".location, .sehir, [class*='location'], [class*='city']")
        location = loc_el.get_text(strip=True) if loc_el else "Türkiye"

        # Link
        link_el = card.select_one("a[href]") or (card if card.name == "a" else None)
        href    = link_el.get("href", "") if link_el else ""
        url     = href if href.startswith("http") else f"{KARIYER_BASE}{href}"

        if title and url and url != KARIYER_BASE:
            jobs.append({
                "title":    title,
                "url":      url,
                "company":  company,
                "location": location if "türkiye" in location.lower() or location == "" else f"{location}, Türkiye",
                "source":   "kariyer.net",
            })

    return jobs


def fetch_kariyer(fetch_fn: FetchFn, max_queries: int = 6) -> list[_JOB]:
    """
    Kariyer.net'ten iş ilanları çeker.
    İlk max_queries sorguyu çalıştırır (rate limit için).
    """
    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    for query in KARIYER_QUERIES[:max_queries]:
        r = fetch_fn(
            KARIYER_SEARCH,
            params={"q": query},
            headers={
                "Accept":          "text/html,application/xhtml+xml",
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
                "Referer":         "https://www.kariyer.net/",
            },
            timeout=20,
        )
        if not r or r.status_code != 200:
            time.sleep(1)
            continue

        # Önce embed JSON'u dene, sonra HTML parse et
        jobs = _extract_kariyer_json(r.text) or _extract_kariyer_html(r.text)

        for j in jobs:
            url = j.get("url", "")
            if url and url not in seen:
                seen.add(url)
                all_jobs.append(j)

        time.sleep(1.5)  # rate limit için kibar bekleme

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# 5. INDEED TR — Türkiye Indeed (HTML scraper)
# ─────────────────────────────────────────────────────────────────────────────

INDEED_TR_BASE   = "https://tr.indeed.com"
INDEED_TR_SEARCH = f"{INDEED_TR_BASE}/is"

INDEED_TR_QUERIES = [
    "data scientist",
    "veri bilimci",
    "makine ogrenmesi",
    "data analyst",
    "veri analisti",
    "data engineer",
    "nlp researcher",
    "arastirmaci",
]


def _parse_indeed_jobs(html: str, base_url: str = INDEED_TR_BASE) -> list[_JOB]:
    """Indeed TR HTML'inden iş kartlarını çıkarır."""
    if not _BS4_AVAILABLE:
        return []
    jobs: list[_JOB] = []
    soup = BeautifulSoup(html, "html.parser")

    # Indeed iş kartı seçicileri (2024-2025 sürümleri)
    card_selectors = [
        "div.job_seen_beacon",
        "li[class*='result']",
        "div[class*='jobsearch-ResultsList'] > li",
        "div[data-testid='jobListing']",
        "article.resultContent",
    ]

    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards:
        # Başlık
        title_el = (
            card.select_one("h2.jobTitle, h2[class*='title'], a[class*='jobTitle'], span[title]")
            or card.select_one("h2, h3")
        )
        title = title_el.get_text(strip=True) if title_el else ""

        # Şirket
        co_el   = card.select_one("span.companyName, [data-testid='company-name'], [class*='company']")
        company = co_el.get_text(strip=True) if co_el else ""

        # Konum
        loc_el   = card.select_one("div.companyLocation, [data-testid='text-location'], [class*='location']")
        location = loc_el.get_text(strip=True) if loc_el else "Türkiye"

        # URL
        link_el = card.select_one("a[id^='job_'], a[class*='jcs-JobTitle'], a[href*='/rc/clk'], a[href*='/pagead/'], a[href^='/is/']")
        if not link_el:
            link_el = card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        url  = href if href.startswith("http") else f"{base_url}{href}"

        # İş ID'si ile canonical URL
        job_id = card.get("data-jk") or card.get("id", "").replace("job_", "")
        if job_id and not url.startswith("http"):
            url = f"{base_url}/rc/clk?jk={job_id}"

        if title and url and url != base_url:
            jobs.append({
                "title":    title,
                "url":      url,
                "company":  company,
                "location": location,
                "source":   "indeed.tr",
            })

    # Indeed JSON-LD alternatifi
    if not jobs:
        for m in re.finditer(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});', html, re.S):
            try:
                data = json.loads(m.group(1))
                results = data.get("metaData", {}).get("mosaicProviderJobCardsModel", {}).get("results", [])
                for j in results:
                    title   = j.get("displayTitle") or j.get("title", "")
                    company = j.get("company", "")
                    loc     = j.get("formattedLocation") or j.get("location", "Türkiye")
                    job_key = j.get("jobkey", "")
                    url     = f"{base_url}/rc/clk?jk={job_key}" if job_key else ""
                    if title and url:
                        jobs.append({
                            "title":    title,
                            "url":      url,
                            "company":  company,
                            "location": loc,
                            "source":   "indeed.tr",
                        })
                break
            except Exception:
                continue

    return jobs


def fetch_indeed_tr(fetch_fn: FetchFn, max_queries: int = 5) -> list[_JOB]:
    """Indeed TR'den iş ilanlarını çeker."""
    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    for query in INDEED_TR_QUERIES[:max_queries]:
        r = fetch_fn(
            INDEED_TR_SEARCH,
            params={"q": query, "l": "", "sort": "date", "fromage": "14"},
            headers={
                "Accept":          "text/html,application/xhtml+xml",
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
                "Referer":         "https://tr.indeed.com/",
                "Sec-Fetch-Mode":  "navigate",
                "Sec-Fetch-Site":  "same-origin",
            },
            timeout=20,
        )
        if not r or r.status_code != 200:
            time.sleep(2)
            continue

        for j in _parse_indeed_jobs(r.text):
            if j["url"] not in seen:
                seen.add(j["url"])
                all_jobs.append(j)

        time.sleep(2.0)  # Indeed rate limit için yeterli bekleme

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# 6. ACADEMICTRANSFER — Küresel akademik iş ilanları (requests)
# ─────────────────────────────────────────────────────────────────────────────

_AT_BASE   = "https://www.academictransfer.com"
_AT_SEARCH = f"{_AT_BASE}/en/jobs/"

AT_QUERIES = [
    "data scientist",
    "machine learning",
    "artificial intelligence",
    "data engineer",
    "nlp",
    "computational linguist",
    "research scientist",
    "language model",
    "natural language processing",
]


def fetch_academictransfer(fetch_fn: FetchFn, max_queries: int = 8) -> list[_JOB]:
    """
    AcademicTransfer'dan iş ilanlarını çeker (requests tabanlı).
    Coğrafi kısıtlama yok — tüm dünyadan pozisyonlar taranır.
    """
    if not _BS4_AVAILABLE:
        return []

    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    for query in AT_QUERIES[:max_queries]:
        r = fetch_fn(
            _AT_SEARCH,
            params={"q": query},
            headers={
                "Accept":          "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
                "Referer":         _AT_BASE,
            },
            timeout=20,
        )
        if not r or r.status_code != 200:
            time.sleep(1)
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("article"):
            title_el = card.select_one("h3")
            link_el  = card.select_one("a[href*='/en/jobs/']")
            # Location: span with gap-1 class (city name)
            loc_el   = card.select_one("span[class*='gap-1']")
            # Org: span with text-gray-700 class in right column
            org_el   = card.select_one("span[class*='text-gray-700']")

            title = title_el.get_text(strip=True) if title_el else ""
            href  = link_el.get("href", "")       if link_el  else ""
            url   = href if href.startswith("http") else f"{_AT_BASE}{href}"
            loc   = loc_el.get_text(strip=True)   if loc_el   else ""
            org   = org_el.get_text(strip=True)   if org_el   else ""

            if title and url and url not in seen:
                seen.add(url)
                all_jobs.append({
                    "title":    title,
                    "url":      url,
                    "company":  org,
                    "location": loc,
                    "source":   "academictransfer",
                })

        time.sleep(1.0)

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# 7. ANA TOPLAMA FONKSİYONU
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_extra(
    fetch_fn: FetchFn,
    enable_ashby:            bool = True,
    enable_remotive:         bool = True,
    enable_wwr:              bool = True,
    enable_kariyer:          bool = True,
    enable_indeed_tr:        bool = True,
    enable_academictransfer: bool = True,
    workers: int = 4,
    verbose: bool = True,
) -> list[_JOB]:
    """
    Tüm ek kaynaklardan iş ilanlarını toplar ve birleştirilmiş liste döner.
    İsteğe bağlı olarak belirli kaynaklar devre dışı bırakılabilir.
    """
    sources: dict[str, tuple[bool, Callable]] = {
        "Ashby":            (enable_ashby,            lambda: fetch_all_ashby(fetch_fn)),
        "Remotive":         (enable_remotive,         lambda: fetch_remotive(fetch_fn)),
        "WeWorkRemotely":   (enable_wwr,              lambda: fetch_weworkremotely(fetch_fn)),
        "AcademicTransfer": (enable_academictransfer, lambda: fetch_academictransfer(fetch_fn)),
        "Kariyer.net":      (enable_kariyer,          lambda: fetch_kariyer(fetch_fn)),
        "Indeed TR":        (enable_indeed_tr,        lambda: fetch_indeed_tr(fetch_fn)),
    }

    all_jobs: list[_JOB] = []
    total_sources = sum(1 for en, _ in sources.values() if en)

    if verbose:
        print(f"\n📡 Ek portal taraması başlıyor ({total_sources} kaynak)...", flush=True)

    # Ashby, Remotive ve WWR paralel çalışabilir (API tabanlı)
    # Kariyer.net ve Indeed TR sıralı çalışır (HTML, rate-limit hassas)
    api_futures = {}
    html_sources = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for name, (enabled, fn) in sources.items():
            if not enabled:
                continue
            if name in ("Kariyer.net", "Indeed TR", "AcademicTransfer"):
                html_sources[name] = fn
            else:
                api_futures[ex.submit(fn)] = name

        for fut, name in api_futures.items():
            try:
                results = fut.result(timeout=60)
                if verbose:
                    print(f"   ✓ {name}: {len(results)} ilan", flush=True)
                all_jobs.extend(results)
            except Exception as e:
                if verbose:
                    print(f"   ✗ {name}: hata ({e})", flush=True)

    for name, fn in html_sources.items():
        try:
            results = fn()
            if verbose:
                print(f"   ✓ {name}: {len(results)} ilan", flush=True)
            all_jobs.extend(results)
        except Exception as e:
            if verbose:
                print(f"   ✗ {name}: hata ({e})", flush=True)

    if verbose:
        print(f"   📦 Ek kaynaklardan toplam: {len(all_jobs)} ilan", flush=True)

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# CLI test modu
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CareerOps Portal Scrapers — test")
    parser.add_argument("--source", choices=["ashby", "remotive", "wwr", "kariyer", "indeed", "all"],
                        default="all", help="Test edilecek kaynak")
    parser.add_argument("--no-proxy", action="store_true", help="Proxy kullanma (doğrudan bağlantı)")
    args = parser.parse_args()

    # Proxy olmadan basit fetch fonksiyonu
    import random as _random
    _UA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    ]

    def _direct_fetch(url, method="GET", **kwargs):
        try:
            kwargs.setdefault("headers", {}).update({"User-Agent": _UA[0]})
            kwargs.setdefault("timeout", 20)
            return requests.request(method, url, **kwargs)
        except Exception:
            return None

    fetch = _direct_fetch

    if not args.no_proxy:
        try:
            from proxy_manager import ProxyPool, safe_get
            pool = ProxyPool(verbose=True)

            def fetch(url, **kwargs):
                return safe_get(url, pool, **kwargs)
            print(f"🔐 Proxy havuzu: {pool.size()} proxy\n")
        except ImportError:
            print("⚠️  proxy_manager.py bulunamadı — doğrudan bağlantı kullanılıyor\n")

    src = args.source
    if src in ("ashby", "all"):
        print("\n--- Ashby ---")
        jobs = fetch_all_ashby(fetch)
        for j in jobs[:5]:
            print(f"  {j['company']:20} {j['title'][:50]:50} {j['location']}")
        print(f"  Toplam: {len(jobs)}")

    if src in ("remotive", "all"):
        print("\n--- Remotive ---")
        jobs = fetch_remotive(fetch)
        for j in jobs[:5]:
            print(f"  {j['company']:20} {j['title'][:50]:50} {j['location']}")
        print(f"  Toplam: {len(jobs)}")

    if src in ("wwr", "all"):
        print("\n--- WeWorkRemotely ---")
        jobs = fetch_weworkremotely(fetch)
        for j in jobs[:5]:
            print(f"  {j['company']:20} {j['title'][:50]:50} {j['location']}")
        print(f"  Toplam: {len(jobs)}")

    if src in ("kariyer", "all"):
        print("\n--- Kariyer.net ---")
        jobs = fetch_kariyer(fetch, max_queries=3)
        for j in jobs[:5]:
            print(f"  {j['company']:20} {j['title'][:50]:50} {j['location']}")
        print(f"  Toplam: {len(jobs)}")

    if src in ("indeed", "all"):
        print("\n--- Indeed TR ---")
        jobs = fetch_indeed_tr(fetch, max_queries=3)
        for j in jobs[:5]:
            print(f"  {j['company']:20} {j['title'][:50]:50} {j['location']}")
        print(f"  Toplam: {len(jobs)}")
