#!/usr/bin/env python3
"""
CareerOps — toplama orkestratörü test paketi
=============================================
Ağ erişimi YOK: tüm kazıyıcılar mock'lanır.

    pytest tests/test_ingest.py -v
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ingest import (  # noqa: E402
    P1_LINKEDIN, P2_KARIYER, P3_REST, IngestResult, Source, _normalize,
    build_default_sources, run_ingestion,
)


# ─────────────────────────────────────────────────────────────────────────────
# Yardımcılar
# ─────────────────────────────────────────────────────────────────────────────

def job(title="Data Scientist", url="https://x.test/1", **kw) -> dict:
    return {"title": title, "url": url, **kw}


def recorder():
    """Çağrı sırasını kaydeden bir liste ve kaynak fabrikası döner."""
    calls: list[str] = []

    def make(name: str, jobs=None, delay: float = 0.0, raises=None):
        def fn():
            calls.append(name)
            if delay:
                time.sleep(delay)
            if raises:
                raise raises
            return jobs if jobs is not None else [job(url=f"https://x.test/{name}")]
        return fn

    return calls, make


# ─────────────────────────────────────────────────────────────────────────────
# 1. YÜRÜTME SIRASI — asıl sözleşme
# ─────────────────────────────────────────────────────────────────────────────

def test_linkedin_runs_before_kariyer():
    calls, make = recorder()
    sources = [
        Source("kariyer.net", P2_KARIYER, make("kariyer.net"), concurrent=False),
        Source("linkedin", P1_LINKEDIN, make("linkedin"), concurrent=False),
    ]
    run_ingestion(sources, verbose=False)

    assert calls.index("linkedin") < calls.index("kariyer.net")


def test_priority_order_is_strict_regardless_of_declaration_order():
    calls, make = recorder()
    sources = [
        Source("ashby", P3_REST, make("ashby"), concurrent=False),
        Source("kariyer.net", P2_KARIYER, make("kariyer.net"), concurrent=False),
        Source("remotive", P3_REST, make("remotive"), concurrent=False),
        Source("linkedin", P1_LINKEDIN, make("linkedin"), concurrent=False),
    ]
    run_ingestion(sources, verbose=False)

    assert calls[0] == "linkedin"
    assert calls[1] == "kariyer.net"
    assert set(calls[2:]) == {"ashby", "remotive"}


def test_p3_starts_only_after_p1_and_p2_complete():
    """P3 eş zamanlı olsa bile P1/P2 bitmeden başlamamalı."""
    timeline: list[tuple[str, str]] = []

    def make(name, delay=0.0):
        def fn():
            timeline.append((name, "start"))
            if delay:
                time.sleep(delay)
            timeline.append((name, "end"))
            return [job(url=f"https://x.test/{name}")]
        return fn

    sources = [
        Source("linkedin", P1_LINKEDIN, make("linkedin", 0.05), concurrent=False),
        Source("kariyer.net", P2_KARIYER, make("kariyer.net", 0.05), concurrent=False),
        Source("ashby", P3_REST, make("ashby")),
        Source("remotive", P3_REST, make("remotive")),
    ]
    run_ingestion(sources, verbose=False, max_workers=4)

    def idx(name, phase):
        return timeline.index((name, phase))

    assert idx("linkedin", "end") < idx("kariyer.net", "start")
    assert idx("kariyer.net", "end") < idx("ashby", "start")
    assert idx("kariyer.net", "end") < idx("remotive", "start")


def test_outcome_order_matches_declaration_not_completion():
    """P3 içinde hızlı biten kaynak, tanım sırasını bozmamalı."""
    _, make = recorder()
    sources = [
        Source("linkedin", P1_LINKEDIN, make("linkedin"), concurrent=False),
        Source("slow", P3_REST, make("slow", delay=0.08)),
        Source("fast", P3_REST, make("fast")),
    ]
    res = run_ingestion(sources, verbose=False)

    assert res.order == ["linkedin", "slow", "fast"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. VERİ DEVRİ — tiering katmanına biçim garantisi
# ─────────────────────────────────────────────────────────────────────────────

def test_jobs_are_normalized_for_tiering():
    src = Source(
        "linkedin", P1_LINKEDIN,
        lambda: [{"title": "  AI Linguist ", "url": " https://x.test/9 ",
                  "company": "Acme", "location": "İstanbul"}],
        concurrent=False,
    )
    res = run_ingestion([src], verbose=False)

    assert len(res.jobs) == 1
    j = res.jobs[0]
    assert j["title"] == "AI Linguist"        # kırpılmış
    assert j["url"] == "https://x.test/9"
    assert j["source"] == "linkedin"           # kaynak etiketi enjekte edildi
    assert set(("title", "url", "company", "location", "source",
                "posted_at")).issubset(j)


def test_records_without_title_or_url_are_dropped():
    src = Source("x", P1_LINKEDIN, lambda: [
        {"title": "OK", "url": "https://x.test/1"},
        {"title": "", "url": "https://x.test/2"},      # başlık yok
        {"title": "No URL", "url": ""},                 # url yok
        "kayıt değil",                                  # dict değil
    ], concurrent=False)
    res = run_ingestion([src], verbose=False)

    assert [j["title"] for j in res.jobs] == ["OK"]


def test_existing_source_label_is_preserved():
    src = Source("p3", P3_REST, lambda: [
        {"title": "T", "url": "https://x.test/1", "source": "greenhouse/anthropic"},
    ])
    res = run_ingestion([src], verbose=False)

    assert res.jobs[0]["source"] == "greenhouse/anthropic"


def test_optional_fields_pass_through():
    src = Source("p1", P1_LINKEDIN, lambda: [{
        "title": "T", "url": "https://x.test/1",
        "tags": ["remote"], "description": "<p>jd</p>", "posted_at": "2026-08-27",
    }], concurrent=False)
    res = run_ingestion([src], verbose=False)

    j = res.jobs[0]
    assert j["tags"] == ["remote"]
    assert j["description"] == "<p>jd</p>"
    assert j["posted_at"] == "2026-08-27"


# ─────────────────────────────────────────────────────────────────────────────
# 3. HATA İZOLASYONU
# ─────────────────────────────────────────────────────────────────────────────

def test_failing_source_does_not_stop_later_priorities():
    calls, make = recorder()
    sources = [
        Source("linkedin", P1_LINKEDIN,
               make("linkedin", raises=RuntimeError("boom")), concurrent=False),
        Source("kariyer.net", P2_KARIYER, make("kariyer.net"), concurrent=False),
        Source("ashby", P3_REST, make("ashby")),
    ]
    res = run_ingestion(sources, verbose=False)

    assert calls == ["linkedin", "kariyer.net", "ashby"]
    assert [o.status for o in res.outcomes] == ["error", "ok", "ok"]
    assert "RuntimeError" in res.failures()[0].error
    assert len(res.jobs) == 2       # linkedin'den kayıt yok, diğer ikisi var


def test_disabled_source_is_never_called():
    calls, make = recorder()
    sources = [
        Source("linkedin", P1_LINKEDIN, make("linkedin"), concurrent=False),
        Source("kariyer.net", P2_KARIYER, make("kariyer.net"), enabled=False,
               concurrent=False),
    ]
    res = run_ingestion(sources, verbose=False)

    assert "kariyer.net" not in calls
    assert res.order == ["linkedin"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. BÜTÇE
# ─────────────────────────────────────────────────────────────────────────────

def test_total_budget_skips_lower_priorities_but_never_p1():
    calls, make = recorder()
    sources = [
        Source("linkedin", P1_LINKEDIN, make("linkedin", delay=0.12),
               concurrent=False),
        Source("kariyer.net", P2_KARIYER, make("kariyer.net"), concurrent=False),
        Source("ashby", P3_REST, make("ashby")),
    ]
    res = run_ingestion(sources, total_budget=0.05, verbose=False)

    assert "linkedin" in calls                  # P1 bütçeden muaf
    assert "kariyer.net" not in calls
    assert {o.name: o.status for o in res.outcomes}["kariyer.net"] == "skipped"


def test_source_budget_marks_overrun_without_dropping_data():
    src = Source("slow", P1_LINKEDIN,
                 lambda: (time.sleep(0.08), [job()])[1], concurrent=False)
    res = run_ingestion([src], source_budget=0.01, verbose=False)

    assert res.outcomes[0].status == "budget"
    assert len(res.jobs) == 1       # aşım işaretlenir, veri atılmaz


# ─────────────────────────────────────────────────────────────────────────────
# 5. TIERING KATMANINA DEVİR
# ─────────────────────────────────────────────────────────────────────────────

def test_handoff_into_score_gate():
    """Toplanan kayıtlar doğrudan is_accepted()'a beslenebilmeli."""
    geo = pytest.importorskip("geo_gate")

    sources = [
        Source("linkedin", P1_LINKEDIN, lambda: [
            {"title": "Veri Bilimci", "url": "https://x.test/tr",
             "location": "İstanbul, Türkiye"},
            {"title": "Data Scientist", "url": "https://x.test/nl",
             "location": "Amsterdam"},
            {"title": "Data Scientist", "url": "https://x.test/ca",
             "location": "Toronto"},
        ], concurrent=False),
    ]
    res = run_ingestion(sources, verbose=False)

    verdicts = {j["url"]: geo.resolve(j["location"]).market_tier for j in res.jobs}
    assert verdicts["https://x.test/tr"] == geo.TIER_DOMESTIC
    assert verdicts["https://x.test/nl"] == geo.TIER_INTL
    assert verdicts["https://x.test/ca"] == geo.TIER_BLOCKED

    # T1 kapısı 5.0, T2 kapısı 7.0, T3 her koşulda düşer
    assert geo.is_accepted(res.jobs[0], 5.1) is True
    assert geo.is_accepted(res.jobs[1], 6.9) is False
    assert geo.is_accepted(res.jobs[1], 7.1) is True
    assert geo.is_accepted(res.jobs[2], 9.9) is False


