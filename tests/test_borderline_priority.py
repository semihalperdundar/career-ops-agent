#!/usr/bin/env python3
"""
CareerOps — sınır bandı önceliklendirme testleri
=================================================
route_borderline() kuyruğu statik skora göre sıralayıp tavan kadarını
LLM'e yönlendirir; kalanı BORDERLINE_FALLBACK'e düşer.

Ağ erişimi ve API anahtarı GEREKTİRMEZ.

    pytest tests/test_borderline_priority.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("td", ROOT / "telegram-daily.py")
td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(td)

import geo_gate as g  # noqa: E402

EU = "Amsterdam"       # kapı 7.0 → band [6.0, 7.0)
TR = "Istanbul"        # kapı 5.0 → band [4.0, 5.0)


def job(score, title=None, location=EU):
    return {
        "title": title or f"job-{score}",
        "url": f"https://x.test/{score}-{location}",
        "location": location,
        "score": score,
        "market_tier": g.resolve(location).market_tier,
        "_gate": g.resolve(location).gate,
    }


def spy_llm(score_map=None, default=9.0):
    """Çağrılan ilanları kaydeden sahte caveman."""
    seen = []

    def fn(j):
        seen.append(j["title"])
        sc = (score_map or {}).get(j["title"], default)
        return {"missing_critical_skills": "none",
                "core_match_logic": "test", "score": sc}
    fn.seen = seen
    return fn


# ─────────────────────────────────────────────────────────────────────────────
# Sıralama ve kesme
# ─────────────────────────────────────────────────────────────────────────────

def test_highest_static_scores_get_the_llm_budget():
    """Eşiğe en yakın ilanlar LLM bütçesini ilk almalı."""
    queue = [job(6.1), job(6.9), job(6.3), job(6.7), job(6.5)]
    fn = spy_llm()

    out = td.route_borderline(queue, budget=2, llm_fn=fn)

    assert fn.seen == ["job-6.9", "job-6.7"]
    assert out["used"] == 2
    assert out["truncated"] == 3


def test_scan_order_does_not_influence_selection():
    """Kuyruk sırası ters çevrilse bile aynı iki ilan seçilmeli."""
    scores = [6.1, 6.9, 6.3, 6.7, 6.5]
    a = spy_llm()
    b = spy_llm()
    td.route_borderline([job(s) for s in scores], budget=2, llm_fn=a)
    td.route_borderline([job(s) for s in reversed(scores)], budget=2, llm_fn=b)

    assert a.seen == b.seen == ["job-6.9", "job-6.7"]


def test_cut_score_is_lowest_evaluated():
    out = td.route_borderline([job(s) for s in (6.1, 6.9, 6.5)],
                              budget=2, llm_fn=spy_llm())
    assert out["cut_score"] == 6.5


def test_no_truncation_when_budget_exceeds_queue():
    fn = spy_llm()
    out = td.route_borderline([job(6.2), job(6.8)], budget=25, llm_fn=fn)

    assert out["used"] == 2
    assert out["truncated"] == 0
    assert out["cut_score"] == 6.2
    assert len(fn.seen) == 2


def test_empty_queue_is_safe():
    out = td.route_borderline([], budget=25, llm_fn=spy_llm())
    assert out == {"accepted": [], "rejected": [], "used": 0, "total": 0,
                   "truncated": 0, "cut_score": None,
                   "reasons": out["reasons"]}
    assert not out["reasons"]


# ─────────────────────────────────────────────────────────────────────────────
# Kesilenler fallback'e düşer
# ─────────────────────────────────────────────────────────────────────────────

def test_truncated_jobs_follow_fallback_reject(monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_FALLBACK", "reject")
    fn = spy_llm(default=9.0)          # LLM'e gidenler kabul edilir

    out = td.route_borderline([job(s) for s in (6.9, 6.8, 6.1, 6.2)],
                              budget=2, llm_fn=fn)

    assert [j["title"] for j in out["accepted"]] == ["job-6.9", "job-6.8"]
    assert {j["title"] for j in out["rejected"]} == {"job-6.1", "job-6.2"}
    assert fn.seen == ["job-6.9", "job-6.8"]   # kesilenlere token harcanmadı


def test_truncated_jobs_follow_fallback_accept(monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_FALLBACK", "accept")
    out = td.route_borderline([job(s) for s in (6.9, 6.1)],
                              budget=1, llm_fn=spy_llm())

    assert len(out["accepted"]) == 2       # biri LLM, biri fallback
    assert out["rejected"] == []


def test_truncated_reasons_are_labelled(monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_FALLBACK", "reject")
    out = td.route_borderline([job(6.9), job(6.1)], budget=1,
                              llm_fn=spy_llm())

    labels = dict(out["reasons"])
    assert any(k.startswith("truncated:") for k in labels), labels


def test_zero_budget_sends_everything_to_fallback():
    """ENABLE_LLM_ENRICHMENT kapalıyken budget=0 gelir."""
    fn = spy_llm()
    out = td.route_borderline([job(6.9), job(6.1)], budget=0, llm_fn=fn)

    assert fn.seen == []
    assert out["used"] == 0
    assert out["truncated"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# LLM kararı kuyruk içinde doğru uygulanır
# ─────────────────────────────────────────────────────────────────────────────

def test_llm_decides_per_job_within_budget():
    fn = spy_llm({"job-6.9": 7.5, "job-6.8": 6.4})   # biri geçer, biri geçmez

    out = td.route_borderline([job(6.9), job(6.8)], budget=2, llm_fn=fn)

    assert [j["title"] for j in out["accepted"]] == ["job-6.9"]
    assert [j["title"] for j in out["rejected"]] == ["job-6.8"]
    assert dict(out["reasons"]) == {"llm-upgrade": 1, "llm-reject": 1}


def test_llm_result_attached_for_enrichment_reuse():
    out = td.route_borderline([job(6.5)], budget=1,
                              llm_fn=spy_llm(default=8.0))
    accepted = out["accepted"][0]

    assert accepted["llm"]["core_match_logic"] == "test"
    assert accepted["llm_score"] == 8.0


def test_tier_specific_gates_respected_in_queue():
    """Aynı kuyrukta TR ve AB ilanları kendi kapılarıyla değerlendirilir."""
    queue = [job(4.5, "tr-job", TR), job(6.5, "eu-job", EU)]
    fn = spy_llm({"tr-job": 5.2, "eu-job": 6.5})      # TR geçer, AB geçmez

    out = td.route_borderline(queue, budget=2, llm_fn=fn)

    assert [j["title"] for j in out["accepted"]] == ["tr-job"]
    assert [j["title"] for j in out["rejected"]] == ["eu-job"]


def test_llm_error_does_not_break_queue(monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_FALLBACK", "reject")
    calls = []

    def flaky(j):
        calls.append(j["title"])
        if j["title"] == "job-6.9":
            raise RuntimeError("quota")
        return {"score": 9.0}

    out = td.route_borderline([job(6.9), job(6.5)], budget=2, llm_fn=flaky)

    assert calls == ["job-6.9", "job-6.5"]     # ilk hata akışı kesmedi
    assert [j["title"] for j in out["accepted"]] == ["job-6.5"]


def test_input_queue_not_mutated_in_place():
    queue = [job(6.1), job(6.9)]
    original = [j["title"] for j in queue]
    td.route_borderline(queue, budget=1, llm_fn=spy_llm())

    assert [j["title"] for j in queue] == original


# ─────────────────────────────────────────────────────────────────────────────
# Üretim tavanı
# ─────────────────────────────────────────────────────────────────────────────

def test_production_ceiling_is_quota_safe():
    """25 x 24 run = 600 istek/gün; 6b ile ~840 < ~1500 günlük limit."""
    assert td.BORDERLINE_LLM_MAX == 25
    assert td.BORDERLINE_LLM_MAX * 24 + td.LLM_MAX_JOBS * 24 < 1500


@pytest.mark.parametrize("budget", [1, 5, 25, 100])
def test_used_never_exceeds_budget(budget):
    fn = spy_llm()
    out = td.route_borderline([job(6.0 + i * 0.01) for i in range(60)],
                              budget=budget, llm_fn=fn)

    assert out["used"] == min(budget, 60)
    assert len(fn.seen) == out["used"]
