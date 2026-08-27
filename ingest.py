#!/usr/bin/env python3
"""
CareerOps — Veri Toplama Orkestratörü
======================================
Kaynakları KESİN öncelik sırasıyla çalıştırır ve çıktıyı Score-Gated
Geo-Tiering katmanına tek biçimli olarak devreder.

    P1  LinkedIn      — ilk, tek başına, bütçe kapısının dışında
    P2  Kariyer.net   — ikinci, tek başına (T1 yurt içi pazarın omurgası)
    P3  Kalan kaynaklar — P1 ve P2 BİTTİKTEN sonra, eş zamanlı

Neden asyncio değil: mevcut kazıyıcılar bloklayan G/Ç kullanıyor
(requests, playwright.sync_api, curl_cffi). asyncio'ya taşımak hepsinin
yeniden yazılmasını gerektirirdi ve Playwright sync API'si bir olay
döngüsünün içinde çalışmaz. Sıralı yürütme + P3 için ThreadPoolExecutor
aynı garantiyi verir: P1 → P2 → P3, kesin sıra.

Tasarım kararları:
  • Bağımlılık enjeksiyonu — her kaynak bir `Source(name, priority, fn)`.
    Test, gerçek ağ katmanını hiç görmeden sırayı doğrulayabilir.
  • Hata izolasyonu — bir kaynağın patlaması diğerlerini durdurmaz.
  • Bütçe — kaynak başına ve toplam süre tavanı; aşılırsa sonraki
    öncelikler atlanır (P1 asla atlanmaz).
  • Deterministik çıktı — kayıtlar `IngestResult` içinde kaynak sırasıyla.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

# Öncelik sabitleri — sayı küçüldükçe önce çalışır
P1_LINKEDIN = 1
P2_KARIYER = 2
P3_REST = 3

JobList = list[dict]
FetchFn = Callable[[], JobList]


@dataclass(frozen=True)
class Source:
    """Tek bir toplama kaynağı."""
    name: str
    priority: int
    fn: FetchFn
    enabled: bool = True
    # P3 içinde eş zamanlı çalışabilir mi (rate-limit hassas kaynaklar False)
    concurrent: bool = True


@dataclass
class SourceOutcome:
    name: str
    priority: int
    count: int = 0
    seconds: float = 0.0
    status: str = "ok"          # ok | error | skipped | budget
    error: str = ""


@dataclass
class IngestResult:
    jobs: JobList = field(default_factory=list)
    outcomes: list[SourceOutcome] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def order(self) -> list[str]:
        """Kaynakların gerçekte çalıştırıldığı sıra — testin doğruladığı şey."""
        return [o.name for o in self.outcomes]

    def by_source(self) -> dict[str, int]:
        return {o.name: o.count for o in self.outcomes}

    def failures(self) -> list[SourceOutcome]:
        return [o for o in self.outcomes if o.status == "error"]


def _normalize(jobs: Iterable[dict], source: str) -> JobList:
    """
    Tiering katmanının beklediği tek biçimli kayda çevirir.

    Zorunlu alanlar: title, url. Eksikse kayıt düşer — aşağı akıştaki
    puanlama ve dedup bu ikisi olmadan anlamsız.
    """
    out: JobList = []
    for j in jobs or []:
        if not isinstance(j, dict):
            continue
        title = str(j.get("title") or "").strip()
        url = str(j.get("url") or "").strip()
        if not title or not url:
            continue
        out.append({
            "title": title,
            "url": url,
            "company": str(j.get("company") or "").strip(),
            "location": str(j.get("location") or "").strip(),
            "source": str(j.get("source") or source),
            "posted_at": j.get("posted_at") or "",
            **({"tags": j["tags"]} if j.get("tags") else {}),
            **({"description": j["description"]} if j.get("description") else {}),
        })
    return out


def _run_one(src: Source, budget: float | None) -> tuple[SourceOutcome, JobList]:
    """Tek kaynağı çalıştırır; istisnayı yutar ve sonuca yazar."""
    started = time.monotonic()
    try:
        jobs = _normalize(src.fn() or [], src.name)
        elapsed = time.monotonic() - started
        status = "budget" if (budget and elapsed > budget) else "ok"
        return SourceOutcome(src.name, src.priority, len(jobs), elapsed, status), jobs
    except Exception as exc:  # kaynak hatası akışı durdurmaz
        elapsed = time.monotonic() - started
        return (SourceOutcome(src.name, src.priority, 0, elapsed, "error",
                              f"{type(exc).__name__}: {exc}"), [])


def run_ingestion(
    sources: Sequence[Source],
    total_budget: float | None = None,
    source_budget: float | None = None,
    max_workers: int = 4,
    verbose: bool = True,
) -> IngestResult:
    """
    Kaynakları önceliğe göre çalıştırır ve birleşik sonucu döner.

    Sıra garantisi:
      1. P1 kaynakları, tanımlandıkları sırayla, TEK TEK
      2. P2 kaynakları, tanımlandıkları sırayla, TEK TEK
      3. P3 kaynakları — P1 ve P2 tamamen bittikten SONRA, eş zamanlı
         (concurrent=False olanlar P3 içinde yine sıralı çalışır)

    `total_budget` aşılırsa P1 DIŞINDAKİ kalan kaynaklar "skipped" işaretlenir.
    """
    result = IngestResult()
    started = time.monotonic()

    def budget_left() -> bool:
        return total_budget is None or (time.monotonic() - started) < total_budget

    active = [s for s in sources if s.enabled]
    by_priority: dict[int, list[Source]] = {}
    for s in active:
        by_priority.setdefault(s.priority, []).append(s)

    for priority in sorted(by_priority):
        group = by_priority[priority]

        # P1 bütçeden muaf: en yüksek verimli kaynak asla atlanmaz
        if priority != P1_LINKEDIN and not budget_left():
            for s in group:
                result.outcomes.append(
                    SourceOutcome(s.name, s.priority, 0, 0.0, "skipped",
                                  "toplam bütçe aşıldı"))
            if verbose:
                print(f"   ⏱  P{priority}: toplam bütçe aşıldı — atlandı",
                      flush=True)
            continue

        sequential = [s for s in group if not s.concurrent]
        parallel = [s for s in group if s.concurrent]

        # P1 ve P2 her zaman sıralı: tek kaynak, deterministik sıra
        if priority in (P1_LINKEDIN, P2_KARIYER):
            sequential, parallel = group, []

        for src in sequential:
            outcome, jobs = _run_one(src, source_budget)
            result.outcomes.append(outcome)
            result.jobs.extend(jobs)
            if verbose:
                _log(outcome)

        if parallel:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {ex.submit(_run_one, s, source_budget): s for s in parallel}
                # Tamamlanma sırası değil, TANIM sırası korunur — çıktı
                # deterministik olmalı ki testler ve loglar kararlı kalsın
                done: dict[str, tuple[SourceOutcome, JobList]] = {}
                for fut in as_completed(futs):
                    src = futs[fut]
                    try:
                        done[src.name] = fut.result()
                    except Exception as exc:
                        done[src.name] = (
                            SourceOutcome(src.name, src.priority, 0, 0.0,
                                          "error", str(exc)), [])
                for src in parallel:
                    outcome, jobs = done[src.name]
                    result.outcomes.append(outcome)
                    result.jobs.extend(jobs)
                    if verbose:
                        _log(outcome)

    result.seconds = time.monotonic() - started
    if verbose:
        print(f"   📦 Toplama tamamlandı: {len(result.jobs)} ilan "
              f"({result.seconds:.0f}s)", flush=True)
    return result


def _log(o: SourceOutcome) -> None:
    icon = {"ok": "✓", "error": "✗", "skipped": "⤼", "budget": "⏱"}[o.status]
    tail = f" — {o.error}" if o.error else ""
    print(f"   {icon} P{o.priority} {o.name}: {o.count} ilan "
          f"({o.seconds:.0f}s){tail}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Üretim kaynak kaydı
# ─────────────────────────────────────────────────────────────────────────────

def build_default_sources(
    fetch_fn,
    pool=None,
    window: int | None = None,
    extra_queries: Sequence[str] = (),
    flags: dict | None = None,
) -> list[Source]:
    """
    Gerçek kazıyıcıları öncelik sırasına bağlar.

    İçe aktarmalar fonksiyon gövdesinde: orkestratör modülü kazıyıcılar
    kurulu olmadan da (testte) yüklenebilir kalır.
    """
    flags = flags or {}
    srcs: list[Source] = []

    # ── P1: LinkedIn ────────────────────────────────────────────────────────
    try:
        from playwright_scrapers import fetch_linkedin

        srcs.append(Source(
            name="linkedin",
            priority=P1_LINKEDIN,
            fn=lambda: fetch_linkedin(verbose=False, max_age_minutes=window,
                                      extra_queries=list(extra_queries)),
            enabled=flags.get("linkedin", True),
            concurrent=False,
        ))
    except ImportError:
        pass

    # ── P2: Kariyer.net ─────────────────────────────────────────────────────
    try:
        from portal_scrapers import fetch_kariyer

        srcs.append(Source(
            name="kariyer.net",
            priority=P2_KARIYER,
            fn=lambda: fetch_kariyer(fetch_fn),
            enabled=flags.get("kariyer", True),
            concurrent=False,
        ))
    except ImportError:
        pass

    # ── P3: Kalan kaynaklar ─────────────────────────────────────────────────
    # Greenhouse + Lever: 78 board'a paralel fan-out; orkestratör açısından
    # tek kaynak gibi davranır (iç eş zamanlılık kendi içinde yönetilir).
    boards = flags.get("_boards")
    if boards:
        gh_boards, lever_boards, gh_fn, lever_fn, workers = boards

        def _fetch_boards() -> JobList:
            out: JobList = []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = (
                    [ex.submit(gh_fn, s, n, pool) for s, n in gh_boards] +
                    [ex.submit(lever_fn, s, n, pool) for s, n in lever_boards]
                )
                for fut in as_completed(futs):
                    try:
                        out.extend(fut.result() or [])
                    except Exception:
                        pass
            return out

        srcs.append(Source("greenhouse+lever", P3_REST, _fetch_boards,
                           flags.get("boards", True)))

    try:
        from portal_scrapers import (fetch_academictransfer, fetch_all_ashby,
                                     fetch_indeed_tr, fetch_remotive,
                                     fetch_weworkremotely)

        srcs += [
            Source("ashby", P3_REST, lambda: fetch_all_ashby(fetch_fn),
                   flags.get("ashby", True)),
            Source("remotive", P3_REST, lambda: fetch_remotive(fetch_fn),
                   flags.get("remotive", True)),
            Source("wwr", P3_REST, lambda: fetch_weworkremotely(fetch_fn),
                   flags.get("wwr", True)),
            Source("academictransfer", P3_REST,
                   lambda: fetch_academictransfer(fetch_fn),
                   flags.get("academictransfer", True)),
            # Indeed TR rate-limit hassas → P3 içinde sıralı
            Source("indeed.tr", P3_REST, lambda: fetch_indeed_tr(fetch_fn),
                   flags.get("indeed_tr", True), concurrent=False),
        ]
    except ImportError:
        pass

    try:
        from tr_portals import fetch_isinolsun, fetch_techcareer

        srcs += [
            Source("techcareer", P3_REST,
                   lambda: fetch_techcareer(fetch_fn, verbose=False),
                   flags.get("techcareer", True)),
            Source("isinolsun", P3_REST,
                   lambda: fetch_isinolsun(fetch_fn, verbose=False),
                   flags.get("isinolsun", True)),
        ]
    except ImportError:
        pass

    return srcs