# ─────────────────────────────────────────────────────────────────────────────
# 6. ÜRETİM KAYIT DEFTERİ — mock'lanmış modüllerle
# ─────────────────────────────────────────────────────────────────────────────

def test_build_default_sources_assigns_correct_priorities():
    srcs = {s.name: s for s in build_default_sources(fetch_fn=MagicMock())}

    assert srcs["linkedin"].priority == P1_LINKEDIN
    assert srcs["kariyer.net"].priority == P2_KARIYER
    for name in ("ashby", "remotive", "wwr", "indeed.tr"):
        if name in srcs:
            assert srcs[name].priority == P3_REST

    # P1 ve P2 asla eş zamanlı çalışmamalı
    assert srcs["linkedin"].concurrent is False
    assert srcs["kariyer.net"].concurrent is False


def test_build_default_sources_respects_flags():
    srcs = {s.name: s for s in build_default_sources(
        fetch_fn=MagicMock(), flags={"kariyer": False})}

    assert srcs["kariyer.net"].enabled is False
    assert srcs["linkedin"].enabled is True


def test_linkedin_called_with_window_and_extra_queries():
    with patch("playwright_scrapers.fetch_linkedin") as mock_li:
        mock_li.return_value = [job()]
        srcs = build_default_sources(
            fetch_fn=MagicMock(), window=75, extra_queries=["rlhf", "ai linguist"])
        linkedin = next(s for s in srcs if s.name == "linkedin")
        linkedin.fn()

    assert mock_li.call_args == call(
        verbose=False, max_age_minutes=75, extra_queries=["rlhf", "ai linguist"])


