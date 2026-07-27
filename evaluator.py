#!/usr/bin/env python3
"""
CareerOps — LLM Job Evaluator
==============================
Herhangi bir LLM API'ye gönderilecek yapılandırılmış, ağırlıklı puanlama
prompt'u üretir. LLM çağrısını kendisi yapmaz — çağrı üst katmanda yapılır.

Değerlendirme ağırlıkları:
  40%  Core Tech Fit      — Temel teknik yetenek eşleşmesi
  30%  Transferable Fit   — Transfer edilebilir/akademik yetenek eşleşmesi
  30%  Seniority/YoE Fit  — Kıdem + deneyim yılı uyumu

Kullanım (modül olarak):
    from evaluator import build_prompt, parse_score

    prompt   = build_prompt(job_dict)
    response = your_llm_api(prompt)          # Claude, GPT-4, Gemini...
    result   = parse_score(response)
    print(result["weighted_score"], result["action"])

Kullanım (CLI):
    python evaluator.py --title "Junior Data Scientist" \\
                        --company "Booking.com" \\
                        --location "Amsterdam, NL" \\
                        --desc description.txt
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    from caveman_compress import compress as _caveman_compress
    _CAVEMAN_AVAILABLE = True
except ImportError:
    _CAVEMAN_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# Aday profili — LLM bağlamı (dosya okumadan hızlı erişim)
# ─────────────────────────────────────────────────────────────────────────────

_TECH_MATRIX = """\
## KEYWORD / TECHNOLOGY MATRIX — Semih Alper Dündar
<!-- LLM: Bu matriksi ÖNCE oku. Teknik yetenek değerlendirmesinin tek kaynağı budur. -->

| Kategori | Expert (üretim/tez düzeyi) | Proficient (proje düzeyi) | Familiar (öz-çalışma) | Transfer Kuralı |
|----------|---------------------------|--------------------------|----------------------|-----------------|
| Diller | Python (5yr), SQL | R | Bash, Julia | R istatistik → Python pandas |
| ML/DL | PyTorch, Scikit-learn | TensorFlow, Keras | JAX | PyTorch → TensorFlow (aynı paradigma, farklı API) |
| NLP/LLM | HuggingFace Transformers, Tokenizers, BERT/GPT fine-tuning | LangChain, RAG temelleri | LlamaIndex, vLLM | HF → herhangi bir LLM framework |
| Türkçe NLP | Mergen TLM tokenizer (4yr katkı), morfolojik analiz, korpus işleme | — | — | Biricik alan uzmanlığı |
| XAI | SHAP, LIME, attention görselleştirme | Captum, integrated gradients | — | XAI araştırması → herhangi bir MLOps explainability aracı |
| Veri Mühendisliği | Pandas, NumPy | PySpark, Spark | Snowflake, dbt, Airflow | Araştırma pipeline → üretim ETL (metodoloji taşınır) |
| Görselleştirme | Power BI (DAX), Matplotlib, Seaborn | Plotly | Tableau | Power BI → Tableau (aynı kavramlar) |
| MLOps | Weights & Biases, Git/GitHub Actions | MLflow, Docker | Kubernetes | W&B → herhangi bir deney takip aracı |
| İstatistik | Faktör Analizi, SEM, Psikometri | Bayesian temel | ARIMA | Psikometri → A/B testi ve nedensellik çıkarımı |
| Veritabanı | PostgreSQL, SQLite | MySQL | MongoDB, Redis | SQL → herhangi bir SQL diyalekti |
"""

_ACADEMIC_TO_INDUSTRY = """\
## AKADEMİK → ENDÜSTRİ ÇEVİRİ TABLOSU
<!-- LLM: Akademik deneyimi değerlendirirken bu KPI çerçeveli versiyonları kullan -->

