#!/usr/bin/env python3
"""
CareerOps — sınır bandı koşullu LLM yönlendirme testleri
=========================================================
Ağ erişimi ve API anahtarı GEREKTİRMEZ: llm_fn enjekte edilir.

    pytest tests/test_borderline_gate.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geo_gate as g  # noqa: E402

TR = "Istanbul, Türkiye"      # kapı 5.0 → band [4.0, 5.0)
EU = "Amsterdam"              # kapı 7.0 → band [6.0, 7.0)
US = "Austin, TX"             # kapı 7.0
BLOCKED = "Toronto"           # T3


def llm(score):
    """Sabit skor döndüren sahte caveman; çağrı sayısını da kaydeder."""
    calls = []

    def fn(job):
        calls.append(job)
        return {"missing_critical_skills": "none",
                "core_match_logic": "test", "score": score}
    fn.calls = calls
    return fn


# ─────────────────────────────────────────────────────────────────────────────
# Band sınıflandırma — LLM çağrılmadan
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("loc,score,band", [
    # T1 TR, kapı 5.0
    (TR, 9.0, g.GATE_ACCEPT), (TR, 5.1, g.GATE_ACCEPT), (TR, 5.0, g.GATE_ACCEPT),
    (TR, 4.9, g.GATE_BORDERLINE), (TR, 4.0, g.GATE_BORDERLINE),
    (TR, 3.99, g.GATE_REJECT), (TR, 0.0, g.GATE_REJECT),
    # T2 EU, kapı 7.0
    (EU, 8.0, g.GATE_ACCEPT), (EU, 7.0, g.GATE_ACCEPT),
    (EU, 6.9, g.GATE_BORDERLINE), (EU, 6.0, g.GATE_BORDERLINE),
    (EU, 5.99, g.GATE_REJECT),
    # T2 US aynı kapı
    (US, 7.0, g.GATE_ACCEPT), (US, 6.5, g.GATE_BORDERLINE),
    (US, 5.0, g.GATE_REJECT),
    # T3 her skorda red
    (BLOCKED, 10.0, g.GATE_REJECT), (BLOCKED, 7.5, g.GATE_REJECT),
])
def test_band_boundaries(loc, score, band):
    assert g.classify_score(loc, score)["band"] == band


def test_threshold_is_inclusive():
    """Tam eşik GEÇER: ölçümde en sık reddedilen değer buydu."""
    assert g.classify_score(EU, 7.0)["band"] == g.GATE_ACCEPT
    assert g.classify_score(TR, 5.0)["band"] == g.GATE_ACCEPT


def test_margin_is_configurable(monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_MARGIN", 2.0)
    assert g.classify_score(EU, 5.5)["band"] == g.GATE_BORDERLINE
    assert g.classify_score(EU, 4.9)["band"] == g.GATE_REJECT


# ─────────────────────────────────────────────────────────────────────────────
# Token harcaması — band dışına SIFIR çağrı
# ─────────────────────────────────────────────────────────────────────────────

def test_auto_accept_spends_no_tokens():
    fn = llm(0.0)
    out = g.resolve_gate(EU, 8.5, llm_fn=fn)

    assert out["accepted"] is True
    assert out["reason"] == "static-accept"
    assert fn.calls == []            # LLM hiç çağrılmadı


def test_auto_reject_spends_no_tokens():
    fn = llm(10.0)
    out = g.resolve_gate(EU, 4.0, llm_fn=fn)

    assert out["accepted"] is False
    assert out["reason"] == "static-reject"
    assert fn.calls == []            # yüksek LLM skoru bile çağrılmaz


def test_blocked_tier_spends_no_tokens():
    fn = llm(10.0)
    out = g.resolve_gate(BLOCKED, 9.9, llm_fn=fn)

    assert out["accepted"] is False
    assert out["reason"] == "tier-blocked"
    assert fn.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Sınır bandı — LLM kararı
# ─────────────────────────────────────────────────────────────────────────────

def test_borderline_upgraded_when_llm_meets_gate():
    fn = llm(7.4)
    out = g.resolve_gate(EU, 6.4, llm_fn=fn)

    assert out["accepted"] is True
    assert out["reason"] == "llm-upgrade"
    assert out["llm_score"] == 7.4
    assert len(fn.calls) == 1


def test_borderline_upgraded_on_exact_gate():
    out = g.resolve_gate(EU, 6.4, llm_fn=llm(7.0))
    assert out["accepted"] is True


def test_borderline_rejected_when_llm_below_gate():
    fn = llm(6.9)
    out = g.resolve_gate(EU, 6.8, llm_fn=fn)

    assert out["accepted"] is False
    assert out["reason"] == "llm-reject"
    assert len(fn.calls) == 1


def test_tr_borderline_uses_tr_gate_not_eu_gate():
    """TR kapısı 5.0; 6.0'lık LLM skoru TR'de kabul, AB'de red olmalı."""
    assert g.resolve_gate(TR, 4.5, llm_fn=llm(6.0))["accepted"] is True
    assert g.resolve_gate(EU, 6.5, llm_fn=llm(6.0))["accepted"] is False


