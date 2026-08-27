#!/usr/bin/env python3
"""
CareerOps Telegram Hourly Notifier
Her saat çalışır (--daemon modu) veya tek seferlik tetiklenir.
Greenhouse, Lever, Ashby, Remotive, WeWorkRemotely, Kariyer.net ve
Indeed TR'yi tarar, profil uyumunu puanlar, yeni ilanları Telegram'a
gönderir ve yerel arşive kaydeder.

Kullanım:
  python telegram-daily.py           # tek seferlik çalıştır
  python telegram-daily.py --daemon  # saatlik döngü (erken başvuru avantajı)
"""

import csv
import hashlib
import re as _re
import io
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests

# ─── MODÜLLER ─────────────────────────────────────────────────────────────────
try:
    from proxy_manager import ProxyPool, safe_get
    PROXY_AVAILABLE = True
except ImportError:
    PROXY_AVAILABLE = False
    print("⚠️  proxy_manager.py bulunamadı — proxy olmadan devam edilecek", flush=True)

try:
    from portal_scrapers import fetch_all_extra
    SCRAPERS_AVAILABLE = True
except ImportError:
    SCRAPERS_AVAILABLE = False
    print("⚠️  portal_scrapers.py bulunamadı — yalnızca Greenhouse/Lever taranacak", flush=True)

try:
    from playwright_scrapers import fetch_all_playwright, fetch_linkedin
    PW_SCRAPERS_AVAILABLE = True
except ImportError:
    PW_SCRAPERS_AVAILABLE = False

try:
    from cf_scraper import make_cf_fetch
    CF_AVAILABLE = True
except ImportError:
    CF_AVAILABLE = False

try:
    # Modern SDK — eski google-generativeai paketi deprecate edildi
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

try:
    # Dinamik model çözümleyici — model adı sabitlenmez, katalogdan seçilir
    import gemini_model
    GEMINI_MODEL_AVAILABLE = gemini_model.SDK_AVAILABLE
except ImportError:
    GEMINI_MODEL_AVAILABLE = False

try:
    from freshness import human_age, job_age_minutes, minutes_since, within_window
    FRESHNESS_AVAILABLE = True
except ImportError:
    FRESHNESS_AVAILABLE = False
    print("⚠️  freshness.py bulunamadı — tazelik kapısı devre dışı", flush=True)

try:
    from run_state import freshness_window, load_state, record_run
    RUN_STATE_AVAILABLE = True
except ImportError:
    RUN_STATE_AVAILABLE = False
    print("⚠️  run_state.py bulunamadı — sabit pencere kullanılacak", flush=True)

try:
    from ingest import build_default_sources, run_ingestion
    INGEST_AVAILABLE = True
except ImportError:
    INGEST_AVAILABLE = False
    print("⚠️  ingest.py bulunamadı — toplama sırası garanti edilemiyor", flush=True)

try:
    from tr_portals import fetch_all_tr
    TR_PORTALS_AVAILABLE = True
except ImportError:
    TR_PORTALS_AVAILABLE = False

try:
    import geo_gate
    from geo_gate import resolve as geo_resolve
    GEO_GATE_AVAILABLE = True
except ImportError:
    GEO_GATE_AVAILABLE = False
    print("⚠️  geo_gate.py bulunamadı — statik konum filtresine düşülüyor", flush=True)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE  = f"https://api.telegram.org/bot{BOT_TOKEN}"

SENT_ARCHIVE = BASE_DIR / "data" / "telegram-sent.tsv"
ARCHIVE_LOG  = BASE_DIR / "data" / "telegram-archive.tsv"

MIN_SCORE               = 5.0   # Bu puanın altındakileri gönderme
# Profil bazlı eşik: P2 (R&D/NLP) puanlaması daha muhafazakâr (base 2.5'ten
# başlar), P1 eşiği uygulanınca gerçek eşleşmeler de eleniyordu
MIN_SCORE_BY_PROFILE    = {"P1": 5.0, "P2": 4.3}
# P2 havuzu yapısal olarak küçük; kota olmadan P1 kalabalığı tüm kontenjanı yer
P2_MIN_SLOTS            = 5     # her run'da P2'ye ayrılan minimum kontenjan
MAX_PER_RUN             = 15    # Saatlik maksimum ilan
FETCH_WORKERS           = 12    # Greenhouse/Lever paralel API isteği sayısı
SCHEDULE_INTERVAL_HOURS = 1     # --daemon modu tarama aralığı

# Ek kaynaklar — devre dışı bırakmak için False yap
ENABLE_ASHBY            = True
ENABLE_REMOTIVE         = True
ENABLE_WWR              = True
ENABLE_KARIYER          = True   # portal_scrapers (requests tabanlı)
ENABLE_INDEED_TR        = True   # portal_scrapers (requests tabanlı)
ENABLE_ACADEMICTRANSFER = True   # portal_scrapers (requests tabanlı), çalışıyor

# Premium (Bright Data ISP) proxy — varsa ücretsiz havuz devre dışı kalır
HAS_PREMIUM_PROXY = bool(os.environ.get("PREMIUM_PROXY_URL", "").strip())

# CF Scraper — CF korumalı sitelerde curl_cffi kullan (requests yerine)
# Premium proxy varsa otomatik açılır: TLS impersonation + ISP exit IP
USE_CF_FETCH = HAS_PREMIUM_PROXY or os.environ.get("USE_CF_FETCH", "").lower() in ("1", "true")

# LLM Enrichment — Gemini ile zenginleştirilmiş değerlendirme
GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY", "")
# Model adı artık SABİTLENMEZ — gemini_model.resolve_model() katalogdan
# seçer. GEMINI_MODEL env değişkeni hâlâ override olarak çalışır ama
# katalogda doğrulanır (yanlış yazım sessizce 404 olmasın).
GEMINI_MODEL_PRIORITY = os.environ.get(
    "GEMINI_MODEL_PRIORITY",
    "gemini-3.6-flash,gemini-3.5-flash,gemini-2.5-flash,gemini-2.0-flash")
ENABLE_LLM_ENRICHMENT = bool(GEMINI_API_KEY)  # env'de key varsa otomatik aktif
USE_CAVEMAN_PROMPTS   = True   # caveman_compress → ~%40 token tasarrufu
LLM_MAX_JOBS          = 10     # run başına LLM çağrı tavanı (free tier koruması)
LLM_MAX_DESC_CHARS    = 2800   # JD payload bütçesi (~700 token, caveman sınırı)
LLM_MAX_OUTPUT_TOKENS = 900    # yapılandırılmış JSON yanıt için yeterli

# Global çalışma süresi tavanı. Asılı bir tarama GitHub Actions'ın 360 dk'lık
# varsayılan iş zaman aşımına kadar dakika yakıyordu; bu tavan taramayı erken
# keser ve eldeki ilanlarla gönderime geçer.
RUN_BUDGET_SEC = int(os.environ.get("RUN_BUDGET_SEC", "600"))
SOURCE_BUDGET_SEC = int(os.environ.get("SOURCE_BUDGET_SEC", "150"))

# RLHF Feedback
RLHF_LOG     = BASE_DIR / "data" / "rlhf_feedback.json"
PENDING_JOBS = BASE_DIR / "data" / "telegram-pending.json"
OFFSET_FILE  = BASE_DIR / "data" / "telegram-offset.txt"

# Playwright kaynakları
ENABLE_LINKEDIN             = True   # requests tabanlı, hızlı, çalışıyor
ENABLE_INDEED_NL            = True   # nl.indeed.com, Playwright, çalışıyor
ENABLE_GLASSDOOR            = True   # slug URL ile, Playwright, çalışıyor
ENABLE_ACADEMIC_POSITIONS   = True   # Playwright, JS render, çalışıyor
ENABLE_KARIYER_PW           = HAS_PREMIUM_PROXY  # PerimeterX + TR IP gerektirir
ENABLE_INDEED_PW            = HAS_PREMIUM_PROXY  # geo-blocked; TR exit ile açılır

TURKISH_MONTHS = {
    1:"Ocak", 2:"Şubat", 3:"Mart", 4:"Nisan", 5:"Mayıs", 6:"Haziran",
    7:"Temmuz", 8:"Ağustos", 9:"Eylül", 10:"Ekim", 11:"Kasım", 12:"Aralık"
}

