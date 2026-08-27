#!/usr/bin/env python3
"""
CareerOps — Coğrafi Beyaz Liste Kapısı (T0-T5 kaskadı)
=======================================================
Kural: YALNIZCA coğrafi Avrupa + Türkiye kabul edilir. Diğer her şey düşer.

Kaskad:
    T0  normalize()      posta kodu / çalışma-modu sarmalayıcı / diakritik
    T1  cache            data/geo-cache.json — kalıcı, O(1)
    T2  static aliases   ülke adları (çok dilli) + büyük şehirler
    T3  gazetteer        ABD eyalet kodu + gömülü şehir tablosu
                         + opsiyonel GeoNames dökümü (data/geonames-cities.tsv)
    T4  LLM              yalnızca artık kuyruk, batch, sonuç cache'lenir
    T5  policy           belirsiz string'ler için ACCEPT/REVIEW/DROP kararı

Verdict üç durumludur; ikili değil:
    ACCEPT  kabul edilen ülke kodu çözüldü
    REVIEW  coğrafya belirsiz ama AB/TR dışı sinyal YOK → puan cezasıyla kabul
    DROP    kabul edilmeyen ülke çözüldü veya AB/TR dışı sinyal var

Kullanım:
    from geo_gate import resolve, ACCEPT, DROP, REVIEW
    v = resolve("2289 Rijswijk")
    v.verdict, v.cc, v.weight      # ("ACCEPT", "NL", 1.5)
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).parent
CACHE_PATH = BASE_DIR / "data" / "geo-cache.json"
GEONAMES_PATH = BASE_DIR / "data" / "geonames-cities.tsv"

ACCEPT = "ACCEPT"
REVIEW = "REVIEW"
DROP = "DROP"

# ── Kabul edilen pazar ────────────────────────────────────────────────────────
# Coğrafi Avrupa (ISO-3166-1 alpha-2). Kıta sınırı coğrafidir: UK, CH, NO, IS,
# RS, UA dahil; TR ayrıca stratejik olarak eklenir (P2 dil verisi ekseni).
EUROPE_GEO: frozenset[str] = frozenset({
    "AL", "AD", "AT", "BA", "BE", "BG", "BY", "CH", "CY", "CZ", "DE", "DK",
    "EE", "ES", "FI", "FO", "FR", "GB", "GG", "GI", "GR", "HR", "HU", "IE",
    "IM", "IS", "IT", "JE", "LI", "LT", "LU", "LV", "MC", "MD", "ME", "MK",
    "MT", "NL", "NO", "PL", "PT", "RO", "RS", "RU", "SE", "SI", "SK", "SM",
    "UA", "VA", "XK",
})

# Coğrafi olarak Avrupa ama pratikte hedeflenmeyen pazarlar. Yaptırım, ödeme
# altyapısı ve uzaktan istihdam kısıtları nedeniyle varsayılan olarak KAPALI.
# Açmak için: EXCLUDED_MARKETS = frozenset()
EXCLUDED_MARKETS: frozenset[str] = frozenset({"RU", "BY"})

# ── Skor kapılı coğrafi katmanlar ────────────────────────────────────────────
# T1 Yurt içi (TR)          : mutlak öncelik, eşik > 5.0
# T2 Seçili uluslararası    : coğrafi Avrupa + US + AU, eşik > 7.0
# T3 Kara liste             : geri kalan her yer → puanlanmadan düşürülür
TIER_DOMESTIC = "T1_TR"
TIER_INTL = "T2_INTL"
TIER_BLOCKED = "T3_BLOCKED"

DOMESTIC_CC: frozenset[str] = frozenset({"TR"})
INTL_CC: frozenset[str] = (EUROPE_GEO | {"US", "AU"}) - EXCLUDED_MARKETS
ACCEPTED_CC: frozenset[str] = DOMESTIC_CC | INTL_CC

# Katman başına skor kapısı — KESİN BÜYÜKTÜR (>), eşitlik geçmez
SCORE_GATE: dict[str, float] = {
    TIER_DOMESTIC: 5.0,
    TIER_INTL: 7.0,
}

# ── Ağırlıklar (score_job'a eklenir) ─────────────────────────────────────────
# TR "mutlak maksimum öncelik" olduğu için en yüksek ağırlığı alır; bu hem
# sıralamayı hem de kendi kapısını (>5.0) geçme olasılığını yükseltir.
W_TR = 2.0        # Türkiye — T1, mutlak öncelik
W_CORE = 1.0      # Avrupa çekirdek pazarları
W_EU_WIDE = 0.8   # ülke belirsiz ama Avrupa kesin (EMEA, DACH, EU remote)
W_INTL_FAR = 0.5  # US / AU — izinli ama teşvik edilmiyor
W_REVIEW = 0.3    # coğrafyası çözülemedi, kabul edilmeyen sinyal yok
CORE_MARKETS: frozenset[str] = frozenset({"NL", "DE", "GB", "BE", "FR", "ES",
                                          "AT", "CH", "IE", "SE", "DK", "NO",
                                          "FI", "IT", "PT", "PL", "LU", "CZ"})


def tier_for_cc(cc: str) -> str:
    """Ülke kodunu katmana eşler. 'EU' = ülke belirsiz Avrupa bloğu."""
    if not cc:
        return TIER_BLOCKED
    cc = cc.upper()
    if cc in DOMESTIC_CC:
        return TIER_DOMESTIC
    if cc == "EU" or cc in INTL_CC:
        return TIER_INTL
    return TIER_BLOCKED


def gate_for_tier(tier: str) -> float:
    """Katmanın geçme eşiği. Bilinmeyen katman → erişilemez eşik."""
    return SCORE_GATE.get(tier, float("inf"))


# ─────────────────────────────────────────────────────────────────────────────
# T0 — Normalizasyon
# ─────────────────────────────────────────────────────────────────────────────

# Çalışma modu sarmalayıcıları (NL/DE/FR/EN)
_WORK_MODE = re.compile(
    r"\b(hybride?\s+werken\s+in|hybrid(?:e)?\s*[-–:]?\s*|op\s+locatie|"
    r"vor\s+ort\s+in|t[eé]l[eé]travail\s+[àa]|remote\s+(?:from|in)|"
    r"on-?site\s*[-–:]?\s*|thuiswerken\s+in)\b",
    re.I,
)
# Recruiter jargonu
_JARGON = re.compile(
    r"\b(greater\s+|.*?\s+metropolitan\s+area|.*?\s+en\s+omgeving|"
    r"raum\s+|.*?\s+und\s+umgebung|.*?\s+ve\s+çevresi)\b",
    re.I,
)
_AREA_SUFFIX = re.compile(r"\s+(area|region|regio|bölgesi|metropolitan area)\b", re.I)
# Baştaki/sondaki posta kodları: "2289 Rijswijk", "London SW1A 1AA", "75008 Paris"
_POSTAL = re.compile(
    r"(^\s*[0-9]{4,6}\s*[A-Z]{0,2}\s+)|(\s+[0-9]{4,6}\s*[A-Z]{0,2}\s*$)|"
    r"(\s+[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}\s*$)",
    re.I,
)
_PAREN = re.compile(r"\((?:hybrid|remote|on-?site|f/m/d|m/w/d|h/f)\)", re.I)
_WS = re.compile(r"\s{2,}")


# Türkçe'ye özgü harfler NFKD ile ayrışmaz: 'ı' ve 'İ' ayrı temel harflerdir,
# birleşen (combining) işaret taşımazlar. Eşlemesiz bırakılırsa "Kadıköy"
# gazetteer'daki "kadikoy" ile eşleşmez.
_TR_MAP = str.maketrans({
    "ı": "i", "İ": "i", "I": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u",
})


def strip_accents(text: str) -> str:
    """Diakritikleri kaldırır — 'München' ve 'Munchen' aynı anahtara düşsün."""
    text = text.translate(_TR_MAP)
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


def normalize(raw: str) -> str:
    """T0: gürültüyü temizler, eşleştirilebilir düşük entropi string'i döner."""
    if not raw:
        return ""
    s = str(raw).strip().lower()
    s = _PAREN.sub(" ", s)
    s = _WORK_MODE.sub(" ", s)
    s = _JARGON.sub(" ", s)
    s = _AREA_SUFFIX.sub(" ", s)
    s = _POSTAL.sub(" ", s)
    s = s.replace("’", "'").replace("–", "-")
    s = _WS.sub(" ", s).strip(" ,;-/|")
    return strip_accents(s)