def test_llm_result_attached_for_reuse():
    """Kapıda alınan sonuç saklanmalı — 6b aynı ilanı tekrar sormasın."""
    out = g.resolve_gate(EU, 6.5, llm_fn=llm(8.0))
    assert out["llm"]["core_match_logic"] == "test"


def test_job_dict_passed_to_llm():
    fn = llm(8.0)
    job = {"title": "AI Linguist", "location": EU, "description": "jd"}
    g.resolve_gate(job, 6.5, llm_fn=fn)

    assert fn.calls[0]["title"] == "AI Linguist"


# ─────────────────────────────────────────────────────────────────────────────
# Dayanıklılık — LLM yok / hata / bozuk yanıt
# ─────────────────────────────────────────────────────────────────────────────

def test_no_llm_falls_back_to_reject_by_default(monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_FALLBACK", "reject")
    out = g.resolve_gate(EU, 6.5, llm_fn=None)

    assert out["accepted"] is False
    assert out["reason"].startswith("borderline-no-llm")


def test_no_llm_can_fall_back_to_accept(monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_FALLBACK", "accept")
    assert g.resolve_gate(EU, 6.5, llm_fn=None)["accepted"] is True


def test_llm_exception_does_not_propagate(monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_FALLBACK", "reject")

    def boom(job):
        raise RuntimeError("quota")

    out = g.resolve_gate(EU, 6.5, llm_fn=boom)
    assert out["accepted"] is False
    assert "llm-error" in out["reason"]


@pytest.mark.parametrize("bad", [None, {}, {"score": None}, {"score": "iyi"}])
def test_malformed_llm_result_falls_back(bad, monkeypatch):
    monkeypatch.setattr(g, "BORDERLINE_FALLBACK", "reject")
    out = g.resolve_gate(EU, 6.5, llm_fn=lambda job: bad)

    assert out["accepted"] is False
    assert out["reason"].startswith("borderline-llm")


def test_invalid_base_score_rejected():
    out = g.resolve_gate(EU, "abc", llm_fn=llm(9.0))
    assert out["accepted"] is False


# ─────────────────────────────────────────────────────────────────────────────
# is_accepted ile tutarlılık
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("loc,score", [(EU, 7.0), (EU, 8.0), (TR, 5.0)])
def test_is_accepted_agrees_with_static_accept(loc, score):
    assert g.is_accepted(loc, score) is True
    assert g.resolve_gate(loc, score)["accepted"] is True


@pytest.mark.parametrize("loc,score", [(EU, 5.0), (TR, 3.0), (BLOCKED, 9.0)])
def test_is_accepted_agrees_with_static_reject(loc, score):
    assert g.is_accepted(loc, score) is False
    assert g.resolve_gate(loc, score)["accepted"] is False
