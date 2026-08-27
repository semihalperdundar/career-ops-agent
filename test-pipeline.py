#!/usr/bin/env python3
"""
CareerOps — pipeline regresyon paketi
======================================
Ağ erişimi gerektirmez; saf mantık doğrulaması.

    python test-pipeline.py     # 0 = hepsi geçti, 1 = başarısız var
"""

from __future__ import annotations

import csv
import importlib.util
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("td", ROOT / "telegram-daily.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

import geo_gate as g          # noqa: E402
import run_state              # noqa: E402

now = datetime.now(timezone.utc)
fails: list[str] = []


def chk(cond: bool, label: str) -> None:
    if not cond:
        fails.append(label)
    print(f"{'OK  ' if cond else 'FAIL'} {label}")


def section(name: str) -> None:
    print(f"\n== {name} ==")


section("1 TAZELİK")
for job, exp, lbl in [
    ({"source": "greenhouse/x",
      "posted_at": (now - timedelta(minutes=10)).isoformat()}, True, "10dk GH"),
    ({"source": "greenhouse/x",
      "posted_at": (now - timedelta(minutes=200)).isoformat()}, False, "200dk GH"),
    ({"source": "lever/y",
      "posted_at": str(int((now - timedelta(minutes=30)).timestamp() * 1000))},
     True, "30dk Lever epoch"),
    ({"source": "greenhouse/x"}, False, "damgasız katı kaynak elenir"),
    ({"source": "techcareer"}, True, "damgasız T1 kaynak muaf"),
    ({"source": "linkedin", "posted_at": "45 minutes ago"}, True, "45dk göreli"),
    ({"source": "linkedin", "posted_at": "3 days ago"}, False, "3 gün"),
]:
    chk(m.is_fresh(job, 75) == exp, lbl)

section("2 COĞRAFİ KATMAN")
for loc, cc, tier in [
    ("Istanbul, Türkiye", "TR", g.TIER_DOMESTIC),
    ("Kadıköy", "TR", g.TIER_DOMESTIC),
    ("İzmir", "TR", g.TIER_DOMESTIC),
    ("Amsterdam", "NL", g.TIER_INTL),
    ("2289 Rijswijk", "NL", g.TIER_INTL),
    ("Aachen", "DE", g.TIER_INTL),
    ("New York, NY", "US", g.TIER_INTL),
    ("Sydney", "AU", g.TIER_INTL),
    ("Remote - EMEA", "EU", g.TIER_INTL),
    ("Toronto", "CA", g.TIER_BLOCKED),
    ("Tokyo", "JP", g.TIER_BLOCKED),
    ("Dubai", "AE", g.TIER_BLOCKED),
]:
    v = g.resolve(loc)
    chk(v.cc == cc and v.market_tier == tier, f"{loc} → {cc}/{tier}")

section("3 SKOR KAPISI (kesin >)")
for loc, score, exp in [
    ("Istanbul", 5.1, True), ("Istanbul", 5.0, False), ("Istanbul", 4.9, False),
    ("Amsterdam", 7.1, True), ("Amsterdam", 7.0, False), ("Amsterdam", 6.9, False),
    ("New York, NY", 7.5, True), ("New York, NY", 6.9, False),
    ("Sydney", 7.2, True), ("Toronto", 9.9, False), ("Tokyo", 10.0, False),
]:
    chk(g.is_accepted(loc, score) == exp,
        f"{loc} skor={score} → {'geçer' if exp else 'geçmez'}")

section("4 PAZAR KAPISI (stajyer + kara liste)")
for job, exp, lbl in [
    ({"title": "DS Intern", "location": "Amsterdam"}, "INTERN", "intern"),
    ({"title": "Werkstudent Data", "location": "Berlin"}, "INTERN", "werkstudent"),
    ({"title": "Stajyer Veri Analisti", "location": "İstanbul"}, "INTERN", "stajyer"),
    ({"title": "DS", "location": "Toronto"}, "GEO", "Kanada kara liste"),
    ({"title": "International Data Analyst", "location": "Amsterdam"}, None,
     "'international' yanlış pozitif yok"),
    ({"title": "Veri Bilimci", "location": "İstanbul"}, None, "TR geçer"),
    ({"title": "Data Scientist", "location": "Austin, TX"}, None, "ABD artık geçer"),
]:
    r = m.market_gate(job)
    chk((r.split("/")[0] if r else None) == exp, lbl)

section("5 DEDUP KİMLİĞİ")
b = "https://boards.greenhouse.io/x/jobs/5"
chk(m.job_id({"url": b}) == m.job_id({"url": b + "?utm_source=t&gh_src=a"}),
    "izleme parametresi → aynı")
chk(m.job_id({"url": b}) == m.job_id({"url": b + "/"}), "sondaki slash → aynı")
chk(m.job_id({"url": b + "?jk=1"}) != m.job_id({"url": b + "?jk=2"}),
    "kimlik parametresi → farklı")

section("6 ÇALIŞMA DURUMU")
w, _ = run_state.freshness_window(
    {"last_success_at": (now - timedelta(minutes=60)).isoformat()})
chk(w == 75, "60dk önce → 75dk pencere")
w2, _ = run_state.freshness_window(
    {"last_success_at": (now - timedelta(days=5)).isoformat()})
chk(w2 == run_state.MAX_WINDOW, f"5 gün kesinti → {run_state.MAX_WINDOW}dk tavan")

section("7 PROFİL SINIFLANDIRMA")
for title, exp in [
    ("Junior Data Analyst", "P1"), ("Data Scientist", "P1"),
    ("NLP Engineer", "P1"), ("Senior NLP Engineer", "P2"),
    ("AI Linguist", "P2"), ("Corpus Lead", "P2"), ("Prompt Engineer", "P2"),
    ("Senior Data Scientist", None), ("iOS Developer", None),
]:
    chk(m.detect_profile(title) == exp, f"{title} → {exp}")

section("8 ARŞİV BÜTÜNLÜĞÜ")
archive = ROOT / "data" / "telegram-archive.tsv"
if archive.exists():
    rows = list(csv.DictReader(open(archive, encoding="utf-8", newline=""),
                               delimiter="\t"))
    ids = [m.job_id({"url": r["url"]}) for r in rows]
    chk(len(ids) == len(set(ids)), f"tekrar yok ({len(rows)} satır)")
    blocked = [r for r in rows
               if g.resolve(r["location"]).market_tier == g.TIER_BLOCKED]
    chk(not blocked, f"kara liste bölgesi kalmadı ({len(blocked)} ihlal)")
else:
    print("SKIP arşiv yok")

print(f"\n{'TÜM TESTLER GEÇTİ' if not fails else 'BAŞARISIZ: ' + str(fails)}")
sys.exit(1 if fails else 0)