# ─────────────────────────────────────────────────────────────────────────────
# T2 — Statik takma ad tablosu (ülke + büyük şehir → ISO kodu)
# ─────────────────────────────────────────────────────────────────────────────

_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "NL": ("netherlands", "nederland", "the netherlands", "holland", "dutch"),
    "DE": ("germany", "deutschland", "allemagne", "almanya", "german"),
    "GB": ("united kingdom", "uk", "u.k.", "great britain", "england",
           "scotland", "wales", "northern ireland", "britain"),
    "FR": ("france", "frankrijk", "frankreich", "fransa"),
    "ES": ("spain", "espana", "espagne", "spanien", "ispanya"),
    "IT": ("italy", "italia", "italie", "italien", "italya"),
    "BE": ("belgium", "belgie", "belgique", "belgien", "belcika"),
    "AT": ("austria", "osterreich", "autriche", "avusturya"),
    "CH": ("switzerland", "schweiz", "suisse", "svizzera", "isvicre"),
    "IE": ("ireland", "eire", "irlanda", "irlanda"),
    "PT": ("portugal", "portekiz"),
    "SE": ("sweden", "sverige", "schweden", "isvec"),
    "DK": ("denmark", "danmark", "danemark", "danimarka"),
    "NO": ("norway", "norge", "norwegen", "norvec"),
    "FI": ("finland", "suomi", "finlandiya"),
    "IS": ("iceland", "island", "izlanda"),
    "PL": ("poland", "polska", "polen", "polonya"),
    "CZ": ("czech republic", "czechia", "cesko", "cekya"),
    "SK": ("slovakia", "slovensko", "slovakya"),
    "HU": ("hungary", "magyarorszag", "macaristan"),
    "RO": ("romania", "romanya"),
    "BG": ("bulgaria", "bulgaristan"),
    "GR": ("greece", "hellas", "ellada", "yunanistan"),
    "HR": ("croatia", "hrvatska", "hirvatistan"),
    "SI": ("slovenia", "slovenija", "slovenya"),
    "RS": ("serbia", "srbija", "sirbistan"),
    "BA": ("bosnia", "bosnia and herzegovina", "bosna hersek"),
    "ME": ("montenegro", "karadag"),
    "MK": ("north macedonia", "macedonia", "makedonya"),
    "AL": ("albania", "arnavutluk"),
    "XK": ("kosovo", "kosova"),
    "EE": ("estonia", "eesti", "estonya"),
    "LV": ("latvia", "latvija", "letonya"),
    "LT": ("lithuania", "lietuva", "litvanya"),
    "LU": ("luxembourg", "luxemburg", "luksemburg"),
    "MT": ("malta",),
    "CY": ("cyprus", "kibris"),
    "UA": ("ukraine", "ukrayna"),
    "MD": ("moldova",),
    "TR": ("turkey", "turkiye", "turkey (remote)", "turkish republic", "tur"),
    "RU": ("russia", "russian federation", "rusya"),
    "BY": ("belarus", "belarus'",),
}