# ─── JOB SOURCES ─────────────────────────────────────────────────────────────
GREENHOUSE_BOARDS = [
    ("anthropic",         "Anthropic"),
    ("careem",            "Careem"),
    ("dbtlabs",           "dbt Labs"),
    ("hightouch",         "Hightouch"),
    ("airbyte",           "Airbyte"),
    ("fivetran",          "Fivetran"),
    ("elastic",           "Elastic"),
    ("n26",               "N26"),
    ("celonis",           "Celonis"),
    ("hellofresh",        "HelloFresh"),
    ("getyourguide",      "GetYourGuide"),
    ("stabilityai",       "Stability AI"),
    ("isomorphiclabs",    "Isomorphic Labs"),
    ("wayve",             "Wayve"),
    ("speechmatics",      "Speechmatics"),
    ("scandit",           "Scandit"),
    ("blackforestlabs",   "Black Forest Labs"),
    ("helsing",           "Helsing"),
    ("amplemarket",       "Amplemarket"),
    ("runwayml",          "Runway"),
    ("polyai",            "PolyAI"),
    ("parloa",            "Parloa"),
    ("intercom",          "Intercom"),
    ("humeai",            "Hume AI"),
    ("factorial",         "Factorial"),
    ("physicsx",          "PhysicsX"),
    ("traderepublicbank", "Trade Republic"),
    ("sumup",             "SumUp"),
    ("later",             "Later"),
    ("hootsuite",         "Hootsuite"),
    ("airtable",          "Airtable"),
    ("vercel",            "Vercel"),
    ("temporal",          "Temporal"),
    ("arizeai",           "Arize AI"),
    ("runpod",            "RunPod"),
    ("gleanwork",         "Glean"),
    ("boomilp",           "Boomi"),
    ("buynomics",         "Buynomics"),
    # ── P2 (R&D / NLP / dil verisi) odaklı board'lar ─────────────────────────
    # Önceki liste tamamen ürün/tech şirketlerinden oluşuyordu; anotasyon,
    # RLHF ve dil verisi işvereni hiç yoktu — P2 ilanı yapısal olarak
    # bulunamıyordu. Aşağıdakiler canlı API ile doğrulandı.
    ("scaleai",           "Scale AI"),
    ("turing",            "Turing"),
    ("labelbox",          "Labelbox"),
    ("snorkelai",         "Snorkel AI"),
    ("toloka",            "Toloka"),
    ("prolific",          "Prolific"),
    ("assemblyai",        "AssemblyAI"),
    ("speechmatics",      "Speechmatics"),
]

LEVER_COMPANIES = [
    ("adyen",       "Adyen"),
    ("spotify",     "Spotify"),
    ("klarna",      "Klarna"),
    ("mistral",     "Mistral AI"),
    ("wandb",       "Weights & Biases"),
    ("palantir",    "Palantir"),
    ("vinted",      "Vinted"),
    ("qonto",       "Qonto"),
    ("sanctuary",   "Sanctuary AI"),
    ("getir",       "Getir"),
    ("clarity-ai",  "Clarity AI"),
    ("pigment",     "Pigment"),
    ("lovable",     "Lovable"),
    ("legora",      "Legora"),
    ("forto",       "Forto"),
]

# ─── FILTERS ─────────────────────────────────────────────────────────────────
P1_KEYWORDS = [
    "data scientist", "data analyst", "data engineer", "business analyst",
    "business intelligence", "bi analyst", "analytics engineer", "ml engineer",
    "machine learning engineer", "ai engineer", "applied scientist",
    "quantitative analyst", "nlp engineer", "data specialist", "analytics",
    "veri bilimci", "veri analisti", "veri mühendisi", "iş analisti",
    "iş zekası", "makine öğrenmesi", "yapay zeka", "analitik",
    "veri etiketleme", "data science", "mlops", "llmops",
]

# P2 keywords: iki katman — score_job() T1 ve T2 farklı ağırlıklar uygular
P2_TIER1 = [
    # Yüksek özgüllük: doğrudan profil eşleşmesi (1.5× ağırlık)
    "nlp researcher", "computational linguist", "turkish language", "turkish specialist",
    "turkish linguist", "rlhf", "language model", "ai trainer", "language trainer",
    "psychometric", "doğal dil işleme", "veri etiketleme", "dil modeli",
    "r&d specialist", "research scientist", "araştırmacı", "dil uzmanı",
    "corpus", "red teaming", "post-training", "pre-training",
    # Türkçe LLM özel — aktif VM fine-tuning projesiyle doğrudan eşleşme
    # (generic "fine-tuning" veya "LLM" EKLENMEDİ — yalnızca Türkçe-spesifik)
    "turkish llm fine-tuning", "turkish llm alignment",
    "turkish language model fine-tuning", "turkish language model training",
    "turkish instruction tuning", "turkish nlp alignment",
    "low-resource turkish nlp", "turkish pre-training",
    "türkçe llm", "türkçe dil modeli eğitimi", "türkçe rlhf",
    "türkçe instruction tuning", "türkçe alignment",
    "mergen tlm", "mergen language model",
]
P2_TIER2 = [
    # Geniş keşif: bitişik veya transfer edilebilir roller (0.7× ağırlık)
    "linguistic", "linguist", "data annotator", "annotation", "language specialist",
    "language expert", "research associate", "research specialist", "research analyst",
    "content reviewer", "language quality", "ai quality", "model trainer",
    "educational researcher", "assessment specialist", "değerlendirme uzmanı",
    "eğitim araştırmacısı", "ölçme değerlendirme", "yapay zeka eğitimi",
    "ai safety", "content moderation", "information extraction", "named entity",
    "text classification", "text analyst", "speech recognition", "machine translation",
    # "nlp engineer" P1 listesinde de var; her ikisinde tutmak çift sayıma yol
    # açıp "NLP Engineer"i junior takibinden çıkarıyordu → yalnızca "nlp" bırakıldı
    "language technology", "text mining", "conversational ai",
    "semantic search", "fine-tuning", "ai evaluator", "data quality",
    "language data", "multilingual", "knowledge graph", "dialogue system",
    "embedding specialist", "annotation lead", "labeling", "label",
    # Tek başına güçlü P2 sinyalleri — bunlar listede yoktu, dolayısıyla
    # "Staff ML Engineer, NLP" gibi ünvanlar P1'e düşüp kıdem kapısında siliniyordu
    "nlp", "llm", "prompt engineer", "ontology", "taxonomy",
    "transcription", "localization", "localisation", "translator",
    "türkçe", "türk dili", "dilbilim",
]

# ─── CONFIG'DEN ANAHTAR KELİME BİRLEŞTİRME ───────────────────────────────────
# profile_rnd.matching blokları kod listeleriyle BİRLEŞTİRİLİR; böylece yeni
# hedef rol eklemek için kod düzenlemek gerekmez.
#
# İKİ DOSYA OKUNUR (sırayla, birleştirilerek):
#   1. config/matching.yml  — repoda tutulur, kişisel veri içermez → CI'da GEÇERLİ
#   2. config/profile.yml   — kişisel veri içerdiği için gitignore'da → yalnızca yerel
#
# Ayrım kritik: profile.yml runner'da hiç bulunmadığı için anahtar kelimeler
# yalnızca orada dursaydı üretimde sessizce devre dışı kalırdı.

MATCHING_CONFIG_PATHS = (
    BASE_DIR / "config" / "matching.yml",
    BASE_DIR / "config" / "profile.yml",
)


def _read_matching_block(path) -> dict:
    """Tek bir YAML dosyasından profile_rnd.matching bloğunu okur."""
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    cfg = ((data.get("profile_rnd") or {}).get("matching") or {})
    return cfg if isinstance(cfg, dict) else {}


def _load_matching_config(paths=None) -> dict:
    """Tüm config kaynaklarını okuyup listeleri birleştirir."""
    merged: dict = {}
    for p in (paths or MATCHING_CONFIG_PATHS):
        block = _read_matching_block(p)
        for key, values in block.items():
            if isinstance(values, list):
                merged.setdefault(key, []).extend(values)
    return merged


def _merge_keywords(base: list, extra) -> list:
    """Küçük harfe indirir, sırayı korur, tekrarları eler."""
    out, seen = [], set()
    for kw in list(base) + list(extra or []):
        k = str(kw).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


_MATCHING_CFG = _load_matching_config()
P2_TIER1 = _merge_keywords(P2_TIER1, _MATCHING_CFG.get("tier1"))
P2_TIER2 = _merge_keywords(P2_TIER2, _MATCHING_CFG.get("tier2"))
# Aynı kelime iki katmanda da varsa T1 kazanır (çift sayım P1 dengesini bozar)
P2_TIER2 = [k for k in P2_TIER2 if k not in set(P2_TIER1)]

P2_KEYWORDS = P2_TIER1 + P2_TIER2   # detect_profile() uyumluluğu için

# Portal aramalarında kullanılacak ek P2 sorguları
P2_SEARCH_QUERIES = [
    str(q).strip() for q in (_MATCHING_CFG.get("search_queries") or []) if str(q).strip()
]

NEGATIVE_KEYWORDS = [
    "ios", "android", "php", "ruby", "embedded", "firmware", "fpga",
    "blockchain", "web3", "crypto", "cobol", "mainframe",
    "network engineer", "devops engineer", "sre", "site reliability",
    "react developer", "angular developer", "frontend developer",
    "graphic designer", "ux designer", "finance manager", "accountant",
    "lawyer", "legal counsel",
]
# Config'den ek negatif kelimeler (kod listesi taban olarak korunur)
NEGATIVE_KEYWORDS = _merge_keywords(NEGATIVE_KEYWORDS, _MATCHING_CFG.get("negative"))