| Akademik Başarı | Endüstri Eşdeğeri | İş Metriği |
|-----------------|-------------------|-----------|
| XAI tezi (LTCN+SHAP, banka churn) | Finansal risk için yorumlanabilir ML sistemi | ~%40 model opaklık azalması; düzenleyici denetim uyumlu |
| Mergen TLM tokenizer katkısı (4yr) | Üretim NLP sistem mühendisliği | 100K+ kullanıcı; 10M+ token işleme |
| 6 hakemli yayın | Dışarıdan doğrulanmış araştırma çıktıları | Sıfır başarısız hakem incelemesi; indeksli dergilerde yayın |
| WFP Türkiye gıda güvenliği analizi | Politikayı etkileyen veri analizi | 500K+ nüfus; 12 il; kaynak tahsis etkisi |
| Erasmus+ uluslararası proje | Çok işlevli uluslararası proje teslimi | AB destekli (€300K+ eşdeğeri); çok ülkeli ekip |
| Okuma Motivasyonu Ölçeği (MSc) | Psikometrik ölçüm aracı tasarımı | N=400 üzerinde doğrulandı; faktör analizi; Cronbach α>0.85 |
| MSc Veri Bilimi ve Toplum | Uygulamalı ML + Nicel araştırma | İki izli: endüstri ML + nicel metodoloji |
"""

_CANDIDATE_SUMMARY = """\
## ADAY: Semih Alper Dündar

### Seniority (KRİTİK)
- **P1 (Data/AI):** ENDÜSTRİDE JUNIOR — MSc 2025'te tamamlandı, minimal endüstri istihdamı
  - Ancak: PhD düzeyi analitik derinlik → diğer juniorlardan ayırt edici güçlü bir farklılaştırıcı
  - Kabul edilebilir ünvanlar: Junior, Entry-level, Graduate, Associate, Analyst
- **P2 (NLP/R&D):** ARAŞTIRMADA SENIOR — PhD (2021) + 7 yıl araştırma + 6 yayın
  - Kabul edilebilir ünvanlar: Senior, Lead, Specialist, Principal Researcher, Expert

### Eğitim
- PhD Türk Dili, Gazi Üniversitesi (2021)
- MSc Veri Bilimi ve Toplum, Tilburg Üniversitesi (2025)

### Temel Güçler (tüm profiller için)
1. PhD düzeyi analitik derinlik + junior endüstri konumu = nadir kombinasyon
2. 6 hakemli yayın + 2 kitap (çoğu DS adayından farklılaşma)
3. 4+ yıl Mergen Türkçe transformer LLM'e aktif katkı
4. XAI tezi: LTCN+SHAP modeli (banka churn, banking churn yorumlanabilirlik)
5. İkidilli NLP araştırma kapasitesi: Türkçe (ana dil) + İngilizce (C2)

### Aktif VM Projesi — P2 için OTOMATİK AĞIRLIK ARTIŞI
Aday şu anda kişisel VM'de aktif Türkçe LLM fine-tuning projesi yürütmektedir:
- Türkçe instruction tuning + RLHF alignment pipeline'ı
- Düşük kaynaklı Türkçe NLP için özel pre-training ve post-training deneyleri
- Mergen TLM ekibiyle süreklihttps://mergentlm.com altyapı katkısı

**KURAL (Sadece P2 için):** JD aşağıdaki sinyallerden en az birini içeriyorsa
`core_tech_fit` puanını otomatik olarak ≥ 8.5 yap ve `action = "APPLY"`:
- "Turkish LLM", "Turkish language model", "Turkish NLP alignment"
- "Turkish instruction tuning", "low-resource Turkish NLP"
- "Türkçe LLM", "Türkçe dil modeli", "Mergen"
- "Turkish RLHF", "Turkish pre-training", "Turkish fine-tuning"
"""

# ─────────────────────────────────────────────────────────────────────────────
# Puanlama rubriği
# ─────────────────────────────────────────────────────────────────────────────

_SCORING_RUBRIC = """\
## PUANLAMA RUBRİĞİ (Ağırlıklı, 0–10 ölçeği)

### Boyut 1 — Core Tech Fit [AĞIRLIK: %40]
Temel teknik yeterliliklerin (diller, çerçeveler, araçlar) doğrudan eşleşmesi.

| JD Talebi vs Aday | Puan |
|-------------------|------|
| Tam eşleşme — JD'nin ≥70% teknolojisi Expert/Proficient sütununda | 9–10 |
| Güçlü eşleşme — %50-70 eşleşme, kritik gereksinimler karşılandı | 7–8 |
| Kısmi eşleşme — %30-50, temel gereksinimler karşılandı | 5–6 |
| Zayıf eşleşme — <%30, kritik boşluklar var | 2–4 |
| Engel — kritik gereksinim tamamen eksik, transfer yolu yok | 0–1 |

### Boyut 2 — Transferable Skills Fit [AĞIRLIK: %30]
Akademik/bitişik yeteneklerin transfer edilebilirliği — KRİTİK KURAL:

**TRANSFER KURALLARI (bunları kesinlikle uygula):**
- PyTorch biliniyorsa → TensorFlow/Keras gereksinimi boşluk sayma (aynı paradigma)
- HuggingFace Transformers biliniyorsa → herhangi bir LLM framework boşluk sayma
- Araştırma pipeline'ı biliniyorsa → üretim ETL boşluk sayma (metodoloji transfer olur)
- Psikometri/faktör analizi biliniyorsa → A/B testi boşluk sayma (istatistik temeli)
- Power BI biliniyorsa → Tableau boşluk sayma (aynı kavramlar)
- Akademik SQL/analiz biliniyorsa → endüstri BI/analitik boşluk sayma
- Araştırma yayını → proje teslim kanıtı (iş teslimi ile eşdeğer)
- Mergen TLM katkısı → üretim sistem deneyimi (gerçek kullanıcılar, gerçek sistem)

| Transfer Senaryosu | Puan |
|-------------------|------|
| Çok sayıda güçlü transfer + akademik derinlik | 9–10 |
| 2-3 iyi transfer, araştırma deneyimi kanıtlıyor | 7–8 |
| 1-2 transfer, temel boşluklar hafif | 5–6 |
| Transfer az, boşluklar önemli | 2–4 |
| Transfer yok, kritik boşluklar | 0–1 |

### Boyut 3 — Seniority / YoE Fit [AĞIRLIK: %30]
**⚠️ SERT BEKÇI: Bu boyut için aşağıdaki kurallar kesindir.**

**P1 (Junior Data/AI) için:**
| JD Sinyali | Puan |
|-----------|------|
| Açık junior: "Junior", "Entry-level", "Graduate", "Associate", 0-2 YoE | 9–10 |
| Belirtilmemiş kıdem veya 1-2 YoE: herhangi bir kıdem ipucu yok | 7–8 |
| Medior/Orta düzey veya 2-3 YoE: gerilimiyle erişilebilir | 4–5 |
| Açık olarak 4+ YoE gereksinimi | 1–2 |
| "Senior", "Lead", "Principal", "Staff", "Director", "Manager" başlığı | 0 → Eylem: SKIP |
| 5+ YoE açıkça gereksinim | 0 → Eylem: SKIP |

**P2 (Senior R&D/NLP) için — GENİŞ KAPSAM:**
P2 yalnızca "NLP Researcher" değil; aşağıdaki rolleri de kapsar:
- AI Trainer / RLHF Specialist / Red Teamer / AI Evaluator
- Data Annotator / Annotation Lead / Language Data Specialist
- Content Moderator (AI-assisted) / Trust & Safety Researcher
- Post-Training / Pre-Training Engineer (language modeling odaklı)
- Computational Linguist / Corpus Linguist / Speech Researcher
- Turkish/Multilingual Language Specialist (herhangi bir sektörde)

| JD Sinyali | Puan |
|-----------|------|
| Açık kıdemli: "Senior", "Lead", "Principal", Researcher, 5+ YoE | 9–10 |
| Medior veya belirtilmemiş + araştırma/NLP ağırlıklı | 7–8 |
| Junior ama NLP/annotation/language odaklı | 5–6 |
| Junior ve araştırma bağlantısı yok | 2–4 |

### Ağırlıklı Nihai Skor
nihai_skor = (core_tech × 0.40) + (transferable × 0.30) + (seniority × 0.30)