# Kabul EDİLMEYEN ülkeler — erken DROP için (tam liste değil, sık görülenler)
_NON_EU_ALIASES: dict[str, tuple[str, ...]] = {
    "US": ("united states", "usa", "u.s.a.", "u.s.", "america", "us"),
    "CA": ("canada", "kanada"),
    "MX": ("mexico",), "BR": ("brazil", "brasil"), "AR": ("argentina",),
    "CL": ("chile",), "CO": ("colombia",), "PE": ("peru",),
    "IN": ("india", "hindistan"), "CN": ("china", "cin"),
    "JP": ("japan", "japonya"), "KR": ("south korea", "korea"),
    "SG": ("singapore", "singapur"), "HK": ("hong kong",),
    "AU": ("australia", "avustralya"), "NZ": ("new zealand",),
    "PH": ("philippines", "filipinler"), "ID": ("indonesia",),
    "MY": ("malaysia",), "TH": ("thailand",), "VN": ("vietnam",),
    "PK": ("pakistan",), "BD": ("bangladesh",), "LK": ("sri lanka",),
    "ZA": ("south africa", "guney afrika"), "NG": ("nigeria",),
    "KE": ("kenya",), "EG": ("egypt", "misir"), "MA": ("morocco", "fas"),
    "AE": ("uae", "united arab emirates", "dubai", "abu dhabi"),
    "SA": ("saudi arabia", "riyadh", "suudi arabistan"),
    "QA": ("qatar", "doha"), "KW": ("kuwait",), "BH": ("bahrain",),
    "OM": ("oman",), "IL": ("israel", "tel aviv"), "JO": ("jordan",),
    "AZ": ("azerbaijan", "azerbaycan"), "GE": ("georgia (country)",),
    "AM": ("armenia",), "KZ": ("kazakhstan",), "UZ": ("uzbekistan",),
}

# Avrupa geneli bloklar — ülke belirsiz ama kıta kesin
_EU_BLOC = (
    "europe", "european", "european union", "eu-wide", "eu wide", "emea",
    "dach", "benelux", "nordic", "nordics", "scandinavia", "baltics", "cee",
    "iberia", "eea", "schengen", "anywhere in europe", "eu remote",
    "europe remote", "remote europe", "remote - europe", "remote (europe)",
    "pan-european", "cet timezone", "cest", "gmt timezone",
)
# Kabul edilmeyen bloklar
_NON_EU_BLOC = (
    "apac", "asia pacific", "latam", "latin america", "mena", "anz",
    "north america", "south america", "africa", "middle east", "gcc",
    "asean", "caribbean", "oceania",
)