# ─── SCORING ─────────────────────────────────────────────────────────────────
# Pazar ekseni: Avrupa öncelikli, ABD tamamen bloklu, stajyer/öğrenci bloklu.
NL_TR_KW = ["netherlands","nederland","amsterdam","rotterdam","tilburg","utrecht","eindhoven","the hague","turkey","türkiye","istanbul","ankara","izmir"]
REMOTE_KW = ["remote","worldwide","global","anywhere","fully remote"]

# ── Avrupa çekirdeği (maksimum görünürlük ağırlığı) ──────────────────────────
EU_CORE_KW = [
    "netherlands","nederland","amsterdam","rotterdam","tilburg","utrecht","eindhoven",
    "the hague","den haag","groningen","delft",
    "germany","deutschland","berlin","munich","münchen","hamburg","frankfurt","cologne",
    "köln","stuttgart","düsseldorf",
    "united kingdom","england","scotland","london","cambridge","oxford","manchester",
    "edinburgh","bristol","glasgow",
    "france","paris","lyon","toulouse","nantes","bordeaux","lille",
    "spain","españa","madrid","barcelona","valencia","sevilla","málaga",
    "belgium","belgië","brussels","brussel","antwerp","ghent","leuven",
    "austria","österreich","vienna","wien","graz",
    "switzerland","schweiz","zurich","zürich","geneva","basel","lausanne",
    "ireland","dublin","cork",
    "italy","italia","milan","milano","rome","roma","turin","bologna",
    "portugal","lisbon","lisboa","porto",
    "sweden","stockholm","gothenburg","malmö",
    "denmark","copenhagen","københavn","aarhus",
    "norway","oslo","bergen",
    "finland","helsinki","espoo","tampere",
    "poland","warsaw","warszawa","krakow","kraków","wroclaw",
    "czech","prague","praha","brno",
    "luxembourg","estonia","tallinn","latvia","riga","lithuania","vilnius",
    "greece","athens","romania","bucharest","hungary","budapest",
    "bulgaria","sofia","croatia","zagreb","slovenia","ljubljana","slovakia","bratislava",
    "iceland","reykjavik","malta","cyprus",
]
# Avrupa geneli / bölgesel etiketler
EU_WIDE_KW = [
    "europe","european","eu-wide","emea","dach","benelux","nordic","nordics",
    "scandinavia","baltics","eea","cet","cest","gmt","eu remote","europe remote",
    "remote europe","remote (europe)","remote - europe","anywhere in europe",
]
# Türkiye — P2 dil verisi ekseni için stratejik, Avrupa dışı ama korunuyor
TR_KW = ["turkey","türkiye","turkiye","istanbul","ankara","izmir","bursa","antalya"]

GULF_KW  = ["uae","dubai","abu dhabi","saudi","riyadh","qatar","doha","kuwait","bahrain","oman"]

# ── HARD BLOCK: ABD ──────────────────────────────────────────────────────────
# Kelime sınırı ZORUNLU: "us" alt-dize olarak "Austria", "Belarus", "Prussia"
# içinde geçer; "ma"/"ca" gibi eyalet kısaltmaları da tek başına güvensizdir.
_US_LOCATION_RE = _re.compile(
    r"\b("
    r"usa|u\.s\.a\.?|u\.s\.|united states(?: of america)?|america|american|"
    r"new york|nyc|brooklyn|manhattan|san francisco|sf bay|bay area|silicon valley|"
    r"seattle|boston|chicago|austin|denver|atlanta|dallas|houston|miami|phoenix|"
    r"los angeles|san diego|san jose|palo alto|mountain view|menlo park|sunnyvale|"
    r"redmond|bellevue|portland|philadelphia|washington,? d\.?c\.?|arlington|"
    r"pittsburgh|detroit|minneapolis|nashville|charlotte|raleigh|durham|"
    r"salt lake city|las vegas|san antonio|columbus|indianapolis|kansas city|"
    r"california|texas|florida|virginia|massachusetts|illinois|colorado|georgia|"
    r"washington state|new jersey|pennsylvania|north carolina|arizona|oregon|"
    r"remote[ ,\-]*(?:us|usa|united states)|us[ \-]remote|"
    r"united states[ \-]remote"
    r")\b"
)
# ABD çalışma izni / vatandaşlık gerektiren ifadeler (açıklama + etiketlerde)
_US_AUTH_RE = _re.compile(
    r"\b("
    r"(?:legally )?authorized to work in the (?:us|u\.s\.|united states)|"
    r"work authorization in the (?:us|united states)|"
    r"us work authorization|u\.s\. work authorization|"
    r"us citizen(?:ship)?|u\.s\. citizen(?:ship)?|green card|permanent resident|"
    r"security clearance|ts/sci|public trust clearance|"
    r"h-?1b|opt/cpt|e-?verify|"
    r"must reside in the (?:us|united states)|"
    r"(?:us|united states)[ \-]based(?: only)?"
    r")\b"
)

# ── HARD BLOCK: Stajyer / öğrenci / çırak ────────────────────────────────────
# Kelime sınırı ZORUNLU: "intern" alt-dize olarak "international", "internal"
# içinde geçer — sınırsız eşleşme meşru ilanları siler.
_INTERN_RE = _re.compile(
    r"\b("
    r"intern|interns|internship|internships|"
    r"trainee|trainees|traineeship|"
    r"student|students|studentship|working student|"
    r"apprentice|apprentices|apprenticeship|"
    r"werkstudent|werkstudentin|praktikum|praktikant|praktikantin|"
    r"stagiair|stagiaire|stage[ \-]?intern|"
    r"alternance|alternant|apprenti|"
    r"becario|becaria|prácticas|practicas|"
    r"tirocinio|estágio|estagiário|"
    r"öğrenci staj|stajyer|staj programı|"
    r"co-?op student|summer analyst|campus hire|new grad program"
    r")\b"
)

SENIOR_KW = ["senior","lead","staff","principal","head","director","expert","specialist"]
# "intern" ve "trainee" JUNIOR_KW'den ÇIKARILDI: artık hard-block, kıdem bonusu
# alacak bir kategori değil. Kalsalardı bloklanan ilanlar +1.8 puan kazanırdı.
JUNIOR_KW = ["junior","entry","associate","graduate","jr."]
MEDIOR_KW = ["medior","mid-level","mid level","intermediate","midlevel"]


def market_gate(job: dict) -> str | None:
    """
    Pazar filtresi — BEYAZ LİSTE modeli.

    Kabul: coğrafi Avrupa + Türkiye. Diğer HER ŞEY düşer.
    Kıdem: intern/trainee/student/apprentice hard-block.

    Bloklanma sebebini döner, temizse None. Karar geo_gate kaskadına
    (T0-T5) devredilir; ülke kodu ACCEPTED_CC dışındaysa DROP.
    """
    title  = str(job.get("title", ""))
    loc    = str(job.get("location", ""))
    tags   = job.get("tags") or []
    tags_s = " ".join(map(str, tags)) if isinstance(tags, (list, tuple)) else str(tags)
    desc   = str(job.get("description", ""))

    # 1. Stajyer/öğrenci: başlık + açıklama + etiket
    for field, label in ((title, "title"), (tags_s, "tags"), (desc, "desc")):
        if field and _INTERN_RE.search(field.lower()):
            return f"INTERN/{label}"

    # 2. ABD çalışma izni — coğrafyadan bağımsız diskalifiye edici
    for field, label in ((desc, "desc"), (tags_s, "tags")):
        if field and _US_AUTH_RE.search(field.lower()):
            return f"US_AUTH/{label}"

    # 3. Coğrafi beyaz liste (T0-T5 kaskadı)
    if GEO_GATE_AVAILABLE:
        verdict = geo_resolve(loc, context=f"{title} {tags_s} {desc[:400]}")
        job["geo_cc"] = verdict.cc
        job["geo_w"]  = verdict.weight
        if verdict.verdict == geo_gate.DROP:
            return f"GEO/{verdict.cc}"
        return None

    # geo_gate yoksa: yalnızca statik ABD regex'i (bozulmadan çalışsın)
    for field, label in ((loc, "location"), (title, "title")):
        if field and _US_LOCATION_RE.search(field.lower()):
            return f"US/{label}"
    return None


def geo_weight(location: str) -> float:
    """Coğrafi görünürlük ağırlığı — geo_gate kaskadından türetilir."""
    if GEO_GATE_AVAILABLE:
        return geo_resolve(location).weight
    loc = (location or "").lower()
    if any(kw in loc for kw in EU_CORE_KW):
        return 1.5
    if any(kw in loc for kw in EU_WIDE_KW):
        return 1.2
    if any(kw in loc for kw in TR_KW):
        return 0.8
    if any(kw in loc for kw in REMOTE_KW):
        return 0.4
    return 0.0

