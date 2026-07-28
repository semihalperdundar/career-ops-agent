#!/usr/bin/env python3
"""
CareerOps — Playwright Scrapers
================================
JavaScript renderlamasına ihtiyaç duyan sitelerden iş ilanı çeker:

  • LinkedIn    — jobs-guest API (requests tabanlı, Playwright gerekmez)
  • Kariyer.net — React SPA (Playwright)
  • Indeed TR   — JS rendered (Playwright)
  • Glassdoor   — Cloudflare korumalı (Playwright + stealth)

Her fonksiyon şu çıktı formatını döner:
    [{"title": str, "url": str, "company": str, "location": str, "source": str}]

Hata durumunda sessizce [] döner — ana akışı kesmez.

Bağımlılıklar:
    playwright (sync_api)  — python -m playwright install chromium
    beautifulsoup4 (bs4)   — Anaconda'da mevcut
    requests               — Anaconda'da mevcut
"""

import io
import os
import random
import sys
import time
from urllib.parse import urlparse

import requests

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

try:
    from playwright_stealth import Stealth   # v2 API
    _STEALTH = Stealth()
except ImportError:
    _STEALTH = None

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

# Windows UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_JOB = dict  # {"title", "url", "company", "location", "source"}

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. LINKEDIN — jobs-guest API (requests tabanlı, Playwright gerekmez)
# ─────────────────────────────────────────────────────────────────────────────

LINKEDIN_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

# (anahtar kelimeler, konum) çiftleri — boş konum = global
LINKEDIN_QUERIES = [
    ("data scientist",           "Netherlands"),
    ("data engineer",            "Netherlands"),
    ("analytics engineer",       "Netherlands"),
    ("machine learning engineer","Netherlands"),
    ("data analyst",             "Netherlands"),
    ("nlp researcher",           ""),
    ("ai engineer",              ""),
    ("applied scientist",        "Europe"),
    ("data scientist",           "Turkey"),
    ("veri bilimci",             "Türkiye"),
    ("research scientist",       "Europe"),
]

_LI_HEADERS = {
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.linkedin.com/jobs/",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "same-origin",
}


def _parse_linkedin_html(html: str) -> list[_JOB]:
    """LinkedIn jobs-guest HTML iş kartlarını çıkarır."""
    if not _BS4_AVAILABLE:
        return []
    jobs: list[_JOB] = []
    soup = BeautifulSoup(html, "html.parser")

    for card in soup.select("li"):
        title_el   = card.select_one(
            "h3.base-search-card__title, h3[class*='title']"
        )
        company_el = card.select_one(
            "h4.base-search-card__subtitle, h4[class*='subtitle']"
        )
        loc_el     = card.select_one(
            "span.job-search-card__location, span[class*='location']"
        )
        link_el    = card.select_one(
            "a.base-card__full-link, a[class*='card__full-link'], a[href*='/jobs/view/']"
        )

        title   = title_el.get_text(strip=True)   if title_el   else ""
        company = company_el.get_text(strip=True)  if company_el else ""
        loc     = loc_el.get_text(strip=True)      if loc_el     else ""
        href    = link_el.get("href", "")          if link_el    else ""

        # Tracking parametrelerini temizle
        if href and "linkedin.com/jobs/view/" in href:
            href = href.split("?")[0]

        if title and href:
            jobs.append({
                "title":    title,
                "url":      href,
                "company":  company,
                "location": loc,
                "source":   "linkedin",
            })
    return jobs