def _build_alias_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for cc, names in _COUNTRY_ALIASES.items():
        for n in names:
            idx[strip_accents(n)] = cc
    for cc, names in _NON_EU_ALIASES.items():
        for n in names:
            idx.setdefault(strip_accents(n), cc)
    return idx


_ALIAS_INDEX = _build_alias_index()


# ─────────────────────────────────────────────────────────────────────────────
# T3 — Gazetteer (ABD eyalet kodu + gömülü şehir tablosu + opsiyonel GeoNames)
# ─────────────────────────────────────────────────────────────────────────────

_US_STATES = (
    "al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|"
    "ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|"
    "wa|wv|wi|wy|dc"
)
# ", XX" kalıbı — DE/IN/OR/CA gibi ülke koduyla çakışanlar dahil güvenlidir,
# çünkü virgülden önce ŞEHİR adı bulunması şartı aranır (bkz. _US_STATE_RE).
_US_STATE_RE = re.compile(rf"[a-z]\s*,\s*({_US_STATES})\b")
_CA_PROVINCES = re.compile(r"[a-z]\s*,\s*(on|qc|bc|ab|mb|sk|ns|nb|nl|pe)\b")

# Homonim tablosu: ABD eyalet soneki VARSA ABD kazanır; çıplak ad Avrupa'dır.
_HOMONYMS: frozenset[str] = frozenset({
    "berlin", "paris", "vienna", "athens", "naples", "toledo", "cambridge",
    "birmingham", "manchester", "rome", "milan", "amsterdam", "rotterdam",
    "hamburg", "bremen", "frankfort", "moscow", "warsaw", "prague",
    "odessa", "dublin", "stockholm", "copenhagen", "oslo", "lima",
    "ontario", "georgia", "sydney", "perth", "petersburg",
})

# Gömülü şehir tablosu — statik listenin kaçırdığı Tier-2/3 merkezler.
# GeoNames dökümü mevcutsa (data/geonames-cities.tsv) o tabloyla genişletilir.
_CITIES: dict[str, str] = {}


def _add_cities(cc: str, names: str) -> None:
    for n in names.split():
        _CITIES[n.replace("_", " ")] = cc