# ── Kıdem Kapısı (Seniority Gate) ────────────────────────────────────────────
# P1 (Junior Data/AI) için başlık bazlı sert engel.
# Bu kelimelerden herhangi biri ünvanda varsa ilan P1 profili için reddedilir.
# P2 (R&D/NLP Senior) için bu kısıtlama uygulanmaz.
_P1_SENIOR_WORDS = frozenset({
    "senior", "sr",          # "Senior Data Scientist", "Sr. ML Engineer"
    "lead",                  # "Data Science Lead", "Lead Data Analyst"
    "principal",             # "Principal Data Scientist"
    "staff",                 # "Staff ML Engineer", "Staff Data Scientist"
    "director",              # "Director of Analytics"
    "manager",               # "Data Science Manager", "Analytics Manager"
    "chief",                 # "Chief Data Officer"
    "vp",                    # "VP of Data", "VP Analytics"
})


def _is_senior_for_p1(title: str) -> bool:
    """
    Ünvandaki kelimeleri ayrıştırarak P1 için kıdem engeli uygular.
    Kelime bazlı eşleşme (substring değil) → yanlış pozitif riski düşük.
    "Head of Data/Analytics" özel durumunu ayrıca kontrol eder.
    """
    words = set(_re.split(r"[\s\-/|,.()+]+", title.lower()))
    if words & _P1_SENIOR_WORDS:
        return True
    # "head" tek başına çok genel; "head of X" kalıbı olarak kontrol et
    if "head" in words and words & {"of", "data", "analytics", "ml", "ai", "science"}:
        return True
    return False


# ─── SCOUT & SENTINEL ────────────────────────────────────────────────────────
# Infrastructure block detection + freshness gate.
# detect_block() → call on raw response text before JSON parsing.
# is_fresh()     → call on each job dict; pass-through when no timestamp.

_BLOCK_SIGNATURES: dict[str, list[str]] = {
    "CLOUDFLARE":  ["cloudflare", "ray id", "turnstile", "just a moment", "cf-browser-verification"],
    "CAPTCHA":     ["captcha", "recaptcha", "hcaptcha", "verify you are human", "security check", "attention required"],
    "HTTP_403":    ["403 forbidden", "access denied", "access is denied"],
    "RATE_LIMIT":  ["429 too many requests", "rate limited", "too many requests"],
    "AUTH_WALL":   ["sign in to view", "log in to see", "create an account to view", "please log in"],
}

FRESHNESS_GATE_MINUTES = 60    # taban pencere; run_state gecikmeye göre genişletir

# Zaman damgası ÜRETEMEYEN kaynaklar. Bunlarda "bilinmiyor" ilanı elemez —
# tekrar gönderimi zaten arşiv dedup'ı engeller. Diğer tüm kaynaklarda
# damgasız ilan ELENİR (katı mod).
FRESHNESS_EXEMPT_SOURCES = {
    "glassdoor", "academicpositions", "academictransfer", "indeed.tr",
    # T1 yurt içi: techcareer liste görünümünde tarih vermiyor. TR mutlak
    # öncelikli olduğu için damgasız diye elemek yerine muaf tutuluyor;
    # tekrar gönderimi zaten arşiv dedup'ı engelliyor.
    "techcareer", "isinolsun", "kariyer.net",
}


def detect_block(text: str) -> str | None:
    """
    Scout & Sentinel threat detector.
    Pass the raw response body (HTML or JSON string) before parsing.
    Returns block-type string on threat, None if payload looks clean.
    """
    if not text or len(text.strip()) < 200:
        return "EMPTY_RESPONSE"
    t = text.lower()
    for block_type, patterns in _BLOCK_SIGNATURES.items():
        if any(p in t for p in patterns):
            return block_type
    return None


def parse_freshness_minutes(timestamp_str: str) -> int | None:
    """
    Zaman damgasını "kaç dakika önce" değerine çevirir.
    ISO-8601, epoch (s/ms) ve göreli metin (TR/EN/NL) desteklenir.
    Ayrıştırılamazsa None.
    """
    return minutes_since(timestamp_str) if FRESHNESS_AVAILABLE else None


def is_fresh(job: dict, max_minutes: int = FRESHNESS_GATE_MINUTES) -> bool:
    """
    İlan tazelik penceresinde mi?

    Katı mod: zaman damgası yoksa ilan ELENİR — ancak kaynak yapısal olarak
    damga üretemiyorsa (FRESHNESS_EXEMPT_SOURCES) muaf tutulur.
    """
    if not FRESHNESS_AVAILABLE:
        return True
    src = str(job.get("source", "")).split("/")[0].lower()
    unknown_ok = src in FRESHNESS_EXEMPT_SOURCES
    return within_window(job, max_minutes, unknown_ok=unknown_ok)


def detect_profile(title: str) -> str | None:
    """
    Ünvanı P1 (Junior Data/AI) veya P2 (Senior R&D/NLP) profiline atar.

    Önceki sürümdeki iki hata P2 ilanlarının neredeyse tamamını eliyordu:

    1. Beraberlik P1'e gidiyordu. "NLP Engineer" hem P1 hem P2 listesinde
       geçtiği için p1 == p2 oluyor, ilan P1 sayılıyordu.
    2. P1'e atanan kıdemli ünvan doğrudan çöpe gidiyordu. Yani
       "Senior NLP Engineer" önce (1) yüzünden P1 oluyor, sonra kıdem
       kapısına takılıp siliniyordu — oysa tam olarak P2 profilinin hedefi.

    Yeni mantık: T1 eşleşmeleri (yüksek özgüllüklü, "rlhf"/"computational
    linguist") P1'in genel kelimelerini geçersiz kılar; kıdemli ünvanda
    herhangi bir P2 sinyali varsa ilan P1'den düşürülmek yerine P2'ye taşınır.
    """
    t = title.lower()
    if any(neg in t for neg in NEGATIVE_KEYWORDS):
        return None

    p1_hits = [kw for kw in P1_KEYWORDS if kw in t]
    t1_hits = [kw for kw in P2_TIER1 if kw in t]
    t2_hits = [kw for kw in P2_TIER2 if kw in t]

    # Özgüllük kuralı: bir P1 kelimesi, eşleşen bir P2 kelimesinin İÇİNDE
    # geçiyorsa sayılmaz — daha spesifik eşleşme generic olanı kapsar.
    # Örn. "Text Analytics Specialist": P1 "analytics" ile P2 "text analytics"
    # çakışıyordu ve generic olan kazanıp ilanı P1'e (ve eşiğin altına) itiyordu.
    p2_hits = t1_hits + t2_hits
    p1_hits = [kw for kw in p1_hits if not any(kw in p2kw for p2kw in p2_hits)]

    p1 = len(p1_hits)
    t1 = len(t1_hits)
    t2 = len(t2_hits)
    p2 = t1 + t2
    if p1 == 0 and p2 == 0:
        return None

    # T1 = 1.5×, T2 = 0.7× — score_job ile aynı ağırlıklandırma
    p2_weight = t1 * 1.5 + t2 * 0.7
    senior    = _is_senior_for_p1(title)

    if p2_weight > p1:
        return "P2"
    if senior:
        # Kıdemli ünvan: P1 (junior) için uygun değil ama P2 sinyali varsa P2
        return "P2" if p2 > 0 else None
    return "P1"


def _apply_profile_quota(jobs: list, limit: int, p2_slots: int = P2_MIN_SLOTS) -> list:
    """
    Kontenjanı profiller arasında paylaştırır.

    Saf skor sıralamasında P1 ilanları (çok daha kalabalık kaynak havuzu)
    listenin tamamını dolduruyor ve P2 hiç görünmüyordu. Burada P2'ye
    ayrılmış minimum kontenjan verilir; P2 o kadar ilan yoksa boş kalan
    kontenjan P1'e döner.
    """
    if len(jobs) <= limit:
        return jobs
    p2 = [j for j in jobs if j.get("profile") == "P2"]
    p1 = [j for j in jobs if j.get("profile") != "P2"]
    take_p2 = min(len(p2), max(p2_slots, 0))
    take_p1 = limit - take_p2
    if take_p1 > len(p1):                      # P1 az ise kalanı P2'ye ver
        take_p2 = min(len(p2), limit - len(p1))
        take_p1 = len(p1)
    merged = p1[:take_p1] + p2[:take_p2]
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged[:limit]