def test_full_order_with_production_registry():
    """Gerçek kayıt defteri, kazıyıcılar mock'lanmış — sıra sözleşmesi."""
    calls: list[str] = []

    def spy(name):
        def fn(*a, **kw):
            calls.append(name)
            return [job(url=f"https://x.test/{name}")]
        return fn

    with patch("playwright_scrapers.fetch_linkedin", side_effect=spy("linkedin")), \
         patch("portal_scrapers.fetch_kariyer", side_effect=spy("kariyer.net")), \
         patch("portal_scrapers.fetch_all_ashby", side_effect=spy("ashby")), \
         patch("portal_scrapers.fetch_remotive", side_effect=spy("remotive")), \
         patch("portal_scrapers.fetch_weworkremotely", side_effect=spy("wwr")), \
         patch("portal_scrapers.fetch_academictransfer", side_effect=spy("at")), \
         patch("portal_scrapers.fetch_indeed_tr", side_effect=spy("indeed.tr")), \
         patch("tr_portals.fetch_techcareer", side_effect=spy("techcareer")), \
         patch("tr_portals.fetch_isinolsun", side_effect=spy("isinolsun")):
        sources = build_default_sources(fetch_fn=MagicMock(), window=60)
        res = run_ingestion(sources, verbose=False)

    assert calls[0] == "linkedin"
    assert calls[1] == "kariyer.net"
    assert len(calls) > 2
    assert res.order[0] == "linkedin"
    assert res.order[1] == "kariyer.net"


# ─────────────────────────────────────────────────────────────────────────────
# 7. SONUÇ NESNESİ
# ─────────────────────────────────────────────────────────────────────────────

def test_result_helpers():
    _, make = recorder()
    sources = [
        Source("linkedin", P1_LINKEDIN, make("linkedin"), concurrent=False),
        Source("bad", P3_REST, make("bad", raises=ValueError("x"))),
    ]
    res = run_ingestion(sources, verbose=False)

    assert isinstance(res, IngestResult)
    assert res.by_source() == {"linkedin": 1, "bad": 0}
    assert [o.name for o in res.failures()] == ["bad"]
    assert res.seconds >= 0


def test_normalize_is_pure():
    src = [{"title": "T", "url": "https://x.test/1"}]
    out = _normalize(src, "s")

    assert out[0] is not src[0]          # kopya döner
    assert "source" not in src[0]        # girdi mutasyona uğramaz