def fetch_linkedin(max_per_query: int = 25, verbose: bool = False) -> list[_JOB]:
    """
    LinkedIn jobs-guest API'sinden iş ilanları çeker.
    Login gerektirmez, Playwright gerekmez.
    """
    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    for keywords, location in LINKEDIN_QUERIES:
        params: dict = {
            "keywords": keywords,
            "start":    0,
            "count":    max_per_query,
            "f_TPR":    "r2592000",  # son 30 gün
        }
        if location:
            params["location"] = location

        try:
            ua = random.choice(_USER_AGENTS)
            r  = requests.get(
                LINKEDIN_API,
                params=params,
                headers={**_LI_HEADERS, "User-Agent": ua},
                timeout=20,
            )
            if r.status_code == 200 and r.text.strip():
                for job in _parse_linkedin_html(r.text):
                    if job["url"] not in seen:
                        seen.add(job["url"])
                        all_jobs.append(job)
            elif r.status_code == 429:
                if verbose:
                    print("   ⚠️  LinkedIn rate limit — bekleniyor...", flush=True)
                time.sleep(15)
        except Exception:
            pass

        time.sleep(random.uniform(2.0, 4.0))

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# Playwright yardımcıları
# ─────────────────────────────────────────────────────────────────────────────

# PREMIUM_PROXY_URL formatı: http://user:pass@gate.saglayici.com:7777
# Kariyer.net/PerimeterX TR exit ister; Indeed CF datacenter IP'yi bloklar.
def _proxy_cfg(country: str | None = None) -> dict | None:
    raw = os.environ.get("PREMIUM_PROXY_URL", "").strip()
    if not raw:
        return None
    u    = urlparse(raw)
    user = u.username or ""
    # Residential gateway'lerin çoğu geo'yu username son eki olarak alır
    if country and user:
        user = f"{user}-country-{country.lower()}"
    return {
        "server":   f"{u.scheme}://{u.hostname}:{u.port}",
        "username": user,
        "password": u.password or "",
    }


# ── Bant genişliği koruması ───────────────────────────────────────────────────
# Proxy trafiği GB başına ücretlendirilir; görsel/font/CSS ilan metnine sıfır
# katkı yapar. Script/XHR/fetch ASLA bloklanmaz — WAF challenge'ları ve SPA
# veri çağrıları onlara bağlı.
_BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}

# Analitik/reklam host'ları: hem bant genişliği hem fingerprint gürültüsü
_BLOCKED_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "facebook.com/tr", "hotjar.com", "segment.io",
    "segment.com", "mixpanel.com", "amplitude.com", "sentry.io",
    "newrelic.com", "optimizely.com", "adservice.google", "adsystem.com",
    "criteo.", "taboola.", "outbrain.", "bat.bing.com", "clarity.ms",
)

# Ortam değişkeniyle kapatılabilir (bir site CSS'siz render edemezse)
_BLOCK_ASSETS = os.environ.get("PW_BLOCK_ASSETS", "1") not in ("0", "false", "False")


def _route_filter(route, request):
    """Ağır asset'leri ve tracker'ları iptal eder; kalanını geçirir."""
    try:
        if request.resource_type in _BLOCKED_RESOURCES:
            return route.abort()
        url = request.url
        if any(h in url for h in _BLOCKED_HOSTS):
            return route.abort()
        return route.continue_()
    except Exception:
        try:
            route.continue_()
        except Exception:
            pass


# Geo başına locale/timezone — WAF fingerprint'i IP ile tutarlı olmalı
_GEO = {
    "tr": ("tr-TR", "Europe/Istanbul", "tr-TR,tr;q=0.9,en;q=0.8"),
    "nl": ("nl-NL", "Europe/Amsterdam", "nl-NL,nl;q=0.9,en;q=0.8"),
    "gb": ("en-GB", "Europe/London", "en-GB,en;q=0.9"),
}
_GEO_DEFAULT = ("en-US", "Europe/Amsterdam", "en-US,en;q=0.9")


