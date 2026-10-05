#!/usr/bin/env python3
"""
CareerOps — T0 kara liste, saatlik flush ve caveman şeması testleri
====================================================================
Ağ erişimi ve API anahtarı GEREKTİRMEZ.

    pytest tests/test_t0_caveman.py -v
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("td", ROOT / "telegram-daily.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

import evaluator as ev      # noqa: E402
import run_state            # noqa: E402


def reason(job: dict) -> str | None:
    r = m.market_gate(job)
    return r.split("/")[0] if r else None


# ─────────────────────────────────────────────────────────────────────────────
# T0 — gig/anotasyon değirmenleri ve hayalet ilanlar
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("company", [
    "Alignerr", "Outlier", "Outlier AI", "Outlier AI, Inc.", "Crossover",
    "Turing", "turing.com", "Braintrust", "Mercor", "micro1", "Remotasks",
    "Scale AI", "Appen", "TELUS International", "Clickworker", "Toloka",
    "Upwork", "Fiverr", "Toptal", "Andela",
])
def test_scam_company_dropped(company):
    assert reason({"title": "Data Analyst", "company": company,
                   "location": "Amsterdam"}) == "SCAM"


@pytest.mark.parametrize("company", [
    "Turing Institute", "Alan Turing Institute", "Outliers Consulting",
    "Crossover Health Systems", "ING", "Getir", "Booking.com",
])
def test_legitimate_company_with_blacklisted_substring_survives(company):
    """
    Alt-dize taraması meşru kurumları eliyordu. Kara liste AD eşleşmesi
    yapmalı: 'Turing Institute' blacklist'teki 'turing' ile eşleşmemeli.
    """
    assert reason({"title": "Data Scientist", "company": company,
                   "location": "Amsterdam"}) is None


def test_scam_company_in_title_dropped():
    assert reason({"title": "Data Annotation role at Alignerr",
                   "company": "Acme", "location": "Amsterdam"}) == "SCAM"


@pytest.mark.parametrize("title", [
    "Multiple Positions - Data", "Various Roles in Analytics",
    "Talent Pool - Engineering", "General Application",
    "Spontaneous Application", "Genel Başvuru", "Yetenek Havuzu",
])
def test_ghost_posting_dropped(title):
    assert reason({"title": title, "company": "Acme",
                   "location": "Amsterdam"}) == "GHOST"


def test_corp_suffix_normalisation():
    assert m._norm_company("Outlier AI, Inc.") == "outlier"
    assert m._norm_company("Mercor Technologies Ltd") == "mercor"
    assert m._norm_company("Turing Institute") == "turing institute"
    assert m.is_blacklisted_company("Outlier AI") is True
    assert m.is_blacklisted_company("Turing Institute") is False


# ─────────────────────────────────────────────────────────────────────────────
# T0 — çok dilli stajyer varyantları
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    # EN
    "Data Science Intern", "Internship Machine Learning", "Trainee Analyst",
    "Working Student Data", "Graduate Programme Analytics",
    "Summer Analyst - Data", "Placement Year Data Science",
    # DE
    "Werkstudent Data Science", "Praktikum Künstliche Intelligenz",
    "Praktikant Datenanalyse", "Duales Studium Informatik",
    "Ausbildung Fachinformatiker", "Praxissemester Data",
    # NL
    "Stagiair Data Analyse", "Afstudeerstage Machine Learning",
    "Stage Data Analyse", "Stage Business Intelligence", "Meeloopstage Data",
    # FR
    "Alternance Data Analyst", "Apprenti Data Engineer",
    "Stage conventionné data",
    # ES / PT / IT
    "Becario de Datos", "Prácticas Data Science", "Tirocinio Data",
    "Estágio em Dados",
    # TR
    "Stajyer Veri Analisti", "Staj Programı - Veri", "Öğrenci Staj Veri",
    # PL / CZ / SE / NO / FI
    "Praktykant Data", "Stážista Data", "Praktikplats Data",
    "Traineeprogram Data", "Harjoittelija Data Science",
])
def test_intern_variant_dropped(title):
    assert reason({"title": title, "company": "SAP",
                   "location": "Amsterdam"}) == "INTERN"


@pytest.mark.parametrize("title", [
    "Stage Manager Analytics", "Staging Environment Engineer",
    "Stage Lighting Technician", "Stage Production Designer",
    "International Data Analyst", "Internal Audit Data Analyst",
    "Senior Data Scientist", "Student Success Platform Engineer",
])
def test_intern_false_positives_survive(title):
    """
    'stage' ve 'intern' alt-dizeleri meşru ünvanlarda geçer. Bunların
    elenmesi gerçek ilan kaybı demek.
    """
    got = reason({"title": title, "company": "Acme", "location": "Amsterdam"})
    assert got != "INTERN", f"{title} yanlışlıkla stajyer sayıldı ({got})"


# ─────────────────────────────────────────────────────────────────────────────
# Saatlik flush
# ─────────────────────────────────────────────────────────────────────────────

def _window(minutes_ago: int) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    stamp = (now - dt.timedelta(minutes=minutes_ago)).isoformat()
    return run_state.freshness_window({"last_success_at": stamp})[0]


def test_strict_flush_caps_window_on_delayed_run(monkeypatch):
    """
    GitHub cron'u gecikince pencere sınırsız genişlemez. Aksi halde 11
    saatlik yığın tek seferde Telegram'a boşalıyordu.
    """
    monkeypatch.setattr(run_state, "STRICT_HOURLY_FLUSH", True)
    monkeypatch.setattr(run_state, "MAX_WINDOW", 90)

    assert _window(60) == 75          # normal: 60 + 15 tampon
    assert _window(240) == 90         # 4 saat gecikme → tavan
    assert _window(660) == 90         # 11 saat gecikme → tavan


def test_backfill_mode_allows_wide_window(monkeypatch):
    monkeypatch.setattr(run_state, "STRICT_HOURLY_FLUSH", False)
    monkeypatch.setattr(run_state, "MAX_WINDOW", 2880)

    assert _window(660) == 675        # 11 saat + tampon, tavana takılmaz


def test_first_run_uses_min_window():
    assert run_state.freshness_window({"last_success_at": None})[0] == \
        run_state.MIN_WINDOW


# ─────────────────────────────────────────────────────────────────────────────
# Caveman şeması
# ─────────────────────────────────────────────────────────────────────────────

def test_schema_order_and_values_parsed():
    raw = ('{"missing_critical_skills":"no kubernetes",'
           '"core_match_logic":"nlp heavy, cv matches","score":7.8}')
    out = ev.parse_caveman(raw)

    assert out["missing_critical_skills"] == "no kubernetes"
    assert out["core_match_logic"] == "nlp heavy, cv matches"
    assert out["score"] == 7.8
    assert out["key_order_ok"] is True


def test_score_first_flagged_as_contract_violation():
    """Caveman mantığının tüm değeri sırada; ihlal sessiz geçmemeli."""
    raw = ('{"score":9.0,"missing_critical_skills":"none",'
           '"core_match_logic":"exact fit"}')
    assert ev.parse_caveman(raw)["key_order_ok"] is False


def test_verbose_fields_are_clamped():
    raw = ('{"missing_critical_skills":"no scala, no spark, no kafka, no hadoop",'
           '"core_match_logic":"data eng role but candidate is pure research",'
           '"score":4.2}')
    out = ev.parse_caveman(raw)

    assert len(out["missing_critical_skills"].replace(",", " ").split()) <= 3
    assert len(out["core_match_logic"].split()) <= 5


def test_score_clamped_to_range():
    assert ev.parse_caveman('{"a":1,"score":99}')["score"] == 10.0
    assert ev.parse_caveman('{"a":1,"score":-5}')["score"] == 0.0


def test_markdown_fence_tolerated():
    raw = '```json\n{"missing_critical_skills":"none","core_match_logic":"fit","score":8}\n```'
    assert ev.parse_caveman(raw)["score"] == 8.0


@pytest.mark.parametrize("raw", ["", "bozuk çıktı", "{bad json", "null"])
def test_malformed_response_reports_error(raw):
    assert "error" in ev.parse_caveman(raw)


def test_non_numeric_score_reports_error():
    out = ev.parse_caveman('{"missing_critical_skills":"none",'
                           '"core_match_logic":"fit","score":"iyi"}')
    assert "error" in out


def test_prompt_declares_schema_order_and_no_tools():
    p = ev.build_caveman_prompt({
        "title": "Senior NLP Engineer", "company": "Acme",
        "location": "Amsterdam", "description": "<p>Python, PyTorch, k8s</p>",
    })

    # Şema sırası prompt'ta açıkça dayatılmalı
    i_missing = p.index("missing_critical_skills")
    i_logic = p.index("core_match_logic")
    i_score = p.index('"score"')
    assert i_missing < i_logic < i_score
    assert "KEY ORDER IS THE REASONING ORDER" in p
    # HTML temizlenmiş olmalı (token tasarrufu)
    assert "<p>" not in p
    # Transfer kuralları dahil — CV eşleştirmesi bunlara dayanıyor
    assert "Transfer Kuralı" in p


def test_caveman_prompt_much_smaller_than_full_rubric():
    job = {"title": "Data Scientist", "company": "X", "location": "Amsterdam",
           "description": "Python SQL"}
    caveman = len(ev.build_caveman_prompt(job))
    full = len(ev.build_prompt(job))
    assert caveman < full * 0.5, f"caveman {caveman} vs rubrik {full}"