_add_cities("NL", """
amsterdam rotterdam den_haag the_hague utrecht eindhoven tilburg groningen
almere breda nijmegen enschede haarlem arnhem amersfoort zaanstad hertogenbosch
den_bosch zwolle leiden maastricht dordrecht ede leeuwarden alkmaar delft
venlo deventer helmond oss hilversum amstelveen roosendaal purmerend schiedam
spijkenisse vlaardingen almelo gouda zoetermeer lelystad hoorn velsen hengelo
apeldoorn haarlemmermeer hoofddorp rijswijk boxtel woerden veenendaal
capelle nieuwegein katwijk heerlen sittard emmen assen middelburg
overijssel gelderland brabant noord-brabant limburg friesland drenthe
flevoland zeeland randstad eindhoven-area
""")
_add_cities("DE", """
berlin munich munchen hamburg cologne koln frankfurt stuttgart dusseldorf
dortmund essen leipzig bremen dresden hannover nuremberg nurnberg duisburg
bochum wuppertal bielefeld bonn munster karlsruhe mannheim augsburg wiesbaden
monchengladbach gelsenkirchen braunschweig chemnitz kiel aachen halle magdeburg
freiburg krefeld lubeck oberhausen erfurt mainz rostock kassel hagen potsdam
saarbrucken ludwigshafen oldenburg osnabruck heidelberg darmstadt regensburg
ingolstadt wurzburg ulm heilbronn jena tubingen konstanz bavaria bayern
nordrhein-westfalen baden-wurttemberg hessen sachsen niedersachsen
""")
_add_cities("GB", """
london manchester birmingham leeds glasgow liverpool bristol sheffield
edinburgh cardiff belfast nottingham newcastle southampton oxford brighton
leicester coventry reading bradford hull plymouth wolverhampton stoke derby
swansea aberdeen portsmouth york dundee cambridge norwich exeter milton_keynes
""")
_add_cities("FR", """
paris marseille lyon toulouse nice nantes montpellier strasbourg bordeaux
lille rennes reims saint-etienne toulon grenoble dijon angers nimes
villeurbanne clermont-ferrand aix-en-provence brest tours amiens limoges
annecy perpignan besancon metz orleans rouen mulhouse caen nancy sophia-antipolis
""")
_add_cities("ES", """
madrid barcelona valencia sevilla zaragoza malaga murcia palma bilbao alicante
cordoba valladolid vigo gijon granada vitoria coruna elche oviedo badalona
cartagena terrassa jerez sabadell mostoles alcala pamplona almeria san_sebastian
santander castellon burgos albacete getafe salamanca logrono catalonia andalusia
""")
_add_cities("IT", """
rome roma milan milano naples napoli turin torino palermo genoa genova bologna
florence firenze bari catania venice venezia verona messina padua padova trieste
brescia parma modena reggio reggio_emilia perugia livorno ravenna cagliari
rimini salerno ferrara sassari latina monza bergamo pescara trento vicenza
""")
_add_cities("BE", """
brussels brussel bruxelles antwerp antwerpen ghent gent charleroi liege
bruges brugge namur leuven mons aalst mechelen la_louviere kortrijk hasselt
sint-niklaas ostend genk wallonia flanders vlaanderen
""")
_add_cities("AT", "vienna wien graz linz salzburg innsbruck klagenfurt villach wels")
_add_cities("CH", """
zurich zurich geneva geneve basel bern lausanne winterthur lucerne luzern
st_gallen lugano biel thun zug
""")
_add_cities("IE", "dublin cork limerick galway waterford drogheda dundalk swords")
_add_cities("PT", "lisbon lisboa porto braga amadora coimbra funchal setubal aveiro")
_add_cities("SE", """
stockholm gothenburg goteborg malmo uppsala vasteras orebro linkoping helsingborg
jonkoping norrkoping lund umea gavle boras eskilstuna sodertalje karlstad lulea
""")
_add_cities("DK", "copenhagen kobenhavn aarhus odense aalborg esbjerg randers kolding horsens vejle roskilde")
_add_cities("NO", "oslo bergen trondheim stavanger drammen fredrikstad kristiansand sandnes tromso")
_add_cities("FI", "helsinki espoo tampere vantaa oulu turku jyvaskyla lahti kuopio pori")
_add_cities("PL", """
warsaw warszawa krakow cracow lodz wroclaw poznan gdansk szczecin bydgoszcz
lublin katowice bialystok gdynia czestochowa radom sosnowiec torun kielce gliwice rzeszow
""")
_add_cities("CZ", "prague praha brno ostrava plzen liberec olomouc budejovice hradec pardubice")
_add_cities("RO", "bucharest bucuresti cluj timisoara iasi constanta craiova brasov galati ploiesti sibiu")
_add_cities("GR", "athens thessaloniki patras heraklion larissa volos piraeus")
_add_cities("HU", "budapest debrecen szeged miskolc pecs gyor")
_add_cities("BG", "sofia plovdiv varna burgas ruse")
_add_cities("HR", "zagreb split rijeka osijek zadar")
_add_cities("RS", "belgrade beograd novi_sad nis kragujevac")
_add_cities("EE", "tallinn tartu narva")
_add_cities("LV", "riga daugavpils liepaja")
_add_cities("LT", "vilnius kaunas klaipeda")
_add_cities("SK", "bratislava kosice presov zilina")
_add_cities("SI", "ljubljana maribor celje")
_add_cities("LU", "luxembourg esch-sur-alzette")
_add_cities("UA", "kyiv kiev lviv kharkiv odesa dnipro")
_add_cities("TR", """
istanbul ankara izmir bursa antalya adana konya gaziantep sanliurfa mersin
diyarbakir kayseri eskisehir samsun denizli sakarya adapazari kahramanmaras
malatya erzurum van batman elazig tekirdag trabzon manisa kocaeli izmit
balikesir aydin mugla bodrum canakkale kutahya sivas ordu tokat corum
levent maslak atasehir kadikoy besiktas sisli umraniye kartal pendik
gebze cayirova umraniye maltepe bagcilar bakirkoy beylikduzu
""")