def _new_browser(playwright, headless: bool = True, country: str | None = None):
    """Stealth + opsiyonel residential proxy ile Chromium; (browser, ctx) döner."""
    ua    = random.choice(_USER_AGENTS)
    proxy = _proxy_cfg(country)
    locale, tz, accept_lang = _GEO.get((country or "").lower(), _GEO_DEFAULT)

    browser = playwright.chromium.launch(
        headless=headless,
        proxy=proxy,                    # None ise doğrudan bağlantı
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    ctx = browser.new_context(
        user_agent=ua,
        viewport={
            "width":  1280 + random.randint(-80, 80),
            "height": 800  + random.randint(-50, 50),
        },
        locale=locale,
        timezone_id=tz,
        java_script_enabled=True,
        bypass_csp=True,
        ignore_https_errors=bool(proxy),   # MITM yapan gateway'ler için
        extra_http_headers={
            "Accept-Language": accept_lang,
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    if _STEALTH:
        _STEALTH.apply_stealth_sync(ctx)   # webdriver/chrome.runtime/WebGL patch
    else:                                  # fallback: manuel patch
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        )

    # Context seviyesinde route — bu ctx'ten açılan her sayfa için geçerli
    if _BLOCK_ASSETS:
        ctx.route("**/*", _route_filter)

    return browser, ctx


# ─────────────────────────────────────────────────────────────────────────────
# 2. KARİYER.NET — Playwright (React SPA)
# ─────────────────────────────────────────────────────────────────────────────

KARIYER_QUERIES_PW = [
    "data scientist",
    "veri bilimci",
    "data engineer",
    "veri analisti",
    "makine öğrenmesi",
    "yapay zeka",
    "nlp",
    "araştırmacı",
]

_KARIYER_BASE = "https://www.kariyer.net"
_KARIYER_SEARCH = f"{_KARIYER_BASE}/is-ilanlari"


def _extract_kariyer_pw(page) -> list[_JOB]:
    """Playwright page'den Kariyer.net iş kartlarını çıkarır."""
    if not _BS4_AVAILABLE:
        return []
    soup  = BeautifulSoup(page.content(), "html.parser")
    jobs: list[_JOB] = []

    card_selectors = [
        "a.k-ilan-card",
        "div.ilan-liste-item",
        "li.job-post",
        "div[class*='JobCard']",
        "div[class*='job-card']",
        "a[href*='/is-ilani/']",
    ]
    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards:
        title_el = card.select_one(
            "h2, h3, [class*='title'], [class*='Title'], [class*='pozisyon']"
        )
        co_el  = card.select_one(
            "[class*='company'], [class*='Company'], [class*='firma'], [class*='Firma']"
        )
        loc_el = card.select_one(
            "[class*='location'], [class*='Location'], [class*='sehir'], [class*='City']"
        )

        title   = title_el.get_text(strip=True) if title_el else ""
        company = co_el.get_text(strip=True)    if co_el    else ""
        loc     = loc_el.get_text(strip=True)   if loc_el   else "Türkiye"

        href = card.get("href", "") if card.name == "a" else ""
        if not href:
            link_el = card.select_one("a[href*='/is-ilani/']")
            href    = link_el.get("href", "") if link_el else ""
        url = href if href.startswith("http") else f"{_KARIYER_BASE}{href}"

        if title and url and "kariyer.net" in url:
            jobs.append({
                "title":    title,
                "url":      url,
                "company":  company,
                "location": loc,
                "source":   "kariyer.net",
            })
    return jobs


def fetch_kariyer_pw(max_queries: int = 5, verbose: bool = False) -> list[_JOB]:
    """Playwright ile Kariyer.net React SPA'dan iş ilanları çeker."""
    if not _PW_AVAILABLE:
        if verbose:
            print("   ⚠️  Playwright bulunamadı — Kariyer.net atlandı", flush=True)
        return []

    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    try:
        with sync_playwright() as pw:
            browser, ctx = _new_browser(pw, headless=True, country="tr")
            page = ctx.new_page()

            for query in KARIYER_QUERIES_PW[:max_queries]:
                try:
                    page.goto(
                        _KARIYER_SEARCH,
                        wait_until="networkidle",
                        timeout=30000,
                    )
                    # Arama kutusuna yaz ve gönder
                    try:
                        page.fill("input[name='q'], input[placeholder*='Ara'], input[type='search']", query)
                        page.keyboard.press("Enter")
                        page.wait_for_selector(
                            "a[href*='/is-ilani/'], div[class*='job'], div[class*='ilan']",
                            timeout=10000,
                        )
                    except PlaywrightTimeout:
                        # Arama kutusuna yazamadıysak URL parametresiyle dene
                        page.goto(
                            f"{_KARIYER_SEARCH}?q={requests.utils.quote(query)}",
                            wait_until="networkidle",
                            timeout=25000,
                        )

                    for job in _extract_kariyer_pw(page):
                        if job["url"] not in seen:
                            seen.add(job["url"])
                            all_jobs.append(job)
                except Exception:
                    continue

                time.sleep(random.uniform(2.0, 3.5))

            browser.close()
    except Exception as e:
        if verbose:
            print(f"   ⚠️  Kariyer.net Playwright hatası: {e}", flush=True)

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# 3. INDEED TR — Playwright (JS rendered)
# ─────────────────────────────────────────────────────────────────────────────

INDEED_TR_QUERIES_PW = [
    "data scientist",
    "veri bilimci",
    "data engineer",
    "data analyst",
    "veri analisti",
    "makine öğrenmesi",
    "nlp",
]

_INDEED_TR_BASE   = "https://tr.indeed.com"
_INDEED_TR_SEARCH = f"{_INDEED_TR_BASE}/is"


def _extract_indeed_pw(page) -> list[_JOB]:
    """Playwright page'den Indeed TR iş kartlarını çıkarır."""
    if not _BS4_AVAILABLE:
        return []
    soup  = BeautifulSoup(page.content(), "html.parser")
    jobs: list[_JOB] = []

    card_selectors = [
        "div.job_seen_beacon",
        "li[class*='result']",
        "div[data-testid='jobListing']",
        "article.resultContent",
    ]
    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards:
        title_el = (
            card.select_one("h2.jobTitle span[title], h2.jobTitle a span, h2[class*='title'] span")
            or card.select_one("h2, h3")
        )
        co_el  = card.select_one("span.companyName, [data-testid='company-name']")
        loc_el = card.select_one("div.companyLocation, [data-testid='text-location']")

        title   = title_el.get_text(strip=True) if title_el else ""
        company = co_el.get_text(strip=True)    if co_el    else ""
        loc     = loc_el.get_text(strip=True)   if loc_el   else "Türkiye"

        job_key = card.get("data-jk", "")
        link_el = card.select_one("a[id^='job_'], a[class*='jcs-JobTitle']")
        href    = link_el.get("href", "") if link_el else ""

        if job_key:
            url = f"{_INDEED_TR_BASE}/rc/clk?jk={job_key}"
        elif href:
            url = href if href.startswith("http") else f"{_INDEED_TR_BASE}{href}"
        else:
            continue

        if title:
            jobs.append({
                "title":    title,
                "url":      url,
                "company":  company,
                "location": loc,
                "source":   "indeed.tr",
            })
    return jobs


def fetch_indeed_tr_pw(max_queries: int = 5, verbose: bool = False) -> list[_JOB]:
    """Playwright ile Indeed TR'den iş ilanları çeker."""
    if not _PW_AVAILABLE:
        if verbose:
            print("   ⚠️  Playwright bulunamadı — Indeed TR atlandı", flush=True)
        return []

    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    try:
        with sync_playwright() as pw:
            browser, ctx = _new_browser(pw, headless=True, country="tr")
            page = ctx.new_page()
            page.set_extra_http_headers({"Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"})

            for query in INDEED_TR_QUERIES_PW[:max_queries]:
                url = (
                    f"{_INDEED_TR_SEARCH}"
                    f"?q={requests.utils.quote(query)}&sort=date&fromage=30"
                )
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_selector(
                            "div.job_seen_beacon, li[class*='result'], article.resultContent",
                            timeout=10000,
                        )
                    except PlaywrightTimeout:
                        pass

                    for job in _extract_indeed_pw(page):
                        if job["url"] not in seen:
                            seen.add(job["url"])
                            all_jobs.append(job)
                except Exception:
                    continue

                time.sleep(random.uniform(2.5, 4.0))

            browser.close()
    except Exception as e:
        if verbose:
            print(f"   ⚠️  Indeed TR Playwright hatası: {e}", flush=True)

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# 4. GLASSDOOR — Playwright (slug URL ile — çalışıyor!)
# ─────────────────────────────────────────────────────────────────────────────
# Glassdoor'da lokasyon filtresi için slug URL kullanmak zorunlu.
# Slug format: /Job/{loc}-{kw}-jobs-SRCH_IL.0,{loc_len}_IN{country_id}_KO{kw_start},{kw_end}.htm
# IN178 = Netherlands, IN197 = Turkey
# KO offset: loc_len+1 = kw_start, kw_start+len(kw_slug) = kw_end

_GD_BASE = "https://www.glassdoor.com"

GLASSDOOR_SLUG_URLS = [
    # Netherlands (IN178, loc_len=11, kw_start=12)
    f"{_GD_BASE}/Job/netherlands-data-scientist-jobs-SRCH_IL.0,11_IN178_KO12,26.htm",
    f"{_GD_BASE}/Job/netherlands-data-engineer-jobs-SRCH_IL.0,11_IN178_KO12,25.htm",
    f"{_GD_BASE}/Job/netherlands-analytics-engineer-jobs-SRCH_IL.0,11_IN178_KO12,30.htm",
    f"{_GD_BASE}/Job/netherlands-machine-learning-engineer-jobs-SRCH_IL.0,11_IN178_KO12,37.htm",
    f"{_GD_BASE}/Job/netherlands-data-analyst-jobs-SRCH_IL.0,11_IN178_KO12,24.htm",
    f"{_GD_BASE}/Job/netherlands-ai-engineer-jobs-SRCH_IL.0,11_IN178_KO12,23.htm",
    f"{_GD_BASE}/Job/netherlands-nlp-researcher-jobs-SRCH_IL.0,11_IN178_KO12,26.htm",
    f"{_GD_BASE}/Job/netherlands-research-scientist-jobs-SRCH_IL.0,11_IN178_KO12,30.htm",
    # Turkey (IN197, loc_len=6, kw_start=7)
    f"{_GD_BASE}/Job/turkey-data-scientist-jobs-SRCH_IL.0,6_IN197_KO7,21.htm",
    f"{_GD_BASE}/Job/turkey-data-engineer-jobs-SRCH_IL.0,6_IN197_KO7,20.htm",
    f"{_GD_BASE}/Job/turkey-data-analyst-jobs-SRCH_IL.0,6_IN197_KO7,19.htm",
]

import re as _re
_GD_RATING_RE = _re.compile(r"\d+\.\d+$")


def _extract_glassdoor_pw(page) -> list[_JOB]:
    """Playwright page'den Glassdoor iş kartlarını çıkarır."""
    if not _BS4_AVAILABLE:
        return []
    soup  = BeautifulSoup(page.content(), "html.parser")
    jobs: list[_JOB] = []

    cards = soup.select("li[data-test='jobListing']") or soup.select("li.react-job-listing")

    for card in cards:
        title_el = card.select_one("a[data-test='job-title']")
        co_el    = card.select_one("span[class*='EmployerProfile_compactEmployerName']")
        loc_el   = card.select_one("div[data-test='emp-location']")
        link_el  = title_el  # job-title link IS the href

        title   = title_el.get_text(strip=True) if title_el else ""
        # Company name may have rating appended (e.g. "Veneficus4.6") — strip it
        raw_co  = co_el.get_text(strip=True) if co_el else ""
        company = _GD_RATING_RE.sub("", raw_co).strip()
        loc     = loc_el.get_text(strip=True) if loc_el else ""
        href    = title_el.get("href", "") if title_el else ""
        url     = href if href.startswith("http") else f"{_GD_BASE}{href}"

        if title and href:
            jobs.append({
                "title":    title,
                "url":      url,
                "company":  company,
                "location": loc,
                "source":   "glassdoor",
            })
    return jobs


def fetch_glassdoor(max_urls: int = 6, verbose: bool = False) -> list[_JOB]:
    """Playwright ile Glassdoor NL/TR slug URL'lerinden iş ilanları çeker."""
    if not _PW_AVAILABLE:
        if verbose:
            print("   ⚠️  Playwright bulunamadı — Glassdoor atlandı", flush=True)
        return []

    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    try:
        with sync_playwright() as pw:
            browser, ctx = _new_browser(pw, headless=True, country="nl")
            page = ctx.new_page()

            for url in GLASSDOOR_SLUG_URLS[:max_urls]:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_selector(
                            "li[data-test='jobListing'], li.react-job-listing",
                            timeout=10000,
                        )
                    except PlaywrightTimeout:
                        pass

                    for job in _extract_glassdoor_pw(page):
                        if job["url"] not in seen:
                            seen.add(job["url"])
                            all_jobs.append(job)
                except Exception:
                    continue

                time.sleep(random.uniform(2.5, 4.0))

            browser.close()
    except Exception as e:
        if verbose:
            print(f"   ⚠️  Glassdoor Playwright hatası: {e}", flush=True)

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# 5. INDEED NL — nl.indeed.com (Playwright — çalışıyor!)
# ─────────────────────────────────────────────────────────────────────────────

INDEED_NL_QUERIES = [
    "data scientist",
    "data engineer",
    "analytics engineer",
    "machine learning engineer",
    "data analyst",
    "nlp researcher",
    "ai engineer",
    "business intelligence",
    "applied scientist",
]

_INDEED_NL_BASE   = "https://nl.indeed.com"
_INDEED_NL_SEARCH = f"{_INDEED_NL_BASE}/vacatures"


def _extract_indeed_nl_pw(page) -> list[_JOB]:
    """Playwright page'den nl.indeed.com iş kartlarını çıkarır."""
    if not _BS4_AVAILABLE:
        return []
    soup  = BeautifulSoup(page.content(), "html.parser")
    jobs: list[_JOB] = []

    for card in soup.select("div.job_seen_beacon"):
        # data-jk is on the <a class='jcs-JobTitle'> link, not on the div
        link_el = card.select_one("a.jcs-JobTitle, a[class*='jcs-JobTitle']")
        co_el   = card.select_one("[data-testid='company-name']")
        loc_el  = card.select_one("[data-testid='text-location']")

        title   = link_el.get_text(strip=True) if link_el else ""
        company = co_el.get_text(strip=True)   if co_el   else ""
        loc     = loc_el.get_text(strip=True)  if loc_el  else "Netherlands"
        job_key = link_el.get("data-jk", "")  if link_el else ""
        url     = f"{_INDEED_NL_BASE}/rc/clk?jk={job_key}" if job_key else ""

        if title and url:
            jobs.append({
                "title":    title,
                "url":      url,
                "company":  company,
                "location": loc,
                "source":   "indeed.nl",
            })
    return jobs


def fetch_indeed_nl(max_queries: int = 6, verbose: bool = False) -> list[_JOB]:
    """Playwright ile nl.indeed.com'dan iş ilanları çeker."""
    if not _PW_AVAILABLE:
        if verbose:
            print("   ⚠️  Playwright bulunamadı — Indeed NL atlandı", flush=True)
        return []

    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    try:
        with sync_playwright() as pw:
            browser, ctx = _new_browser(pw, headless=True, country="nl")
            page = ctx.new_page()
            page.set_extra_http_headers({"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"})

            for query in INDEED_NL_QUERIES[:max_queries]:
                url = (
                    f"{_INDEED_NL_SEARCH}"
                    f"?q={requests.utils.quote(query)}&sort=date&fromage=30"
                )
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_selector("div.job_seen_beacon", timeout=10000)
                    except PlaywrightTimeout:
                        pass

                    for job in _extract_indeed_nl_pw(page):
                        if job["url"] not in seen:
                            seen.add(job["url"])
                            all_jobs.append(job)
                except Exception:
                    continue

                time.sleep(random.uniform(2.0, 3.5))

            browser.close()
    except Exception as e:
        if verbose:
            print(f"   ⚠️  Indeed NL Playwright hatası: {e}", flush=True)

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# 6. ACADEMIC POSITIONS — Küresel akademik iş ilanları (Playwright)
# ─────────────────────────────────────────────────────────────────────────────

_AP_BASE = "https://academicpositions.com"

# Coğrafi kısıtlama yok — tüm kategoriler /all (worldwide) ile taranır
ACADEMIC_POSITIONS_URLS = [
    f"{_AP_BASE}/find-jobs/data-science/1/all",
    f"{_AP_BASE}/find-jobs/machine-learning/1/all",
    f"{_AP_BASE}/find-jobs/artificial-intelligence/1/all",
    f"{_AP_BASE}/find-jobs/statistics/1/all",
    f"{_AP_BASE}/find-jobs/natural-language-processing/1/all",
    f"{_AP_BASE}/find-jobs/computer-science/1/all",
    f"{_AP_BASE}/find-jobs/computational-linguistics/1/all",
    f"{_AP_BASE}/find-jobs/information-science/1/all",
]


def _extract_academic_positions_pw(page) -> list[_JOB]:
    """Playwright page'den AcademicPositions iş kartlarını çıkarır."""
    if not _BS4_AVAILABLE:
        return []
    soup  = BeautifulSoup(page.content(), "html.parser")
    jobs: list[_JOB] = []

    for card in soup.select("div.list-group-item"):
        title_el = card.select_one("h4")
        link_el  = card.select_one("a[href*='/ad/']")
        org_el   = card.select_one("span.text-primary, a.job-link")
        loc_el   = card.select_one("div.job-locations")

        title   = title_el.get_text(strip=True) if title_el else ""
        href    = link_el.get("href", "")       if link_el  else ""
        url     = href if href.startswith("http") else f"{_AP_BASE}{href}"
        company = org_el.get_text(strip=True)   if org_el   else ""
        loc     = loc_el.get_text(strip=True).replace(",", ", ") if loc_el else ""

        if title and url:
            jobs.append({
                "title":    title,
                "url":      url,
                "company":  company,
                "location": loc,
                "source":   "academicpositions",
            })
    return jobs


def fetch_academic_positions(max_urls: int = 5, verbose: bool = False) -> list[_JOB]:
    """Playwright ile AcademicPositions'tan iş ilanları çeker."""
    if not _PW_AVAILABLE:
        if verbose:
            print("   ⚠️  Playwright bulunamadı — AcademicPositions atlandı", flush=True)
        return []

    all_jobs: list[_JOB] = []
    seen: set[str] = set()

    try:
        with sync_playwright() as pw:
            browser, ctx = _new_browser(pw, headless=True)
            page = ctx.new_page()

            for url in ACADEMIC_POSITIONS_URLS[:max_urls]:
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                    time.sleep(2)

                    for job in _extract_academic_positions_pw(page):
                        if job["url"] not in seen:
                            seen.add(job["url"])
                            all_jobs.append(job)
                except Exception:
                    continue

                time.sleep(random.uniform(2.0, 3.5))

            browser.close()
    except Exception as e:
        if verbose:
            print(f"   ⚠️  AcademicPositions Playwright hatası: {e}", flush=True)

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# ANA TOPLAMA FONKSİYONU
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_playwright(
    enable_linkedin:           bool = True,
    enable_indeed_nl:          bool = True,
    enable_glassdoor:          bool = True,
    enable_academic_positions: bool = True,
    enable_kariyer:            bool = False,  # PerimeterX + Türk IP gerektirir
    enable_indeed_tr:          bool = False,  # 403 Forbidden (geo-blocked)
    verbose: bool = True,
) -> list[_JOB]:
    """
    Tüm Playwright / requests tabanlı ek kaynaklardan iş ilanı toplar.
    Playwright kurulu değilse sadece LinkedIn (requests) çalışır.
    """
    all_jobs: list[_JOB] = []
    active = sum([enable_linkedin, enable_kariyer, enable_indeed_tr,
                  enable_glassdoor, enable_indeed_nl, enable_academic_positions])

    if verbose:
        print(f"\n🎭 Playwright/requests taraması başlıyor ({active} kaynak)...", flush=True)

    _steps = [
        ("LinkedIn",            enable_linkedin,           lambda: fetch_linkedin(verbose=verbose)),
        ("Indeed NL",           enable_indeed_nl,          lambda: fetch_indeed_nl(verbose=verbose)),
        ("Glassdoor",           enable_glassdoor,          lambda: fetch_glassdoor(verbose=verbose)),
        ("AcademicPositions",   enable_academic_positions, lambda: fetch_academic_positions(verbose=verbose)),
        ("Kariyer.net PW",      enable_kariyer,            lambda: fetch_kariyer_pw(verbose=verbose)),
        ("Indeed TR PW",        enable_indeed_tr,          lambda: fetch_indeed_tr_pw(verbose=verbose)),
    ]

    for name, enabled, fn in _steps:
        if not enabled:
            continue
        try:
            jobs = fn()
            all_jobs.extend(jobs)
            if verbose:
                print(f"   ✓ {name}: {len(jobs)} ilan", flush=True)
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

    parser = argparse.ArgumentParser(description="CareerOps Playwright Scrapers — test")
    parser.add_argument(
        "--source",
        choices=["linkedin", "kariyer", "indeed", "indeed-nl", "glassdoor", "academic-positions", "all"],
        default="linkedin",
        help="Test edilecek kaynak",
    )
    parser.add_argument("--queries", type=int, default=3, help="Sorgu sayısı")
    args = parser.parse_args()

    print(f"🎭 Playwright  : {'mevcut ✓' if _PW_AVAILABLE else 'bulunamadı ✗'}")
    print(f"🍵 BeautifulSoup: {'mevcut ✓' if _BS4_AVAILABLE else 'bulunamadı ✗'}\n")

    src = args.source

    if src in ("linkedin", "all"):
        print("--- LinkedIn (jobs-guest API) ---")
        jobs = fetch_linkedin(verbose=True)
        for j in jobs[:5]:
            print(f"  {j['company']:25} {j['title'][:45]:45} {j['location']}")
        print(f"  Toplam: {len(jobs)}\n")

    if src in ("kariyer", "all"):
        print("--- Kariyer.net (Playwright) ---")
        jobs = fetch_kariyer_pw(max_queries=args.queries, verbose=True)
        for j in jobs[:5]:
            print(f"  {j['company']:25} {j['title'][:45]:45} {j['location']}")
        print(f"  Toplam: {len(jobs)}\n")

    if src in ("indeed", "all"):
        print("--- Indeed TR (Playwright) ---")
        jobs = fetch_indeed_tr_pw(max_queries=args.queries, verbose=True)
        for j in jobs[:5]:
            print(f"  {j['company']:25} {j['title'][:45]:45} {j['location']}")
        print(f"  Toplam: {len(jobs)}\n")

    if src in ("indeed-nl", "all"):
        print("--- Indeed NL (Playwright) ---")
        jobs = fetch_indeed_nl(max_queries=args.queries, verbose=True)
        for j in jobs[:5]:
            print(f"  {j['company']:25} {j['title'][:45]:45} {j['location']}")
        print(f"  Toplam: {len(jobs)}\n")

    if src in ("glassdoor", "all"):
        print("--- Glassdoor (Playwright slug URL) ---")
        jobs = fetch_glassdoor(max_urls=args.queries, verbose=True)
        for j in jobs[:5]:
            print(f"  {j['company']:25} {j['title'][:45]:45} {j['location']}")
        print(f"  Toplam: {len(jobs)}\n")

    if src in ("academic-positions", "all"):
        print("--- AcademicPositions (Playwright) ---")
        jobs = fetch_academic_positions(max_urls=args.queries, verbose=True)
        for j in jobs[:5]:
            print(f"  {j['company']:25} {j['title'][:45]:45} {j['location']}")
        print(f"  Toplam: {len(jobs)}\n")
