#!/usr/bin/env python3
"""
CareerOps — dinamik Gemini model çözümleyici testleri
======================================================
Ağ erişimi ve API anahtarı GEREKTİRMEZ: katalog mock'lanır.

    pytest tests/test_gemini_model.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gemini_model as gm  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Her test izole: bellek ve disk önbelleği temiz, env sabit."""
    monkeypatch.setattr(gm, "CACHE_PATH", tmp_path / "model-cache.json")
    monkeypatch.setattr(gm, "_resolved", None)
    monkeypatch.setattr(gm, "_client", MagicMock())
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    yield
    monkeypatch.setattr(gm, "_resolved", None)


def catalog(*names):
    """list_available'ı verilen adlarla mock'lar."""
    return patch.object(gm, "list_available", return_value=list(names))


PRIORITY = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash")


# ─────────────────────────────────────────────────────────────────────────────
# Seçim mantığı
# ─────────────────────────────────────────────────────────────────────────────

def test_picks_highest_priority_available(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", PRIORITY)
    with catalog("gemini-2.5-flash", "gemini-3.6-flash", "gemini-1.5-pro"):
        assert gm.resolve_model(force=True) == "gemini-3.6-flash"


def test_falls_through_priority_when_top_missing(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", PRIORITY)
    with catalog("gemini-2.5-flash", "gemini-1.5-pro"):
        assert gm.resolve_model(force=True) == "gemini-2.5-flash"


def test_prefix_match_accepts_versioned_variant(monkeypatch):
    """'gemini-2.5-flash' → 'gemini-2.5-flash-002' kabul edilmeli."""
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("gemini-2.5-flash",))
    with catalog("gemini-2.5-flash-002", "gemini-1.5-pro"):
        assert gm.resolve_model(force=True) == "gemini-2.5-flash-002"


def test_stable_variant_preferred_over_preview(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("gemini-3.6-flash",))
    with catalog("gemini-3.6-flash-preview-01", "gemini-3.6-flash-002"):
        assert gm.resolve_model(force=True) == "gemini-3.6-flash-002"


def test_family_fallback_when_no_priority_match(monkeypatch):
    """Öncelik listesinin hiçbiri yoksa katalogdaki en yeni flash seçilir."""
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("gemini-9.9-flash",))
    with catalog("gemini-4.0-flash", "gemini-2.0-flash", "gemini-1.5-pro"):
        assert gm.resolve_model(force=True) == "gemini-4.0-flash"


def test_family_fallback_skips_preview(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("nope",))
    with catalog("gemini-5.0-flash-preview", "gemini-4.0-flash"):
        assert gm.resolve_model(force=True) == "gemini-4.0-flash"


def test_any_model_when_no_flash(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("nope",))
    with catalog("gemini-1.5-pro"):
        assert gm.resolve_model(force=True) == "gemini-1.5-pro"


# ─────────────────────────────────────────────────────────────────────────────
# GEMINI_MODEL override
# ─────────────────────────────────────────────────────────────────────────────

def test_env_override_wins_when_present_in_catalog(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", PRIORITY)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-1.5-pro")
    with catalog("gemini-3.6-flash", "gemini-1.5-pro"):
        assert gm.resolve_model(force=True) == "gemini-1.5-pro"


def test_env_override_ignored_when_absent_from_catalog(monkeypatch):
    """Yanlış yazılmış GEMINI_MODEL sessizce 404'e gitmemeli."""
    monkeypatch.setattr(gm, "MODEL_PRIORITY", PRIORITY)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-typo-flash")
    with catalog("gemini-3.6-flash"):
        assert gm.resolve_model(force=True) == "gemini-3.6-flash"


# ─────────────────────────────────────────────────────────────────────────────
# Önbellek
# ─────────────────────────────────────────────────────────────────────────────

def test_catalog_queried_once_then_cached(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", PRIORITY)
    with patch.object(gm, "list_available",
                      return_value=["gemini-3.6-flash"]) as mock_list:
        assert gm.resolve_model(force=True) == "gemini-3.6-flash"
        gm._resolved = None                    # belleği boşalt, diski bırak
        assert gm.resolve_model() == "gemini-3.6-flash"
    assert mock_list.call_count == 1           # ikincisi diskten geldi


def test_cache_invalidated_when_priority_changes(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("gemini-2.5-flash",))
    with catalog("gemini-2.5-flash", "gemini-3.6-flash"):
        assert gm.resolve_model(force=True) == "gemini-2.5-flash"

    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("gemini-3.6-flash",))
    monkeypatch.setattr(gm, "_resolved", None)
    with catalog("gemini-2.5-flash", "gemini-3.6-flash"):
        assert gm.resolve_model() == "gemini-3.6-flash"