# Kabul edilmeyen bölgelerin sık görülen şehirleri. Beyaz liste modelinde
# bunlar T5'e düşerse REVIEW olarak sızar; burada kesin DROP'a bağlanır.
# NOT: "perth" bilinçli olarak YOK (Perth, Scotland → GB kaybını önlemek için).
_add_cities("US", """
new_york nyc brooklyn manhattan queens bronx san_francisco seattle boston
chicago austin denver atlanta dallas houston miami phoenix los_angeles
san_diego san_jose palo_alto mountain_view menlo_park sunnyvale redmond
bellevue portland philadelphia pittsburgh detroit minneapolis nashville
charlotte raleigh durham arlington herndon mclean reston bethesda cupertino
santa_clara santa_monica irvine boulder cambridge_ma jersey_city hoboken
fort_worth san_antonio columbus indianapolis kansas_city milwaukee memphis
spokane dayton fort_myers cayce tampa orlando sacramento salt_lake_city
las_vegas cincinnati cleveland st_louis baltimore richmond norfolk
""")
_add_cities("CA", """
toronto vancouver montreal calgary edmonton ottawa winnipeg quebec hamilton
kitchener waterloo mississauga brampton surrey burnaby halifax victoria
""")
_add_cities("IN", """
bangalore bengaluru mumbai delhi new_delhi hyderabad chennai pune kolkata
ahmedabad gurgaon gurugram noida jaipur kochi chandigarh indore coimbatore
""")
_add_cities("AU", "sydney melbourne brisbane adelaide canberra gold_coast newcastle_au")
_add_cities("SG", "singapore")
_add_cities("AE", "dubai abu_dhabi sharjah")
_add_cities("IL", "tel_aviv jerusalem haifa herzliya ramat_gan")
_add_cities("JP", "tokyo osaka kyoto yokohama nagoya fukuoka")
_add_cities("CN", "beijing shanghai shenzhen guangzhou hangzhou chengdu")
_add_cities("HK", "hong_kong kowloon")
_add_cities("KR", "seoul busan incheon")
_add_cities("BR", "sao_paulo rio_de_janeiro brasilia belo_horizonte porto_alegre curitiba")
_add_cities("MX", "mexico_city guadalajara monterrey")
_add_cities("AR", "buenos_aires cordoba_ar")
_add_cities("ZA", "johannesburg cape_town durban pretoria")
_add_cities("NG", "lagos abuja")
_add_cities("KE", "nairobi")
_add_cities("EG", "cairo giza")
_add_cities("PH", "manila cebu makati taguig")
_add_cities("MY", "kuala_lumpur penang")
_add_cities("ID", "jakarta bandung surabaya")
_add_cities("TH", "bangkok phuket")
_add_cities("VN", "hanoi ho_chi_minh saigon")
_add_cities("PK", "karachi lahore islamabad")
_add_cities("NZ", "auckland wellington christchurch")