### Eylem Eşikleri
| Ağırlıklı Skor | Eylem |
|---------------|-------|
| 7.5 – 10.0 | APPLY — güçlü uyum, başvur |
| 5.5 – 7.4 | REVIEW — iyi uyum, detaylı inceleme yap |
| 3.5 – 5.4 | STRETCH — gerilim, güçlü motivasyon varsa başvur |
| 0.0 – 3.4 | SKIP — uyum zayıf veya kıdem engeli |
"""

# ─────────────────────────────────────────────────────────────────────────────
# Ana fonksiyon: build_prompt()
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt(
    job: dict,
    include_tech_matrix: bool = True,
    include_academic_table: bool = True,
    language: str = "tr",  # "tr" veya "en"
    compress: bool = False,  # True → caveman_compress ile token tasarrufu
) -> str:
    """
    Ağırlıklı puanlama rubriği içeren LLM değerlendirme prompt'u oluşturur.

    Parametre `job` sözlüğü (tüm alanlar opsiyonel, en az title gerekli):
        {
            "title":       str,   # İş ünvanı (gerekli)
            "company":     str,   # Şirket adı
            "location":    str,   # Konum
            "description": str,   # JD tam metni (varsa daha iyi değerlendirme)
            "url":         str,   # İlan URL'si
            "source":      str,   # Kaynak (linkedin, glassdoor, vb.)
        }

    Dönüş: LLM'e gönderilecek tam prompt string'i.

    LLM çıktısı MUTLAKA şu JSON formatında olmalıdır:
        {
          "profile_detected": "P1" | "P2",
          "scores": {
            "core_tech_fit":      float (0-10),
            "transferable_fit":   float (0-10),
            "seniority_fit":      float (0-10),
            "location_fit":       float (0-10)
          },
          "weighted_score":        float (0-10),
          "seniority_flag":        "JUNIOR_MATCH" | "MEDIOR_STRETCH" | "SENIOR_SKIP" | "P2_MATCH",
          "action":                "APPLY" | "REVIEW" | "STRETCH" | "SKIP",
          "yoe_required":          "0-2" | "2-3" | "3-4" | "4+" | "unclear",
          "blocking_gaps":         [str, ...],
          "transferable_hits":     [str, ...],
          "one_liner":             str,
          "missing_keywords":      [str, ...],   # max 3 — ATS gap analysis
          "outreach_msg":          str,           # "" unless action == "APPLY"
          "company_insight":       str            # 1-2 sentence company brief
        }
    """
    title       = job.get("title", "Belirtilmemiş")
    company     = job.get("company", "Belirtilmemiş")
    location    = job.get("location", "Belirtilmemiş")
    description = job.get("description", "").strip()
    url         = job.get("url", "")

    if description:
        jd_payload = description
        if compress and _CAVEMAN_AVAILABLE:
            # Yalnızca JD payload'ı sıkıştır — static context dokunulmaz
            jd_payload, _ = _caveman_compress(jd_payload, level="lite")
        desc_section = f"\n### İş Tanımı:\n```\n{jd_payload[:2000]}\n```\n"
    else:
        desc_section = "\n### İş Tanımı: (sağlanmadı — başlık ve konuma göre değerlendir)\n"

    url_section = f"\n**URL:** {url}" if url else ""

    profile_section = _CANDIDATE_SUMMARY
    if include_tech_matrix:
        profile_section += "\n\n" + _TECH_MATRIX
    if include_academic_table:
        profile_section += "\n\n" + _ACADEMIC_TO_INDUSTRY

    prompt = f"""\
Sen CareerOps için bir Kıdemli İşe Alım Stratejisti ve ML Değerlendirme Uzmanısın.
Görevin: Aşağıdaki iş ilanını, aday profiline ve ağırlıklı puanlama rubriğine göre hassas biçimde değerlendirmek.

{'─' * 60}
{profile_section}

{'─' * 60}
{_SCORING_RUBRIC}

{'─' * 60}
## DEĞERLENDİRİLECEK İLAN

**Ünvan:** {title}
**Şirket:** {company}
**Konum:** {location}{url_section}
{desc_section}

{'─' * 60}
## GÖREV TALİMATLARI

0. **KONUM TARAFSIZLIĞI (MUTLAK KURAL):**
   - FIT SCORE hesaplanırken konum (ülke, şehir, uzaktan/ofis) KESİNLİKLE görmezden gelinir.
   - "Hollanda'daki Data Scientist" = "ABD'deki Data Scientist" = "Remote Data Scientist" — teknik eşleşme aynıysa skor aynı olmalıdır.
   - `location_fit` alanını JSON'a YALNIZCA BİLGİ OLARAK ekle — weighted_score hesabına DAHIL ETME.
   - Lokasyon avantajı veya dezavantajı yoktur. Sadece içerik (teknik uyum + kıdem) puanlanır.

1. **Önce profili algıla:** JD P1 mi (Data/AI Junior) yoksa P2 mi (NLP/R&D Senior)?
2. **Kıdem kapısını uygula (P1 için):**
   - JD "Senior", "Lead", "Principal", "Staff", "Director" veya "Manager" içeriyorsa
     → seniority_fit = 0, action = "SKIP", durumu kısaca açıkla ve dur.
   - JD açıkça 4+ yıl deneyim gerektiriyorsa
     → seniority_fit = 1-2, action = "SKIP" değerlendir.