def score_job(title: str, location: str, profile: str) -> float:
    t   = title.lower()
    loc = location.lower()

    # ── Base — keyword eşleşme yoğunluğu ────────────────────────────────────────
    if profile == "P1":
        hits = sum(1 for kw in P1_KEYWORDS if kw in t)
        base = min(6.0, 3.0 + hits * 1.0)
    else:
        # P2: T1 (yüksek özgüllük) ve T2 (geniş keşif) farklı ağırlıklar alır
        # → varyansı artırır, gerçek eşleşmeleri sıralar
        t1_hits = sum(1 for kw in P2_TIER1 if kw in t)
        t2_hits = sum(1 for kw in P2_TIER2 if kw in t)
        base = min(7.0, 2.5 + t1_hits * 1.5 + t2_hits * 0.7)

    # ── Kıdem uyumu — 4 katmanlı (medior artık ayrı puanlanır) ───────────────
    is_junior = any(kw in t for kw in JUNIOR_KW)
    is_medior = any(kw in t for kw in MEDIOR_KW)
    is_senior = any(kw in t for kw in SENIOR_KW)

    if profile == "P1":
        # P1 için senior başlıklar detect_profile'de zaten bloklandı;
        # buraya ulaşırsa is_senior=True beklenmiyor — güvenlik neti olarak -0.5
        if is_junior:
            sen_bonus = 1.8   # Açık junior → maksimum uyum
        elif is_medior:
            sen_bonus = 0.3   # Medior → erişilebilir ama düşük öncelik
        elif is_senior:
            sen_bonus = -0.5  # Sızma güvenlik neti
        else:
            sen_bonus = 1.0   # Belirtilmemiş → nötr pozitif
    else:
        # P2 (Senior R&D/NLP): kıdemli roller tercih edilir
        if is_senior:    sen_bonus = 1.5
        elif is_medior:  sen_bonus = 1.0
        elif is_junior:  sen_bonus = 0.5
        else:            sen_bonus = 0.8

    # ── Turkish/NLP bonusu (P2) ───────────────────────────────────────────────
    nlp_bonus = 0.0
    if profile == "P2":
        if "turkish" in t or "türkçe" in t or any(kw in loc for kw in ["turkey","türkiye","istanbul","ankara"]):
            nlp_bonus = 1.0
        elif any(kw in t for kw in ["nlp","linguist","annotator","language model","rlhf"]):
            nlp_bonus = 0.5

    # ── Coğrafi öncelik — Avrupa pazarı ağırlığı ────────────────────────────
    geo_bonus = geo_weight(location)

    return min(10.0, round(base + sen_bonus + nlp_bonus + geo_bonus, 1))


# ─── LLM ENRICHMENT ──────────────────────────────────────────────────────────

def evaluate_job_llm(job: dict) -> dict | None:
    """
    Qualifying bir ilan için Gemini ile zenginleştirilmiş değerlendirme yapar.

    Model adı SABİT DEĞİL: gemini_model.resolve_model() çalışma anında
    canlı katalogdan öncelik listesindeki ilk mevcut modeli seçer ve
    önbelleğe alır. Sağlayıcı bir modeli kaldırdığında katman 404 ile
    düşmez; önbellek geçersizleştirilip yeniden çözümlenir.

    Tek atışlık yapılandırılmış puanlama — AFC (araç çağrısı) KULLANILMAZ.
    Token tasarrufu: JD payload text_clean ile arındırılıp
    LLM_MAX_DESC_CHARS'a kırpılır, thinking kapalı, yanıt doğrudan JSON
    mime olarak istenir (markdown çiti + açıklama metni üretilmez).
    """
    if not ENABLE_LLM_ENRICHMENT or not GEMINI_MODEL_AVAILABLE:
        return None
    try:
        from evaluator import build_prompt, parse_score

        prompt = build_prompt(
            job,
            compress=USE_CAVEMAN_PROMPTS,
            max_desc_chars=LLM_MAX_DESC_CHARS,
        )
        text, usage, model = gemini_model.generate_json(
            prompt,
            temperature=0.2,
            max_output_tokens=LLM_MAX_OUTPUT_TOKENS,
            thinking_budget=0,
        )
        if not text:
            return None

        result = parse_score(text)
        result["_model"] = model
        if usage:
            result["_tokens"] = {
                "in":  getattr(usage, "prompt_token_count", 0),
                "out": getattr(usage, "candidates_token_count", 0),
            }
        return result
    except Exception as e:
        print(f"⚠️  LLM zenginleştirme hatası ({job.get('title','?')}): {e}",
              flush=True)
        return None


# ─── API FETCHERS (Greenhouse / Lever) ────────────────────────────────────────

def _make_fetch(pool):
    """
    ProxyPool varsa proxy kullanan, yoksa doğrudan requests kullanan
    fetch fonksiyonu döner.
    """
    if pool and PROXY_AVAILABLE:
        def fetch(url, **kwargs):
            return safe_get(url, pool, **kwargs)
    else:
        def fetch(url, **kwargs):
            try:
                kwargs.setdefault("timeout", 15)
                return requests.get(url, **kwargs)
            except Exception:
                return None
    return fetch


def fetch_greenhouse(slug, company, pool=None):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        # Açık JSON API — proxy gerekmez. Ücretsiz havuz (medyan ~7s gecikme)
        # üzerinden geçirmek run süresini katlıyordu. Yalnızca doğrudan
        # istek başarısız olursa havuza düşülür.
        r = requests.get(url, timeout=15)
        if (not r or r.status_code != 200) and pool and PROXY_AVAILABLE:
            r = safe_get(url, pool, timeout=15)
        if not r or r.status_code != 200:
            return []
        jobs = []
        for j in r.json().get("jobs", []):
            loc = j.get("location", {})
            jobs.append({
                "title":    j.get("title", ""),
                "url":      j.get("absolute_url", ""),
                "company":  company,
                "location": loc.get("name", "") if isinstance(loc, dict) else str(loc),
                "source":   f"greenhouse/{slug}",
                # first_published = ilk yayın; updated_at düzenlemede değişir
                "posted_at": j.get("first_published") or j.get("updated_at") or "",
            })
        return [j for j in jobs if j["title"] and j["url"]]
    except Exception:
        return []


def fetch_lever(slug, company, pool=None):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        # Açık JSON API — bkz. fetch_greenhouse notu
        r = requests.get(url, timeout=15)
        if (not r or r.status_code != 200) and pool and PROXY_AVAILABLE:
            r = safe_get(url, pool, timeout=15)
        if not r or r.status_code != 200:
            return []
        jobs = []
        for j in r.json():
            cats = j.get("categories", {})
            locs = cats.get("allLocations", [])
            loc  = locs[0] if locs else cats.get("location", "")
            jobs.append({
                "title":    j.get("text", ""),
                "url":      j.get("hostedUrl") or j.get("applyUrl", ""),
                "company":  company,
                "location": loc,
                "source":   f"lever/{slug}",
                "posted_at": j.get("createdAt") or "",   # epoch ms
            })
        return [j for j in jobs if j["title"] and j["url"]]
    except Exception:
        return []


# ─── ARCHIVE ─────────────────────────────────────────────────────────────────

SENT_HEADER = ["id", "url", "company", "title", "location", "profile", "score", "sent_at"]

_TSV_BAD = _re.compile(r"[\r\n\t]+")


def _tsv_clean(value) -> str:
    """
    Alan içindeki CR/LF/TAB'ı temizler.

    Bazı ilan başlıkları gömülü \\r taşıyordu; csv okuyucu bunu kayıt sonu
    sayıp satırı ikiye bölüyor, bölünen parçalar da geçersiz URL'ler olarak
    dedup setine giriyordu (aynı ilan tekrar gönderilebiliyordu).
    """
    return _TSV_BAD.sub(" ", str(value if value is not None else "")).strip()


# Kimlik TAŞIMAYAN (yalnızca izleme amaçlı) query parametreleri.
# DİKKAT: query'nin tamamını atmak YANLIŞ — Greenhouse gömülü sayfaları
# (?gh_jid=), Indeed (?jk=) ve Trade Republic (?jobId=) ilan kimliğini
# tam olarak burada taşır; atılırsa farklı ilanlar aynı sayılır.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "source", "src", "trk", "trackingid", "refid",
    "originalsubdomain", "position", "pagenum", "ebp", "savedsearchid",
    "sessionid", "gh_src", "lipi", "licu", "eventorigin", "recommendedflavor",
    "fbclid", "gclid", "msclkid", "at", "ts", "rq", "rsk",
})


def canonical_url(url: str) -> str:
    """İzleme parametrelerini atar, kimlik taşıyanları korur."""
    p = urlsplit((url or "").strip().lower())
    kept = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    )
    # Şema (http/https) kimliğin parçası değil → dışarıda bırakılır
    return urlunsplit(("", p.netloc, p.path.rstrip("/"), urlencode(kept), ""))


def job_id(job: dict) -> str:
    """İzleme parametreleri değişse de sabit kalan ilan kimliği."""
    key = canonical_url(job.get("url", ""))
    if not key:
        key = f"{job.get('company','')}|{job.get('title','')}".lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load_sent_urls() -> set:
    """Hem id hem ham URL döner — eski (id'siz) satırlarla geriye dönük uyumlu."""
    sent: set[str] = set()
    for path in (SENT_ARCHIVE, ARCHIVE_LOG):
        if not path.exists():
            continue
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                url = (row.get("url") or "").strip()
                if url:
                    sent.add(url)
                    sent.add(job_id({"url": url}))   # eski satırları da id'ye çevir
                if row.get("id"):
                    sent.add(row["id"].strip())
    return sent