def _load_geonames() -> None:
    """
    Opsiyonel GeoNames dökümünü yükler (data/geonames-cities.tsv).
    Beklenen sütunlar: name<TAB>country_code  (cities15000 alt kümesi yeter).
    Dosya yoksa sessizce atlanır — gömülü tablo tek başına çalışır.
    """
    if not GEONAMES_PATH.exists():
        return
    try:
        with open(GEONAMES_PATH, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2:
                    continue
                name, cc = strip_accents(parts[0].strip().lower()), parts[1].strip().upper()
                if name and len(cc) == 2:
                    _CITIES.setdefault(name, cc)
    except OSError:
        pass


_load_geonames()


# ─────────────────────────────────────────────────────────────────────────────
# T1 — Kalıcı önbellek
# ─────────────────────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _load_cache() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            _cache = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def save_cache() -> None:
    """Çözüm önbelleğini diske yazar (run sonunda bir kez çağrılır)."""
    with _cache_lock:
        data = _load_cache()
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def cache_put(key: str, cc: str) -> None:
    with _cache_lock:
        _load_cache()[key] = cc


# ─────────────────────────────────────────────────────────────────────────────
# Sonuç tipi
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GeoVerdict:
    verdict: str          # ACCEPT | REVIEW | DROP
    cc: str               # ISO-3166-1 alpha-2, "EU" (ülke belirsiz) veya "XX"
    weight: float         # score_job'a eklenecek coğrafi ağırlık
    tier: str             # çözümü yapan kaskad katmanı (T0..T5)
    reason: str = ""
    normalized: str = field(default="", repr=False)
    market_tier: str = TIER_BLOCKED   # T1_TR | T2_INTL | T3_BLOCKED
    gate: float = float("inf")        # bu ilanın geçmesi gereken skor eşiği


def _verdict_for(cc: str, tier: str, reason: str = "", norm: str = "") -> GeoVerdict:
    """Ülke kodundan karar, ağırlık, pazar katmanı ve skor kapısını türetir."""
    mt = tier_for_cc(cc)
    gate = gate_for_tier(mt)

    if mt == TIER_BLOCKED:
        return GeoVerdict(DROP, cc, 0.0, tier, reason or f"not-accepted:{cc}",
                          norm, TIER_BLOCKED, float("inf"))

    if cc == "TR":
        w = W_TR
    elif cc == "EU":
        w = W_EU_WIDE
    elif cc in ("US", "AU"):
        w = W_INTL_FAR
    elif cc in CORE_MARKETS:
        w = W_CORE
    else:
        w = W_EU_WIDE

    return GeoVerdict(ACCEPT, cc, w, tier, reason or f"tier:{mt}", norm, mt, gate)


# ─────────────────────────────────────────────────────────────────────────────
# Skor kapısı — üretim API'si
# ─────────────────────────────────────────────────────────────────────────────

def is_accepted(location_data, base_score: float) -> bool:
    """
    Katman kurallarına göre ilanın geçip geçmediğini döner.

    Kurallar:
        T1  Türkiye                → base_score > 5.0
        T2  Avrupa / US / AU       → base_score > 7.0
        T3  Diğer her yer          → puanlamaya bakılmaksızın False

    `location_data`: konum string'i veya iş sözlüğü ({"location": ...,
    "title": ..., "description": ..., "tags": [...]}). Sözlük verilirse
    başlık/açıklama bağlam olarak kullanılır (T5 belirsizlik çürütmesi).

    Eşik KESİN büyüktür: tam 5.0 veya tam 7.0 geçmez.
    """
    if isinstance(location_data, dict):
        location = location_data.get("location", "")
        tags = location_data.get("tags") or []
        tags_s = " ".join(map(str, tags)) if isinstance(tags, (list, tuple)) else str(tags)
        context = f"{location_data.get('title', '')} {tags_s} " \
                  f"{str(location_data.get('description', ''))[:400]}"
    else:
        location, context = str(location_data or ""), ""

    verdict = resolve(location, context=context)
    if verdict.verdict == DROP:
        return False
    try:
        return float(base_score) > verdict.gate
    except (TypeError, ValueError):
        return False


def gate_details(location_data, base_score: float) -> dict:
    """is_accepted ile aynı karar, ama loglanabilir gerekçeyle birlikte."""
    loc = (location_data.get("location", "")
           if isinstance(location_data, dict) else str(location_data or ""))
    v = resolve(loc)
    passed = is_accepted(location_data, base_score)
    return {
        "accepted": passed,
        "cc": v.cc,
        "market_tier": v.market_tier,
        "gate": v.gate,
        "score": base_score,
        "weight": v.weight,
        "reason": v.reason if passed else (
            "tier-blocked" if v.market_tier == TIER_BLOCKED else "below-gate"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# T2/T3 — Deterministik çözümleyici
# ─────────────────────────────────────────────────────────────────────────────

_TOKEN_SPLIT = re.compile(r"[,/|;()\[\]]+|\s+-\s+|\s{2,}")
_BARE_REMOTE = re.compile(
    r"^(remote|worldwide|global|anywhere|fully\s+remote|remote\s+first|"
    r"work\s+from\s+home|wfh|distributed|flexible|any\s+location|"
    r"uzaktan|hibrit|thuiswerken)$"
)


def _resolve_deterministic(norm: str) -> GeoVerdict | None:
    """T2 + T3. Çözemezse None döner (T4/T5'e devredilir)."""
    if not norm:
        return None

    # T3a — ABD eyalet / Kanada eyalet soneki: homonimlerden ÖNCE çalışır.
    # Karar _verdict_for'a devredilir: ABD artık T2 (izinli, >7.0 kapısı),
    # Kanada T3 (kara liste). Sabit DROP döndürmek ikisini de aynı sayardı.
    if _US_STATE_RE.search(norm):
        return _verdict_for("US", "T3", "us-state-suffix", norm)
    if _CA_PROVINCES.search(norm):
        return _verdict_for("CA", "T3", "ca-province-suffix", norm)

    # T2a — Kabul edilmeyen bloklar (APAC/LATAM/MENA...)
    for bloc in _NON_EU_BLOC:
        if bloc in norm:
            return GeoVerdict(DROP, "XX", 0.0, "T2", f"non-eu-bloc:{bloc}", norm)

    # T2b — Avrupa blokları
    for bloc in _EU_BLOC:
        if bloc in norm:
            return _verdict_for("EU", "T2", f"eu-bloc:{bloc}", norm)

    tokens = [t.strip(" .-") for t in _TOKEN_SPLIT.split(norm) if t.strip(" .-")]

    # T2c — Ülke adı/kodu (en sağdaki segment en güçlü sinyal)
    for tok in reversed(tokens):
        cc = _ALIAS_INDEX.get(tok)
        if cc:
            return _verdict_for(cc, "T2", f"country-alias:{tok}", norm)

    # T3b — Şehir tablosu. Homonimler yalnızca ABD/CA soneki YOKSA buraya
    # ulaşır (yukarıda elendi), dolayısıyla Avrupa okuması güvenlidir.
    for tok in tokens:
        cc = _CITIES.get(tok)
        if cc:
            return _verdict_for(cc, "T3", f"city:{tok}", norm)

    # T3c — Çok kelimeli şehir adları ("den haag", "san sebastian")
    for cc_name, cc in _CITIES.items():
        if " " in cc_name and cc_name in norm:
            return _verdict_for(cc, "T3", f"city-multi:{cc_name}", norm)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# T5 — Belirsizlik politikası
# ─────────────────────────────────────────────────────────────────────────────

def _apply_t5(norm: str, context: str) -> GeoVerdict:
    """
    Coğrafya çözülemedi. İki alt durum ayrılır:

    a) Çıplak uzaktan çalışma ("Remote", "Worldwide") → REVIEW.
       Fail-closed yapmak AB/TR işçisi kabul eden global remote rolleri
       tamamen kaybettirir; fail-open yapmak APAC/LATAM sızdırır. Orta yol:
       kabul et ama W_REVIEW (0.3) ile puanla — eşiği tek başına geçiremez,
       yalnızca güçlü bir başlık eşleşmesiyle listeye girer.

    b) Hiçbir coğrafi içerik yok (boş, "-", "n/a") → REVIEW, aynı gerekçe.

    Bağlamda (başlık/açıklama/etiket) AB/TR dışı sinyal varsa DROP'a düşer.
    """
    ctx = strip_accents((context or "").lower())
    for bloc in _NON_EU_BLOC:
        if bloc in ctx:
            return GeoVerdict(DROP, "XX", 0.0, "T5", f"context-non-eu:{bloc}", norm)
    for name, cc in _ALIAS_INDEX.items():
        if cc not in ACCEPTED_CC and len(name) > 5 and name in ctx:
            return GeoVerdict(DROP, cc, 0.0, "T5", f"context-country:{name}", norm)

    # Belirsiz coğrafya T2 kapısına (>7.0) tabidir: kabul edilmeyen bir
    # bölgeden gelme ihtimali olduğu için yurt içi eşiğiyle geçirilemez.
    reason = "bare-remote" if (not norm or _BARE_REMOTE.match(norm)) else "unresolved"
    return GeoVerdict(REVIEW, "XX", W_REVIEW, "T5", reason, norm,
                      TIER_INTL, gate_for_tier(TIER_INTL))


# ─────────────────────────────────────────────────────────────────────────────
# Genel API
# ─────────────────────────────────────────────────────────────────────────────

# T4 kancası: batch LLM çözümleyici. main tarafından enjekte edilir.
#   llm_resolver(list[str]) -> dict[str, str]   {normalized: cc}
llm_resolver = None


def resolve(location: str, context: str = "") -> GeoVerdict:
    """
    Tam kaskad. `context` başlık + açıklama + etiketlerin birleşimidir;
    yalnızca T5'te, belirsiz coğrafyayı çürütmek için kullanılır.
    """
    norm = normalize(location)

    cached = _load_cache().get(norm)
    if cached:
        if cached == "XX":
            return _apply_t5(norm, context)
        return _verdict_for(cached, "T1", "cache", norm)

    det = _resolve_deterministic(norm)
    if det is not None:
        cache_put(norm, det.cc)
        return det

    if llm_resolver and norm:
        try:
            got = llm_resolver([norm]) or {}
            cc = str(got.get(norm, "")).upper()
            if len(cc) == 2 or cc == "EU":
                cache_put(norm, cc)
                return _verdict_for(cc, "T4", "llm", norm)
        except Exception:
            pass

    return _apply_t5(norm, context)


def unresolved_keys(locations) -> list[str]:
    """T4 batch'i için: deterministik katmanların çözemediği normalize anahtarlar."""
    out, seen = [], set()
    cache = _load_cache()
    for loc in locations:
        norm = normalize(loc)
        if not norm or norm in seen or norm in cache:
            continue
        seen.add(norm)
        if _resolve_deterministic(norm) is None:
            out.append(norm)
    return out


def stats() -> dict:
    return {
        "cache": len(_load_cache()),
        "cities": len(_CITIES),
        "aliases": len(_ALIAS_INDEX),
        "accepted_cc": len(ACCEPTED_CC),
        "geonames": GEONAMES_PATH.exists(),
    }


if __name__ == "__main__":
    import sys
    samples = sys.argv[1:] or [
        "Aachen", "Uppsala", "2289 Rijswijk", "Greater Enschede Area",
        "Hybride werken in 6827 Arnhem", "Vienna, VA", "Cayce, SC",
        "Santa Clara, CA", "Remote - EMEA", "Istanbul, Türkiye", "Levent",
        "Singapore", "Toronto", "Manila, Manila, Philippines", "Remote",
        "Worldwide", "Overijssel", "Boxtel", "Berlin", "Paris, TX", "Serbia",
    ]
    for s in samples:
        v = resolve(s)
        print(f"{s[:34]:36s} {v.verdict:6s} {v.cc:3s} w={v.weight:<4} "
              f"{v.tier} {v.reason}")
    print(stats())