def test_invalidate_cache_clears_memory_and_disk(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", PRIORITY)
    with catalog("gemini-3.6-flash"):
        gm.resolve_model(force=True)
    assert gm.CACHE_PATH.exists()

    gm.invalidate_cache()
    assert gm._resolved is None
    assert not gm.CACHE_PATH.exists()


def test_unreachable_catalog_falls_back_to_first_priority(monkeypatch):
    """Ağ/kota hatasında sabitlemek yerine ilk tercih denenir."""
    monkeypatch.setattr(gm, "MODEL_PRIORITY", PRIORITY)
    with catalog():                             # boş liste = katalog alınamadı
        assert gm.resolve_model(force=True) == "gemini-3.6-flash"


# ─────────────────────────────────────────────────────────────────────────────
# 404 kurtarma
# ─────────────────────────────────────────────────────────────────────────────

def test_not_found_triggers_recovery_and_retry(monkeypatch):
    """Önbellekteki model kaldırılmışsa: 404 → yeniden çözümle → tekrar dene."""
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("gemini-3.6-flash",
                                               "gemini-2.5-flash"))
    calls: list[str] = []

    def fake_generate(*, model, contents, config):
        calls.append(model)
        if model == "gemini-3.6-flash":
            raise RuntimeError("404 models/gemini-3.6-flash is not found")
        resp = MagicMock()
        resp.text = '{"ok":true}'
        resp.usage_metadata = MagicMock(prompt_token_count=10,
                                        candidates_token_count=5)
        return resp

    client = MagicMock()
    client.models.generate_content.side_effect = fake_generate
    monkeypatch.setattr(gm, "_client", client)

    seq = [["gemini-3.6-flash", "gemini-2.5-flash"], ["gemini-2.5-flash"]]
    with patch.object(gm, "list_available", side_effect=seq):
        text, usage, model = gm.generate_json("p")

    assert calls == ["gemini-3.6-flash", "gemini-2.5-flash"]
    assert text == '{"ok":true}'
    assert model == "gemini-2.5-flash"


def test_non_404_error_does_not_retry(monkeypatch):
    """Kota/izin hatasında yeniden denemek kotayı daha da yakar."""
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("gemini-3.6-flash",))
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("429 quota")
    monkeypatch.setattr(gm, "_client", client)

    with catalog("gemini-3.6-flash"):
        text, usage, model = gm.generate_json("p")

    assert text is None
    assert client.models.generate_content.call_count == 1


def test_is_not_found_recognises_variants():
    for msg in ("404 NOT_FOUND", "models/x is not found",
                "model does not exist", "is not supported for generateContent"):
        assert gm._is_not_found(RuntimeError(msg)) is True
    assert gm._is_not_found(RuntimeError("429 RESOURCE_EXHAUSTED")) is False


# ─────────────────────────────────────────────────────────────────────────────
# İstek yapılandırması — AFC YOK, JSON modu AÇIK
# ─────────────────────────────────────────────────────────────────────────────

def test_request_uses_json_mode_without_tools(monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PRIORITY", ("gemini-3.6-flash",))
    client = MagicMock()
    resp = MagicMock()
    resp.text = "{}"
    resp.usage_metadata = None
    client.models.generate_content.return_value = resp
    monkeypatch.setattr(gm, "_client", client)

    with catalog("gemini-3.6-flash"):
        gm.generate_json("prompt", max_output_tokens=900)

    cfg = client.models.generate_content.call_args.kwargs["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.thinking_config.thinking_budget == 0
    assert cfg.max_output_tokens == 900
    # AFC reddedildi: araç tanımı gönderilmemeli
    assert not getattr(cfg, "tools", None)
    assert not getattr(cfg, "automatic_function_calling", None)


def test_no_api_key_returns_none(monkeypatch):
    monkeypatch.setattr(gm, "_client", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gm.generate_json("p") == (None, None, None)


# ─────────────────────────────────────────────────────────────────────────────
# Katalog filtreleme
# ─────────────────────────────────────────────────────────────────────────────

def test_list_available_strips_prefix_and_filters_actions(monkeypatch):
    def model(name, actions):
        m = MagicMock()
        m.name = name
        m.supported_actions = actions
        return m

    client = MagicMock()
    client.models.list.return_value = [
        model("models/gemini-3.6-flash", ["generateContent"]),
        model("models/text-embedding-004", ["embedContent"]),
        model("models/gemini-legacy", []),        # boş = eleme yapma
    ]
    monkeypatch.setattr(gm, "_client", client)

    assert gm.list_available() == ["gemini-3.6-flash", "gemini-legacy"]


def test_list_available_survives_api_error(monkeypatch):
    client = MagicMock()
    client.models.list.side_effect = RuntimeError("network down")
    monkeypatch.setattr(gm, "_client", client)

    assert gm.list_available() == []