def save_sent(jobs: list):
    """Append-only; CI'da her run sonrası repoya geri push edilir."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    for path in (SENT_ARCHIVE, ARCHIVE_LOG):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            if write_header:
                w.writerow(SENT_HEADER)
            for job in jobs:
                w.writerow([_tsv_clean(v) for v in (
                    job.get("id") or job_id(job), job["url"], job["company"],
                    job["title"], job.get("location", ""), job["profile"],
                    f"{job['score']:.1f}", now,
                )])
            f.flush()
            os.fsync(f.fileno())   # runner kill edilirse satırlar kaybolmasın


# ─── RLHF FEEDBACK ───────────────────────────────────────────────────────────

def save_pending_jobs(jobs: list):
    """Gönderilen ilanları callback arama için PENDING_JOBS dosyasına kaydeder."""
    pending = {
        str(i): {
            "url":     j["url"],
            "title":   j["title"],
            "company": j["company"],
            "score":   j["score"],
            "profile": j["profile"],
        }
        for i, j in enumerate(jobs)
    }
    PENDING_JOBS.parent.mkdir(parents=True, exist_ok=True)
    with open(PENDING_JOBS, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, separators=(",", ":"))


def log_rlhf_feedback(job_id: str, decision: str):
    """Bir RLHF geri bildirim girişini rlhf_feedback.json dosyasına ekler."""
    if not PENDING_JOBS.exists():
        return
    with open(PENDING_JOBS, encoding="utf-8") as f:
        pending = json.load(f)
    job = pending.get(str(job_id))
    if not job:
        return
    entry = {"timestamp": datetime.now().isoformat(), "decision": decision, **job}
    feedback: list = []
    if RLHF_LOG.exists():
        try:
            with open(RLHF_LOG, encoding="utf-8") as f:
                feedback = json.load(f)
        except json.JSONDecodeError:
            feedback = []
    feedback.append(entry)
    RLHF_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RLHF_LOG, "w", encoding="utf-8") as f:
        json.dump(feedback, f, ensure_ascii=False, separators=(",", ":"))
    print(f"📝 RLHF: '{decision}' → {job['title']} @ {job['company']}", flush=True)


def handle_callbacks():
    """Telegram callback sorgularını çeker, RLHF kararlarını kaydeder."""
    offset = 0
    if OFFSET_FILE.exists():
        try:
            offset = int(OFFSET_FILE.read_text().strip()) + 1
        except ValueError:
            pass
    try:
        resp = requests.get(
            f"{API_BASE}/getUpdates",
            params={"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]},
            timeout=10,
        )
        updates = resp.json().get("result", [])
    except Exception as e:
        print(f"⚠️  Callback alınamadı: {e}", flush=True)
        return
    logged = 0
    for upd in updates:
        uid = upd.get("update_id", 0)
        cb  = upd.get("callback_query")
        if cb:
            parts = cb.get("data", "").split("|")
            if len(parts) == 2:
                decision, job_id = parts
                log_rlhf_feedback(job_id, decision.upper())
                logged += 1
                try:
                    icon = "✅" if decision == "apply" else "⏭"
                    requests.post(
                        f"{API_BASE}/answerCallbackQuery",
                        json={"callback_query_id": cb["id"], "text": f"{icon} Kaydedildi!"},
                        timeout=5,
                    )
                except Exception:
                    pass
        OFFSET_FILE.write_text(str(uid))
    if logged:
        print(f"📬 {logged} RLHF geri bildirimi işlendi", flush=True)


# ─── TELEGRAM ────────────────────────────────────────────────────────────────
FLAG_MAP = {
    "netherlands": "🇳🇱", "amsterdam": "🇳🇱", "rotterdam": "🇳🇱",
    "tilburg": "🇳🇱", "eindhoven": "🇳🇱", "utrecht": "🇳🇱",
    "turkey": "🇹🇷", "türkiye": "🇹🇷", "istanbul": "🇹🇷", "ankara": "🇹🇷",
    "germany": "🇩🇪", "berlin": "🇩🇪", "munich": "🇩🇪", "hamburg": "🇩🇪",
    "france": "🇫🇷", "paris": "🇫🇷",
    "united kingdom": "🇬🇧", "london": "🇬🇧", "cambridge": "🇬🇧",
    "sweden": "🇸🇪", "stockholm": "🇸🇪",
    "spain": "🇪🇸", "barcelona": "🇪🇸", "madrid": "🇪🇸",
    "portugal": "🇵🇹", "lisbon": "🇵🇹",
    "ireland": "🇮🇪", "dublin": "🇮🇪",
    "switzerland": "🇨🇭", "zurich": "🇨🇭",
    "austria": "🇦🇹", "vienna": "🇦🇹",
    "usa": "🇺🇸", "united states": "🇺🇸", "new york": "🇺🇸", "san francisco": "🇺🇸",
    "canada": "🇨🇦", "toronto": "🇨🇦", "vancouver": "🇨🇦",
    "australia": "🇦🇺", "sydney": "🇦🇺", "melbourne": "🇦🇺",
    "uae": "🇦🇪", "dubai": "🇦🇪", "abu dhabi": "🇦🇪",
    "qatar": "🇶🇦", "doha": "🇶🇦",
    "saudi": "🇸🇦", "riyadh": "🇸🇦",
    "remote": "🌐", "worldwide": "🌐", "global": "🌐",
}

SOURCE_ICONS = {
    "greenhouse":  "🌱",
    "lever":       "⚙️",
    "ashby":       "🔷",
    "remotive":    "🌍",
    "wwr":         "💻",
    "kariyer.net": "🇹🇷",
    "indeed.tr":   "🔍",
    "linkedin":    "💼",
    "glassdoor":   "🚪",
    "indeed.nl":        "🔎",
    "academictransfer": "🎓",
    "academicpositions":"🏛️",
}


def get_flag(location: str) -> str:
    loc = location.lower()
    for kw, flag in FLAG_MAP.items():
        if kw in loc:
            return flag
    return "📍"


def get_source_icon(source: str) -> str:
    for key, icon in SOURCE_ICONS.items():
        if source.startswith(key):
            return icon
    return "📌"


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def score_bar(score: float) -> str:
    filled = round(score / 2)
    return "⭐" * filled + "☆" * (5 - filled)


def format_job(job: dict, idx: int) -> str:
    flag    = get_flag(job.get("location", ""))
    src_ico = get_source_icon(job.get("source", ""))
    loc     = html_escape(job.get("location") or "Remote / Belirtilmemiş")
    company = html_escape(job["company"])
    title   = html_escape(job["title"])
    profile = "🔬 P2 — R&amp;D/NLP" if job["profile"] == "P2" else "📊 P1 — Data/AI"
    score   = job["score"]
    bar     = score_bar(score)
    url     = job["url"]

    lines = [
        f"<b>{idx}. {company}</b> {src_ico}",
        f"📋 {title}",
        f"{flag} {loc}",
        profile,
        f"{bar} <b>{score:.1f}/10</b>",
    ]

    llm = job.get("llm") or {}

    if llm.get("company_insight"):
        lines.append(f"🏢 <i>{html_escape(str(llm['company_insight']))}</i>")

    if llm.get("missing_keywords"):
        kws = " ".join(
            f"<code>{html_escape(str(k))}</code>"
            for k in llm["missing_keywords"][:3]
        )
        lines.append(f"🎯 ATS eksik: {kws}")

    if llm.get("outreach_msg"):
        msg = html_escape(str(llm["outreach_msg"]))[:600]
        lines.append(f"\n💬 <b>Outreach Taslağı:</b>\n<i>{msg}</i>")

    lines.append(f'🔗 <a href="{url}">Başvur →</a>')
    return "\n".join(lines)


def send_message(text: str) -> dict:
    resp = requests.post(
        f"{API_BASE}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=15,
    )
    return resp.json()


def send_job_message(text: str, job_id: str) -> dict:
    """İlan mesajını RLHF inline karar butonları ile gönderir."""
    keyboard = {"inline_keyboard": [[
        {"text": "👍 Başvuracağım",    "callback_data": f"apply|{job_id}"},
        {"text": "👎 İlgimi çekmiyor", "callback_data": f"pass|{job_id}"},
    ]]}
    resp = requests.post(
        f"{API_BASE}/sendMessage",
        json={
            "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": False, "reply_markup": keyboard,
        },
        timeout=15,
    )
    return resp.json()


def broadcast(jobs: list, date_str: str):
    if not jobs:
        send_message(
            f"🤖 <b>CareerOps — {date_str}</b>\n\n"
            "Bugün yeni uygun ilan bulunamadı.\n"
            "Yarın tekrar kontrol edeceğim. 🔄"
        )
        return

    now = datetime.now()
    send_message(
        f"🤖 <b>CareerOps Saatlik Tarama</b>\n"
        f"📅 {date_str} — {now:%H:%M}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆕 <b>{len(jobs)} yeni ilan</b> "
        f"(puan ≥ {MIN_SCORE:.0f}, azalan sırada)"
    )

    save_pending_jobs(jobs)

    for i, job in enumerate(jobs):
        text = format_job(job, i + 1)
        send_job_message(text, str(i))

    send_message(
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 Detaylı analiz: <code>/career-ops pipeline</code>\n"
        "🗂 Tüm arşiv: <code>data/telegram-archive.tsv</code>"
    )


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    run_started = time.monotonic()
    now      = datetime.now()
    date_str = f"{now.day} {TURKISH_MONTHS[now.month]} {now.year}"
    print(f"[{now:%Y-%m-%d %H:%M}] CareerOps başlıyor — {date_str}", flush=True)

    # ── 0. Tazelik penceresi — son BAŞARILI taramadan bu yana geçen süre ──────
    # Cron gecikirse veya bir run atlanırsa pencere otomatik genişler;
    # aradaki ilanlar kaybolmaz, eskiler de içeri sızmaz.
    if RUN_STATE_AVAILABLE:
        window, why = freshness_window(load_state())
    else:
        window, why = FRESHNESS_GATE_MINUTES, "run_state yok"
    print(f"⏱  Tazelik penceresi: {window}dk ({why})", flush=True)

    # ── 1. Proxy havuzu başlat ────────────────────────────────────────────────
    pool = None
    if HAS_PREMIUM_PROXY:
        # ISP gateway kendi rotasyonunu yapar — ücretsiz havuz taraması
        # hem zaman hem bant genişliği israfı olur
        print("🔐 Premium ISP proxy aktif (ücretsiz havuz atlandı)", flush=True)
    elif PROXY_AVAILABLE:
        print("🔐 Proxy havuzu başlatılıyor...", flush=True)
        try:
            pool = ProxyPool(auto_refresh=True, verbose=True)
            print(f"   Aktif proxy: {pool.size()}", flush=True)
        except Exception as e:
            print(f"   ⚠️  Proxy başlatma hatası: {e} — doğrudan bağlantıyla devam ediliyor", flush=True)
            pool = None

    if USE_CF_FETCH and CF_AVAILABLE:
        fetch_fn = make_cf_fetch(pool, verbose=False)
        print("🔐 Fetch modu: curl_cffi (CF bypass)", flush=True)
    else:
        fetch_fn = _make_fetch(pool)

    # ── 1b. Önceki RLHF callback'leri işle ──────────────────────────────────
    handle_callbacks()

    # ── 2. Daha önce gönderilenleri yükle ────────────────────────────────────
    sent_urls = load_sent_urls()
    print(f"📋 Arşiv: {len(sent_urls)} önceden gönderilmiş URL", flush=True)

    # ── 2b-4. TOPLAMA ORKESTRATÖRÜ (ingest.py) ───────────────────────────────
    # Kesin öncelik sırası:  P1 LinkedIn → P2 Kariyer.net → P3 kalan kaynaklar
    # LinkedIn P1 ve bütçeden muaf: en yüksek verimli kaynak, requests tabanlı,
    # proxy/Playwright gerektirmiyor. Kariyer.net P2 (T1 yurt içi pazarın
    # omurgası). Greenhouse/Lever ve diğerleri P3'te eş zamanlı.
    raw: list[dict] = []
    kariyer_empty = indeed_tr_empty = True

    if INGEST_AVAILABLE:
        sources = build_default_sources(
            fetch_fn=fetch_fn,
            pool=pool,
            window=window,
            extra_queries=P2_SEARCH_QUERIES,
            flags={
                "linkedin": ENABLE_LINKEDIN,
                "kariyer": ENABLE_KARIYER,
                "ashby": ENABLE_ASHBY,
                "remotive": ENABLE_REMOTIVE,
                "wwr": ENABLE_WWR,
                "academictransfer": ENABLE_ACADEMICTRANSFER,
                "indeed_tr": ENABLE_INDEED_TR,
                "_boards": (GREENHOUSE_BOARDS, LEVER_COMPANIES,
                            fetch_greenhouse, fetch_lever, FETCH_WORKERS),
            },
        )
        print(f"\n📥 Toplama başlıyor ({len(sources)} kaynak, P1→P2→P3)...",
              flush=True)
        ingest_res = run_ingestion(
            sources,
            total_budget=RUN_BUDGET_SEC,
            source_budget=SOURCE_BUDGET_SEC,
            max_workers=4,
            verbose=True,
        )
        raw.extend(ingest_res.jobs)
        counts = ingest_res.by_source()
        # Ucuz yol TR portallarında sonuç verdiyse Playwright yedeği gereksiz
        kariyer_empty = counts.get("kariyer.net", 0) == 0
        indeed_tr_empty = counts.get("indeed.tr", 0) == 0
        if ingest_res.failures():
            print(f"   ⚠️  başarısız kaynak: "
                  f"{[o.name for o in ingest_res.failures()]}", flush=True)

    # ── 5. Playwright kaynakları (LinkedIn, Kariyer PW, Indeed PW, Glassdoor) ─
    # Global bütçe aşıldıysa en pahalı aşama atlanır; eldeki ilanlarla devam.
    pw_raw: list[dict] = []
    elapsed = time.monotonic() - run_started
    if elapsed > RUN_BUDGET_SEC:
        print(f"⏱  Çalışma bütçesi doldu ({elapsed:.0f}s > {RUN_BUDGET_SEC}s) — "
              f"Playwright aşaması atlandı", flush=True)
        PW_ENABLED = False
    else:
        PW_ENABLED = True
    if PW_SCRAPERS_AVAILABLE and PW_ENABLED:
        try:
            pw_raw = fetch_all_playwright(
                enable_linkedin=False,   # 2b'de zaten çalıştı
                # Kariyer/Indeed TR: ucuz yol (curl_cffi) sonuç verdiyse
                # Playwright'ı hiç açma — aynı siteyi iki kez taramak proxy
                # bant genişliğini ikiye katlar ve PerimeterX limitini tetikler
                enable_kariyer=ENABLE_KARIYER_PW and kariyer_empty,
                enable_indeed_tr=ENABLE_INDEED_PW and indeed_tr_empty,
                enable_indeed_nl=ENABLE_INDEED_NL,
                enable_glassdoor=ENABLE_GLASSDOOR,
                enable_academic_positions=ENABLE_ACADEMIC_POSITIONS,
                verbose=True,
                max_age_minutes=window,        # kaynak tarafında tazelik filtresi
                extra_queries=P2_SEARCH_QUERIES,  # config/profile.yml P2 sorguları
            )
        except Exception as e:
            print(f"⚠️  Playwright portal hatası: {e}", flush=True)
    raw.extend(pw_raw)

    print(f"\n📦 Toplam ham ilan: {len(raw)}", flush=True)

    # ── 6. Filtre + puanlama ──────────────────────────────────────────────────
    seen:  set[str]   = set()
    ready: list[dict] = []
    blocked_count = 0
    stale_count   = 0
    gate_counts: Counter = Counter()   # US_AUTH / INTERN / GEO sayaçları
    tier_counts: Counter = Counter()   # T1_TR / T2_INTL dağılımı
    gate_rejects: Counter = Counter()  # skor kapısında düşenler
    below_gate: list = []              # (skor, kapı, katman, başlık, konum)
    for job in raw:
        url = job.get("url", "").strip()
        jid = job_id(job)
        if not url or jid in sent_urls or url in sent_urls or jid in seen:
            continue
        seen.add(jid)
        job["id"] = jid
        # Pazar kapısı: ABD + stajyer/öğrenci sıfır tolerans (puanlamadan ÖNCE)
        gate = market_gate(job)
        if gate:
            gate_counts[gate.split("/")[0]] += 1
            continue
        # Scout: block detection on raw_html field if present
        raw_html = job.pop("raw_html", None)
        if raw_html:
            block = detect_block(raw_html)
            if block:
                print(f"🚨 SENTINEL [{block}] {job.get('company','?')} — {url[:60]}", flush=True)
                blocked_count += 1
                continue
        # Scout: tazelik kapısı — bu çalıştırmanın penceresi
        if not is_fresh(job, window):
            stale_count += 1
            continue
        profile = detect_profile(job.get("title", ""))
        if not profile:
            continue
        job["profile"] = profile
        job["score"]   = score_job(job["title"], job.get("location", ""), profile)
        job["age_min"] = job_age_minutes(job) if FRESHNESS_AVAILABLE else None

        # ── Skor kapılı coğrafi katman ──────────────────────────────────────
        # T1 TR > 5.0 | T2 AB/US/AU > 7.0 | T3 kara liste (market_gate'te düştü)
        # Katman kapısı profil eşiğinin YERİNE geçer: ikisi de uygulanırsa
        # daha katı olan zaten katman kapısı oluyor.
        if GEO_GATE_AVAILABLE:
            detail = geo_gate.gate_details(job, job["score"])
            job["market_tier"] = detail["market_tier"]
            tier_counts[detail["market_tier"]] += 1
            if not detail["accepted"]:
                gate_rejects[detail["market_tier"]] += 1
                below_gate.append((job["score"], detail["gate"],
                                   detail["market_tier"], job["title"][:44],
                                   str(job.get("location", ""))[:24]))
                continue
            ready.append(job)
        elif job["score"] >= MIN_SCORE_BY_PROFILE.get(profile, MIN_SCORE):
            ready.append(job)
    if blocked_count:
        print(f"🚨 Sentinel: {blocked_count} ilan altyapı bloğu nedeniyle atlandı", flush=True)
    if stale_count:
        print(f"⏱  Scout: {stale_count} ilan pencere dışında ({window}dk)", flush=True)
    if gate_counts:
        print(f"⛔ Pazar kapısı: {dict(gate_counts)}", flush=True)
    if tier_counts:
        print(f"🌍 Katman: {dict(tier_counts)} | skor kapısında düşen: "
              f"{dict(gate_rejects)}", flush=True)
        if below_gate:
            near = sorted(below_gate, reverse=True)[:5]
            print("   kapıya en yakın elenenler:", flush=True)
            for sc, gt, mt, ti, lo in near:
                print(f"     {sc:4} / {gt}  {mt:9s} {ti:46s} {lo}", flush=True)

    # Profil kotası: P2 ilanları P1 kalabalığına ezdirilmesin
    ready.sort(key=lambda x: x["score"], reverse=True)
    ready = _apply_profile_quota(ready, MAX_PER_RUN)

    # ── 6b. LLM zenginleştirme (opsiyonel) ───────────────────────────────────
    if ENABLE_LLM_ENRICHMENT and GEMINI_API_KEY:
        # Yalnızca en yüksek skorlu ilk N ilan LLM'e gider (free tier koruması)
        batch = ready[:LLM_MAX_JOBS]
        # Model adı çalışma anında katalogdan çözümlenir (sabit değil)
        active_model = (gemini_model.resolve_model(verbose=True)
                        if GEMINI_MODEL_AVAILABLE else "?")
        print(f"🤖 LLM zenginleştirme: {len(batch)}/{len(ready)} ilan "
              f"({active_model})...", flush=True)
        tok_in = tok_out = 0
        for job in batch:
            llm_result = evaluate_job_llm(job)
            if llm_result:
                usage = llm_result.pop("_tokens", None)
                llm_result.pop("_model", None)
                if usage:
                    tok_in  += usage["in"]
                    tok_out += usage["out"]
                job["llm"] = llm_result
        if tok_in or tok_out:
            print(f"   Token: {tok_in} in / {tok_out} out "
                  f"(~{tok_in // max(len(batch), 1)} in/ilan)", flush=True)

    # Kaynak + profil dağılımını göster
    src_dist  = Counter(j.get("source", "?").split("/")[0] for j in ready)
    prof_dist = Counter(j.get("profile", "?") for j in ready)
    print(f"📊 Gönderilecek: {len(ready)} ilan | Profil: {dict(prof_dist)} "
          f"| Kaynak: {dict(src_dist)}", flush=True)

    # ── 7. Telegram'a gönder ──────────────────────────────────────────────────
    broadcast(ready, date_str)

    # ── 8. Arşive kaydet ──────────────────────────────────────────────────────
    if ready:
        save_sent(ready)

    # ── 8b. Coğrafi çözüm önbelleğini diske yaz (T1 katmanı) ────────────────
    if GEO_GATE_AVAILABLE:
        try:
            geo_gate.save_cache()
        except OSError as exc:
            print(f"⚠️  geo-cache yazılamadı: {exc}", flush=True)

    # ── 9. Çalışma durumunu kaydet (bir sonraki pencerenin dayanağı) ─────────
    if RUN_STATE_AVAILABLE:
        record_run(
            sent=len(ready), scanned=len(raw),
            stale=stale_count, blocked=blocked_count, success=True,
        )

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Tamamlandı.", flush=True)


def run_scheduler():
    """Saatlik döngü — erken başvuru avantajı için her saat tarama yapar."""
    import time
    print(f"[scheduler] Saatlik mod aktif (her {SCHEDULE_INTERVAL_HOURS}h). Başlıyor...", flush=True)
    while True:
        try:
            main()
        except Exception as exc:
            print(f"[scheduler] Tarama hatası: {exc}", flush=True)
        next_run = datetime.now() + timedelta(hours=SCHEDULE_INTERVAL_HOURS)
        print(f"[scheduler] Sonraki tarama: {next_run:%H:%M} — bekleniyor...", flush=True)
        time.sleep(SCHEDULE_INTERVAL_HOURS * 3600)


def check_proxy() -> int:
    """
    PREMIUM_PROXY_URL sağlık kontrolü.

    Sırayla doğrular:
      1. Değişken tanımlı ve ayrıştırılabilir mi
      2. Gateway üzerinden çıkış IP'si ve ülkesi (geo hedefleme çalışıyor mu)
      3. Kariyer.net ve Indeed TR gerçekten açılıyor mu (TR exit ile)

    Dönüş: 0 = sağlıklı, 1 = sorunlu. CI'da adım olarak kullanılabilir.
    """
    raw = os.environ.get("PREMIUM_PROXY_URL", "").strip()
    print("═" * 58)
    print("PREMIUM PROXY SAĞLIK KONTROLÜ")
    print("═" * 58)
    if not raw:
        print("✗ PREMIUM_PROXY_URL tanımlı değil.")
        print("  Yerel:  export PREMIUM_PROXY_URL='http://kullanici:parola@gateway:port'")
        print("  CI:     Settings → Secrets and variables → Actions → New secret")
        return 1

    try:
        import net_policy
    except ImportError:
        print("✗ net_policy.py bulunamadı.")
        return 1

    print(f"✓ Değişken tanımlı: {net_policy.mask(raw)}")

    failures = 0
    for cc in ("tr", "nl", None):
        proxy = net_policy.premium_proxy(cc)
        label = (cc or "geo yok").upper()
        if not proxy:
            print(f"✗ {label}: proxy URL üretilemedi (format hatalı)")
            failures += 1
            continue
        try:
            r = requests.get(
                "http://ip-api.com/json/?fields=query,countryCode,city,isp",
                proxies={"http": proxy, "https": proxy},
                timeout=25,
            )
            d = r.json()
            got = d.get("countryCode", "?")
            ok = (cc is None) or (got.lower() == cc)
            print(f"{'✓' if ok else '⚠'} {label:7s} çıkış IP {d.get('query','?'):15s} "
                  f"→ {got} / {d.get('city','?')} ({d.get('isp','?')[:28]})")
            if not ok:
                print(f"    beklenen {cc.upper()}, gelen {got} — geo hedefleme "
                      f"kullanıcı adı sonekiyle çalışmıyor olabilir")
                failures += 1
        except Exception as exc:
            print(f"✗ {label}: bağlantı hatası — {type(exc).__name__}: {str(exc)[:70]}")
            failures += 1

    print("─" * 58)
    print("HEDEF SİTE ERİŞİMİ (TR exit üzerinden)")
    try:
        from cf_scraper import safe_cf_get
        for name, url in (("Kariyer.net", "https://www.kariyer.net/is-ilanlari"),
                          ("Indeed TR", "https://tr.indeed.com/is?q=veri+bilimci")):
            resp = safe_cf_get(url, retries=2, timeout=25, delay=False,
                               verbose=False, country="tr")
            if resp is None:
                print(f"✗ {name:12s} yanıt yok (tüm denemeler başarısız)")
                failures += 1
            else:
                size = len(getattr(resp, "text", "") or "")
                blocked = detect_block(getattr(resp, "text", "") or "")
                flag = "✓" if resp.status_code == 200 and not blocked else "✗"
                print(f"{flag} {name:12s} HTTP {resp.status_code}  {size:>7,} bayt"
                      f"{'  BLOK: ' + blocked if blocked else ''}")
                if resp.status_code != 200 or blocked:
                    failures += 1
    except ImportError:
        print("⚠ cf_scraper.py yok — site erişim testi atlandı")

    print("═" * 58)
    print("SONUÇ: " + ("SAĞLIKLI — proxy kullanıma hazır" if not failures
                       else f"{failures} SORUN — yukarıdaki satırlara bak"))
    return 1 if failures else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="CareerOps Telegram Notifier")
    ap.add_argument(
        "--daemon", action="store_true",
        help=f"Saatlik döngüde çalıştır (her {SCHEDULE_INTERVAL_HOURS}h)",
    )
    ap.add_argument(
        "--check-proxy", action="store_true",
        help="PREMIUM_PROXY_URL sağlık kontrolü yap ve çık",
    )
    args = ap.parse_args()
    if args.check_proxy:
        sys.exit(check_proxy())
    elif args.daemon:
        run_scheduler()
    else:
        main()