3. **Her boyutu ayrı ayrı puana:** core_tech_fit, transferable_fit, seniority_fit
4. **Transfer kurallarını uygula:** PyTorch biliniyorsa TensorFlow boşluk sayma, vb.
5. **Akademik çıktıları iş çıktıları olarak tanı:** yayın = teslim kanıtı, Mergen TLM = üretim deneyimi
6. **Ağırlıklı nihai skoru hesapla:** (tech × 0.40) + (transfer × 0.30) + (seniority × 0.30)
7. **Eylem belirle:** APPLY / REVIEW / STRETCH / SKIP
8. **Eksik Anahtar Kelimeler (ATS Analizi):**
   - JD'de açıkça geçen ancak aday profilinde bulunmayan kritik teknik beceri/araçları belirle.
   - Maksimum 3 öğe. Yalnızca gerçek teknik engel olanları listele — transfer edilebilir boşlukları dahil etme.
   - Örnek: ["Kubernetes", "dbt", "Spark Streaming"]
9. **Soğuk Outreach Taslağı:**
   - YALNIZCA action = "APPLY" ise oluştur. Diğer tüm durumlarda `outreach_msg` = "".
   - Tam olarak 3 cümle (İngilizce): (1) JD'deki spesifik bir teknik soruna/hedefe atıfta bulun, (2) adayın XAI/NLP/akademik geçmişini bu soruna köprüle, (3) güçlü ve net bir harekete geçirici çağrı.
   - Ton: özgüvenli, araştırmacı, spam değil. "I am writing to apply..." ile başlama.
10. **Şirket İstihbarat Özeti:**
    - Şirketin sektör konumu, son ürün odağı veya önemli gelişimi hakkında 1-2 cümle.
    - JD ipuçlarından ve dahili bilginden yararlan. Bilmiyorsan genel kal, spekülasyon yapma.

## ÇIKTI FORMAT GEREKSİNİMLERİ

Yanıtını YALNIZCA aşağıdaki JSON bloğuyla başlat (açıklama SONRAYA):

```json
{{
  "profile_detected": "P1",
  "scores": {{
    "core_tech_fit":    0.0,
    "transferable_fit": 0.0,
    "seniority_fit":    0.0,
    "location_fit":     0.0
  }},
  "weighted_score":    0.0,
  "seniority_flag":    "JUNIOR_MATCH",
  "action":            "APPLY",
  "yoe_required":      "unclear",
  "blocking_gaps":     [],
  "transferable_hits": [],
  "one_liner":         "...",
  "missing_keywords":  [],
  "outreach_msg":      "",
  "company_insight":   "..."
}}
```

JSON'dan sonra 3-5 cümlelik kısa açıklama yaz: en önemli güçler, kritik boşluklar, transfer kazanımları.
"""
    return prompt


# ─────────────────────────────────────────────────────────────────────────────
# parse_score() — LLM yanıtını ayrıştırır
# ─────────────────────────────────────────────────────────────────────────────

def parse_score(response: str) -> dict:
    """
    LLM yanıtından JSON bloğunu çıkarır ve ayrıştırır.

    Dönüş: değerlendirme sözlüğü veya hata durumunda {"error": ..., "raw": ...}
    """
    # JSON kod bloğunu ara
    block = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", response)
    if block:
        json_str = block.group(1)
    else:
        # Blok yoksa ham yanıtta { ... } ara
        match = re.search(r"\{[\s\S]*\}", response)
        json_str = match.group(0) if match else ""

    if not json_str:
        return {"error": "JSON bloğu bulunamadı", "raw": response[:500]}

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return {"error": str(e), "raw": json_str[:500]}

    # Ağırlıklı skoru yeniden hesapla (LLM yanlış hesaplamış olabilir)
    scores = data.get("scores", {})
    tech   = float(scores.get("core_tech_fit",    0))
    trans  = float(scores.get("transferable_fit", 0))
    sen    = float(scores.get("seniority_fit",    0))
    recomputed = round(tech * 0.40 + trans * 0.30 + sen * 0.30, 2)

    data["weighted_score_verified"] = recomputed
    return data


# ─────────────────────────────────────────────────────────────────────────────
# quick_score() — Kural bazlı hızlı ön skor (LLM olmadan)
# ─────────────────────────────────────────────────────────────────────────────

def quick_score(
    title: str,
    location: str = "",
    description: str = "",
) -> dict:
    """
    LLM gerektirmeden başlık + konum + açıklamaya dayalı hızlı kural skor'u.
    Telegram pipeline'ı için ikincil filtre olarak kullanılabilir.

    Dönüş: {"profile": str|None, "score": float, "seniority_flag": str}
    """
    # İçe aktarma burada — döngüsel içe aktarmayı önlemek için
    try:
        from telegram_daily import detect_profile, score_job  # type: ignore
    except ImportError:
        try:
            # telegram-daily.py'yi dinamik olarak yükle (tire nedeniyle)
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "td", Path(__file__).parent / "telegram-daily.py"
            )
            td = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(td)
            detect_profile = td.detect_profile
            score_job      = td.score_job
        except Exception:
            return {"profile": None, "score": 0.0, "seniority_flag": "UNKNOWN"}

    profile = detect_profile(title)
    if not profile:
        return {"profile": None, "score": 0.0, "seniority_flag": "BLOCKED"}

    score = score_job(title, location, profile)

    # Kıdem bayrağı
    t = title.lower()
    medior_kw = ["medior", "mid-level", "mid level", "intermediate"]
    junior_kw = ["junior", "entry", "associate", "graduate", "intern"]

    if profile == "P1":
        if any(kw in t for kw in junior_kw):
            flag = "JUNIOR_MATCH"
        elif any(kw in t for kw in medior_kw):
            flag = "MEDIOR_STRETCH"
        else:
            flag = "UNSPECIFIED"
    else:
        flag = "P2_MATCH"

    return {"profile": profile, "score": score, "seniority_flag": flag}


# ─────────────────────────────────────────────────────────────────────────────
# CLI — geliştirici test arayüzü
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="CareerOps Evaluator — LLM prompt oluşturma ve hızlı skor testi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Örnekler:
  python evaluator.py --title "Junior Data Scientist" --company "Booking.com" --location "Amsterdam"
  python evaluator.py --title "Data Analyst" --desc jd.txt --print-prompt
  python evaluator.py --quick-score --title "Senior ML Engineer" --location "Remote"
        """,
    )
    parser.add_argument("--title",       required=True, help="İş ünvanı")
    parser.add_argument("--company",     default="",    help="Şirket adı")
    parser.add_argument("--location",    default="",    help="Konum")
    parser.add_argument("--desc",        default="",    help="JD metin dosyası veya ham metin")
    parser.add_argument("--url",         default="",    help="İlan URL'si")
    parser.add_argument("--print-prompt",action="store_true", help="Prompt metnini yazdır")
    parser.add_argument("--quick-score", action="store_true", help="LLM'siz hızlı kural skoru")
    args = parser.parse_args()

    # Açıklama kaynağı
    desc = ""
    if args.desc:
        p = Path(args.desc)
        desc = p.read_text(encoding="utf-8") if p.exists() else args.desc

    job = {
        "title":       args.title,
        "company":     args.company,
        "location":    args.location,
        "description": desc,
        "url":         args.url,
    }

    print("=" * 65)
    print("  CareerOps Evaluator")
    print("=" * 65)
    print(f"  Ünvan   : {args.title}")
    print(f"  Şirket  : {args.company or '(belirtilmemiş)'}")
    print(f"  Konum   : {args.location or '(belirtilmemiş)'}")
    print(f"  JD uzun.: {len(desc)} karakter")
    print()

    if args.quick_score:
        result = quick_score(args.title, args.location, desc)
        print("── Hızlı Kural Skoru ──────────────────────────")
        print(f"  Profil         : {result['profile'] or 'REDDEDİLDİ'}")
        print(f"  Skor           : {result['score']:.1f}/10")
        print(f"  Kıdem Bayrağı  : {result['seniority_flag']}")
        if result["profile"] is None:
            print("  → Bu ilan kıdem/olumsuz anahtar kelime filtresiyle elendi.")
    else:
        prompt = build_prompt(job)
        if args.print_prompt:
            print("── Üretilen LLM Prompt'u ───────────────────────")
            print(prompt)
        else:
            print("── Prompt istatistikleri ───────────────────────")
            print(f"  Toplam karakter : {len(prompt):,}")
            print(f"  Tahmini token   : ~{len(prompt)//4:,}")
            print()
            print("Prompt üretildi. LLM'e göndermek için:")
            print("  prompt = build_prompt(job)")
            print("  response = your_llm_api(prompt)")
            print("  result = parse_score(response)")
            print()
            print("Prompt'u görmek için --print-prompt ekle.")
