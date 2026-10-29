#!/usr/bin/env python3
"""
ovrhovoditelj.py — detekcija ovrhovoditelja iz FINA Javne objave.

Za dražbe koje prođu filtre (lokacija/vrsta/vrijednost) skine PDF
"Poziv na sudjelovanje u elektroničkoj javnoj dražbi" s Javne objave,
parsira blok ovrhovoditelja (naziv + OIB) i klasificira ga prema listi
poznatih institucionalnih vjerovnika (ovrhovoditelji_oib.csv).

Tehnika dohvata (vidi docs/korak0 nalaze u PR opisu):
- Očevidnik daje uuid predmeta prodaje (JSON, bez zaštite)
- Javna objava: GET /pretrazivanje/pismena?pp={uuid} radi BEZ captche i
  sprema kriterij u sesiju; tablicu puni POST /pretrazivanje/datatable
  (čisti JSON s idPismena/idDms/sifVrPism/idNdmtList)
- PDF download ide kroz nevidljivu reCAPTCHA-u koju izvršava stranica
  sama u pravom browseru → zato Playwright/Chromium, klik kao korisnik.
  NE koristimo captcha solvere: ako FINA prikaže vidljivi izazov,
  download se preskače i dražba dobije kategoriju "nepoznato".

PRIVATNOST: repo i GHA logovi su JAVNI. U logove se smiju pisati samo
kategorije i ID-evi dražbi — NIKAD imena ni OIB-i fizičkih osoba.
Detalji idu samo u state (data/ovrh/, gitignoran) i privatni email.
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("ovrhovoditelj")

BASE_DIR = Path(os.environ.get("PONIP_BASE", Path(__file__).resolve().parent))
REGISTRY_CSV = BASE_DIR / "ovrhovoditelji_oib.csv"
STATE_DIR = BASE_DIR / "data" / "ovrh"
STATE_DIR.mkdir(parents=True, exist_ok=True)

JO_BASE = "https://ponip.fina.hr/javnaobjava-web"
PP_URL_FMT = JO_BASE + "/pretrazivanje/pismena?pp={uuid}"

# Vrste pismena (sifVrPism) koje sadrže stranke postupka na prvoj stranici.
# 25 = Poziv na sudjelovanje u EJD (ovršni postupak) — primarni izvor.
# 59 = Poziv na sudjelovanje u EJD (stečajni postupak) — stečajni dužnik.
# Fallback po nazivu pokriva eventualne nove šifre.
POZIV_SIFRE = (25, 59)
POZIV_NAZIV_PREFIX = "Poziv na sudjelovanje"

RETRY_FAILED_AFTER_DAYS = 2   # ponovi download/parse ako prošli pokušaj nije uspio
REQUEST_PAUSE_S = 1.0         # pristojnost: max ~1 zahtjev u sekundi

VALJANE_KATEGORIJE = {
    "agencija", "banka", "banka_ugasena", "drzava",
    "firma_ostalo", "privatna_osoba", "nepoznato",
}

# EOS nekretnine: nije vjerovnik nego EOS-ova firma koja KUPUJE na dražbama —
# klasificira se kao agencija, ali je u logovima zasebno označavamo.
EOS_NEKRETNINE_OIB = "31075247773"


# ============================================================================
# OIB VALIDACIJA — ISO 7064 MOD 11,10
# ============================================================================

def valid_oib(oib: str) -> bool:
    if not re.fullmatch(r"\d{11}", oib or ""):
        return False
    a = 10
    for d in oib[:10]:
        a = (a + int(d)) % 10
        a = ((a or 10) * 2) % 11
    return (11 - a) % 10 == int(oib[10])


# ============================================================================
# REGISTAR POZNATIH VJEROVNIKA
# ============================================================================

def load_registry(path: Path = REGISTRY_CSV) -> dict[str, dict]:
    """Učita ovrhovoditelji_oib.csv → {oib: {kategorija, naziv, ...}}."""
    registry: dict[str, dict] = {}
    if not path.exists():
        log.error(f"Registar ne postoji: {path}")
        return registry
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            oib = (row.get("oib") or "").strip()
            if not valid_oib(oib):
                log.warning(f"Registar: preskačem neispravan OIB {oib!r} ({row.get('naziv')})")
                continue
            registry[oib] = {
                "kategorija": (row.get("kategorija") or "").strip(),
                "naziv": (row.get("naziv") or "").strip(),
                "napomena": (row.get("napomena") or "").strip(),
            }
    log.info(f"Registar vjerovnika: {len(registry)} OIB-a")
    return registry


# ============================================================================
# PARSIRANJE PDF TEKSTA — blok ovrhovoditelj / ovršenik
# ============================================================================

# Početak bloka stranaka: "u pravnoj stvari ovrhovoditelja ..." i varijante.
# Stečajni pozivi (provjereno na stvarnim PDF-ovima): "... poslovni broj
# ST-x/y u stečajnom postupku nad dužnikom <DUŽNIK>, OIB: ..., zastupanom
# po stečajnom upravitelju ..."
_START_RX = re.compile(
    r"(?:pravnoj\s+stvari\s+|predmetu\s+)?"
    r"(ovrhovoditelj(?:a|ice)?|predlagatelj(?:a|ice)?\s+osiguranja|"
    r"stečajnog\s+dužnika|stecajnog\s+duznika|"
    r"(?:stečajnom|stecajnom)\s+postupku\s+nad\s+(?:dužnikom|duznikom)|"
    r"nad\s+(?:dužnikom|duznikom))\b\s*[:,]?",
    re.IGNORECASE,
)

# Kraj bloka: "protiv ovršenika ...", "protiv protustranke", ili sekcije
# koje sigurno dolaze iza stranaka.
_END_RX = re.compile(
    r"protiv\s+(?:ovr[šs]enik\w*|protustrank\w*|du[žz]nik\w*)|zastupan\w*\s+po|"
    r"radi\s+(?:naplate|ovrhe|osiguranja)|donosi|objavljuje\s+se",
    re.IGNORECASE,
)

_OIB_RX = re.compile(r"OIB\s*[:.]?\s*(\d{11})", re.IGNORECASE)

# Pravni oblik u nazivu → firma (a ne privatna osoba)
_LEGAL_FORM_RX = re.compile(
    r"\b(d\s*\.?\s*o\s*\.?\s*o|j\s*\.?\s*d\s*\.?\s*o\s*\.?\s*o|d\s*\.?\s*d|"
    r"s\s*\.?\s*a|gmbh|a\s*\.?\s*g|ltd|llc|k\s*\.?\s*d|kft|s\s*\.?\s*r\s*\.?\s*l|"
    r"bank[ae]?|štedionic\w+|stedionic\w+|kreditna\s+unija|osiguranje|"
    r"factoring|leasing|uslug[ea]|obrt|zadrug\w+|udrug\w+|ustanov\w+|"
    r"dru[šs]tvo\s+s\s+ograni[čc]enom|dioni[čc]ko\s+dru[šs]tvo|"
    r"ste[čc]ajna\s+masa|"
    r"grad|općina|opcina|županija|zupanija|republika)\b",
    re.IGNORECASE,
)

# Smeće na početku imena: redni brojevi, veznici, interpunkcija
_NAME_JUNK_RX = re.compile(r"^[\s,;.\-–—]*(?:\d+\s*[.)]\s*|i\s+|te\s+)?", re.IGNORECASE)


def _clean_name(raw: str) -> str:
    """Skini vodeće smeće (zarezi, redni brojevi), uzmi segment do prvog
    zareza. Završnu točku NE skidamo — pravni oblici završavaju na "d.o.o."."""
    name = _NAME_JUNK_RX.sub("", raw.strip())
    name = name.split(",")[0]
    name = re.sub(r"\s+", " ", name).strip(" ,;-–—")
    return name


def parse_stranke(text: str) -> dict:
    """
    Iz teksta poziva izvuče ovrhovoditelje i ovršenike.
    Vraća {"ovrhovoditelji": [{naziv, oib, uloga}], "ovrsenici": [...],
           "parse_ok": bool}.
    """
    flat = re.sub(r"\s+", " ", text)

    m_start = _START_RX.search(flat)
    if not m_start:
        return {"ovrhovoditelji": [], "ovrsenici": [], "parse_ok": False}
    uloga = m_start.group(1).lower()
    if "dužnik" in uloga or "duznik" in uloga:
        # Kod stečaja stranka nije vjerovnik nego stečajni dužnik — email
        # to posebno označava, klasifikacija ide normalno (obično firma)
        uloga = "stečajni dužnik"

    rest = flat[m_start.end():]
    m_end = _END_RX.search(rest)
    block = rest[:m_end.start()] if m_end else rest[:600]

    ovrhovoditelji = _extract_entities(block, uloga)

    # Ovršenici (samo za prikaz): blok iza "protiv ovršenika" do iduće sekcije
    ovrsenici: list[dict] = []
    if m_end and m_end.group(0).lower().startswith("protiv"):
        after = rest[m_end.end():]
        m_stop = re.search(
            r"radi\s+(?:naplate|ovrhe|osiguranja)|donosi|objavljuje|zaključ|zakljuc",
            after, re.IGNORECASE,
        )
        ovrsenici = _extract_entities(
            after[:m_stop.start()] if m_stop else after[:600], "ovršenik"
        )

    return {
        "ovrhovoditelji": ovrhovoditelji,
        "ovrsenici": ovrsenici,
        "parse_ok": bool(ovrhovoditelji),
    }


def _extract_entities(block: str, uloga: str) -> list[dict]:
    """Svaki entitet završava s "OIB: <11 znamenaka>"; naziv je segment
    do prvog zareza od kraja prethodnog entiteta."""
    entities = []
    pos = 0
    for m in _OIB_RX.finditer(block):
        chunk = block[pos:m.start()]
        pos = m.end()
        naziv = _clean_name(chunk)
        oib = m.group(1)
        if not valid_oib(oib):
            log.warning(f"Parsiran OIB ne prolazi kontrolnu znamenku — preskačem entitet")
            continue
        if naziv:
            entities.append({"naziv": naziv, "oib": oib, "uloga": uloga})
    return entities


def classify(naziv: str, oib: str | None, registry: dict[str, dict]) -> str:
    """Klasifikacija prema brief-u: registar > pravni oblik > privatna osoba."""
    if oib and oib in registry:
        if oib == EOS_NEKRETNINE_OIB:
            log.info("Ovrhovoditelj je EOS nekretnine d.o.o. (kupac na dražbama!) — vidi state")
        return registry[oib]["kategorija"]
    if not naziv:
        return "nepoznato"
    if _LEGAL_FORM_RX.search(naziv):
        return "firma_ostalo"
    return "privatna_osoba"


# ============================================================================
# FILTRI NEKRETNINE (korak 2 — konfiguracija u config.yaml)
# ============================================================================

def _normalize(s: str) -> str:
    """Spusti u ASCII bez dijakritike za usporedbu (kuća == kuca)."""
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d")


def match_nekretnina_filter(record: dict, nk_cfg: dict) -> bool:
    """Prolazi li dražba filtre lokacije/vrste/vrijednosti za ovaj feature."""
    zupanije = nk_cfg.get("zupanije") or []
    if zupanije and record.get("zupanija") not in zupanije:
        return False

    min_v = nk_cfg.get("min_vrijednost_eur")
    max_v = nk_cfg.get("max_vrijednost_eur")
    vrijednost = record.get("vrijednost") or 0
    if min_v and vrijednost < min_v:
        return False
    if max_v and vrijednost > max_v:
        return False

    kljucne = nk_cfg.get("kljucne_rijeci") or []
    if kljucne:
        tekst = _normalize(f"{record.get('vrsta', '')} {record.get('opis', '')}")
        # \b + max 3 slova sufiksa (stan/stana/stanom; kuca/kucom) ali ne
        # "stanje" (stan+j blokiran lookaheadom)
        hit = any(
            re.search(rf"\b{re.escape(_normalize(k))}(?!j)\w{{0,3}}\b", tekst)
            for k in kljucne
        )
        if not hit:
            return False
    return True


# ============================================================================
# STATE (data/ovrh/ — gitignoran, GHA cache; NE commita se: osobni podaci)
# ============================================================================

def state_path(id_: str) -> Path:
    return STATE_DIR / f"{id_}.json"


def load_state(id_: str) -> dict | None:
    p = state_path(id_)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(id_: str, data: dict):
    data["parsed_at"] = datetime.now().isoformat()
    state_path(id_).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def needs_processing(id_: str) -> bool:
    st = load_state(id_)
    if st is None:
        return True
    if st.get("status") == "ok":
        return False
    # Neuspjeli pokušaji: retry nakon par dana (poziv se možda objavi kasnije)
    try:
        parsed = datetime.fromisoformat(st.get("parsed_at", ""))
    except ValueError:
        return True
    return datetime.now() - parsed > timedelta(days=RETRY_FAILED_AFTER_DAYS)


# ============================================================================
# PLAYWRIGHT DOHVAT S JAVNE OBJAVE
# ============================================================================

def fetch_pismena_meta(page, uuid: str) -> list[dict]:
    """GET ?pp={uuid} i uhvati JSON odgovora /pretrazivanje/datatable."""
    with page.expect_response(
        lambda r: "pretrazivanje/datatable" in r.url, timeout=30_000
    ) as resp_info:
        page.goto(PP_URL_FMT.format(uuid=uuid), wait_until="domcontentloaded")
    data = resp_info.value.json()
    return data.get("aaData") or []


def pick_poziv(rows: list[dict]) -> dict | None:
    """Odaberi važeći 'Poziv na sudjelovanje' (sifVrPism 25/26, ne poništen)."""
    kandidati = [
        r for r in rows
        if (r.get("sifVrPism") in POZIV_SIFRE
            or str(r.get("nazVrPism", "")).startswith(POZIV_NAZIV_PREFIX))
        and r.get("sts") != 10
    ]
    if not kandidati:
        return None
    # Najnoviji po datumu objave (dd.mm.yyyy)
    def _key(r):
        try:
            return datetime.strptime(r.get("datObjaveFmt", ""), "%d.%m.%Y")
        except ValueError:
            return datetime.min
    return max(kandidati, key=_key)


def download_poziv_pdf(page, poziv: dict, out_path: Path) -> bool:
    """
    Klikne 'Detalji' na redu poziva pa download link — identično kao
    korisnik u browseru (stranica sama izvršava svoju nevidljivu captchu).
    Ako download ne krene (npr. vidljivi captcha izazov), odustani.
    U interaktivnom modu (PONIP_OVRH_INTERACTIVE=1 / --interactive) čeka
    se do 3 min da KORISNIK sam riješi izazov u otvorenom prozoru.
    """
    interactive = os.environ.get("PONIP_OVRH_INTERACTIVE") == "1"
    timeout_ms = 180_000 if interactive else 30_000
    # "Detalji" je <span> u zadnjoj ćeliji reda (ne <a>!) — klik otvara
    # child-row s detaljima i download linkom (provjereno na živom DOM-u)
    row = page.locator("tbody tr", has_text=POZIV_NAZIV_PREFIX).first
    row.locator("td:last-child span", has_text="Detalji").first.click()
    page.wait_for_timeout(500)
    dl_link = page.locator('a[title^="Preuzmi pismeno"]').first
    try:
        if interactive:
            log.info("Interaktivni mod: ako iskoči captcha, riješi je u prozoru (čekam do 3 min)")
        with page.expect_download(timeout=timeout_ms) as dl_info:
            dl_link.click()
        dl_info.value.save_as(out_path)
        return True
    except Exception as e:
        log.warning(f"Download nije krenuo ({type(e).__name__}) — preskačem (mogući captcha izazov)")
        return False


def extract_pdf_text(pdf_path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    # Stranke su na prvoj stranici, ali uzmi do 3 za svaki slučaj
    return "\n".join((p.extract_text() or "") for p in reader.pages[:3])


# ============================================================================
# GLAVNI PIPELINE
# ============================================================================

def process_ovrhovoditelji(records: list[dict], cfg: dict) -> dict[str, dict]:
    """
    Za dražbe koje prolaze filtre dohvati i klasificiraj ovrhovoditelja.
    Vraća {id: state_dict} SAMO za kategorije iz obavijesti_za_kategorije.

    Rezultat se drži ODVOJENO od records — ne smije procuriti u javni
    dashboard payload (osobni podaci); u meta ide samo agregat.
    """
    ov_cfg = cfg.get("ovrhovoditelj_filter") or {}
    if not ov_cfg.get("enabled", False):
        log.info("Ovrhovoditelj filter isključen u configu")
        return {}

    nk_cfg = ov_cfg.get("nekretnina") or {}
    notify_kat = set(ov_cfg.get("obavijesti_za_kategorije") or VALJANE_KATEGORIJE)
    ignore_kat = set(ov_cfg.get("ignoriraj_kategorije") or [])
    max_pdf = int(ov_cfg.get("max_pdf_po_runu", 40))

    registry = load_registry()
    if not registry:
        log.error("Prazan registar — nastavljam (sve će biti firma/privatna/nepoznato)")

    kandidati = [
        r for r in records
        if r.get("uuid") and match_nekretnina_filter(r, nk_cfg)
    ]
    # Novi prioritetno, zatim backfill po vrijednosti
    kandidati.sort(key=lambda r: (0 if r.get("nova") else 1, -(r.get("vrijednost") or 0)))
    to_process = [r for r in kandidati if needs_processing(r["id"])][:max_pdf]
    log.info(
        f"Ovrhovoditelji: {len(kandidati)} kandidata prošlo filtre, "
        f"{len(to_process)} za obradu (cap {max_pdf})"
    )

    if to_process:
        _process_batch(to_process, registry)

    # Sastavi rezultat iz state-a za SVE kandidate (i prije keširane)
    rezultat: dict[str, dict] = {}
    for r in kandidati:
        st = load_state(r["id"])
        if not st:
            continue
        kat = st.get("kategorija", "nepoznato")
        if kat in ignore_kat or kat not in notify_kat:
            continue
        rezultat[r["id"]] = st
    log.info(f"Ovrhovoditelji: {len(rezultat)} dražbi matchalo kategorije za obavijest")
    return rezultat


def _process_batch(to_process: list[dict], registry: dict):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright nije instaliran — preskačem dohvat ovrhovoditelja")
        return

    tmp_dir = STATE_DIR / "pdf_tmp"
    tmp_dir.mkdir(exist_ok=True)

    # Headful default: FINA-ina bot-zaštita (F5) RESETIRA konekcije headless
    # Chromiuma (potvrđeno 2026-10-29), pravi headed browser prolazi.
    # Na GHA se vrti pod xvfb-om (vidi workflow). PONIP_OVRH_HEADLESS=1
    # za eksperimente.
    headless = os.environ.get("PONIP_OVRH_HEADLESS", "0") == "1"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="hr-HR",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        for r in to_process:
            id_ = r["id"]
            try:
                _process_one(page, r, registry, tmp_dir)
            except Exception as e:
                log.warning(f"ID {id_}: dohvat pao ({type(e).__name__}: {e})")
                save_state(id_, _base_state(r) | {
                    "status": "download_failed", "kategorija": "nepoznato",
                })
            time.sleep(REQUEST_PAUSE_S)
        browser.close()


def _base_state(r: dict) -> dict:
    return {
        "id": r["id"],
        "uuid": r.get("uuid"),
        "poslBroj": r.get("poslBroj"),
        "sud": r.get("sud"),
    }


def _process_one(page, r: dict, registry: dict, tmp_dir: Path):
    """Jedna dražba: pismena meta → poziv PDF → parse → klasifikacija."""
    id_ = r["id"]
    rows = fetch_pismena_meta(page, r["uuid"])
    poziv = pick_poziv(rows)
    if poziv is None:
        log.info(f"ID {id_}: nema poziva na dražbu među {len(rows)} pismena")
        save_state(id_, _base_state(r) | {
            "status": "no_poziv", "kategorija": "nepoznato",
        })
        return

    pdf_path = tmp_dir / f"{id_}.pdf"
    if not download_poziv_pdf(page, poziv, pdf_path):
        save_state(id_, _base_state(r) | {
            "status": "download_failed", "kategorija": "nepoznato",
            "idPismena": poziv.get("idPismena"),
        })
        return

    try:
        text = extract_pdf_text(pdf_path)
    finally:
        pdf_path.unlink(missing_ok=True)

    stranke = parse_stranke(text)
    if not stranke["parse_ok"]:
        # Log BEZ sadržaja PDF-a (javni logovi!); tekst ide u lokalni
        # debug dump (data/ je gitignoran) za doradu parsera
        log.warning(f"ID {id_}: parsiranje stranaka nije uspjelo")
        debug_dir = STATE_DIR / "debug"
        debug_dir.mkdir(exist_ok=True)
        (debug_dir / f"{id_}.txt").write_text(text[:8000], encoding="utf-8")
        save_state(id_, _base_state(r) | {
            "status": "parse_failed", "kategorija": "nepoznato",
            "idPismena": poziv.get("idPismena"),
        })
        return

    for o in stranke["ovrhovoditelji"]:
        o["kategorija"] = classify(o["naziv"], o["oib"], registry)
        if o["oib"] in registry:
            o["naziv_registar"] = registry[o["oib"]]["naziv"]

    # Glavna kategorija dražbe: "najzanimljivija" po prioritetu briefa
    prioritet = ["privatna_osoba", "agencija", "banka", "banka_ugasena",
                 "firma_ostalo", "drzava", "nepoznato"]
    kategorije = [o["kategorija"] for o in stranke["ovrhovoditelji"]]
    glavna = next((k for k in prioritet if k in kategorije), "nepoznato")

    save_state(id_, _base_state(r) | {
        "status": "ok",
        "kategorija": glavna,
        "ovrhovoditelji": stranke["ovrhovoditelji"],
        "ovrsenici": stranke["ovrsenici"],
        "idPismena": poziv.get("idPismena"),
        "vrPismena": poziv.get("nazVrPism"),
    })
    # Javni log: SAMO kategorija, bez imena/OIB-a
    log.info(f"ID {id_}: ovrhovoditelj klasificiran kao [{glavna}]")


# ============================================================================
# AGREGAT ZA DASHBOARD (javno — samo brojevi, bez osobnih podataka)
# ============================================================================

def summary_counts(ovrh_map: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for st in ovrh_map.values():
        k = st.get("kategorija", "nepoznato")
        counts[k] = counts.get(k, 0) + 1
    return counts


def import_manual_pdf(id_: str, pdf_path: Path, registry: dict | None = None) -> dict:
    """
    Ručni fallback za captcha slučajeve: korisnik sam skine poziv s Javne
    objave (link je u emailu/dry-runu), pa ga ovime ubaci u state.
    """
    registry = registry if registry is not None else load_registry()
    text = extract_pdf_text(pdf_path)
    stranke = parse_stranke(text)
    if not stranke["parse_ok"]:
        raise ValueError(
            f"Parsiranje nije uspjelo za {pdf_path.name} — provjeri je li to "
            f"'Poziv na sudjelovanje' (prva stranica mora imati ovrhovoditelja)"
        )
    for o in stranke["ovrhovoditelji"]:
        o["kategorija"] = classify(o["naziv"], o["oib"], registry)
        if o["oib"] in registry:
            o["naziv_registar"] = registry[o["oib"]]["naziv"]
    prioritet = ["privatna_osoba", "agencija", "banka", "banka_ugasena",
                 "firma_ostalo", "drzava", "nepoznato"]
    kategorije = [o["kategorija"] for o in stranke["ovrhovoditelji"]]
    glavna = next((k for k in prioritet if k in kategorije), "nepoznato")
    st = {
        "id": id_,
        "status": "ok",
        "izvor": "manual_import",
        "kategorija": glavna,
        "ovrhovoditelji": stranke["ovrhovoditelji"],
        "ovrsenici": stranke["ovrsenici"],
    }
    save_state(id_, st)
    return st


# ============================================================================
# CLI — dry-run
# ============================================================================

def _dry_run(limit: int):
    """Suhi rad: trenutne dražbe u najavi, ispis tablice, bez emaila."""
    import monitor  # lazy — izbjegni cikluse i side-effecte pri importu modula

    cfg = monitor.load_config()
    ov_cfg = cfg.get("ovrhovoditelj_filter") or {}
    ov_cfg["enabled"] = True  # dry-run uvijek radi
    ov_cfg["max_pdf_po_runu"] = limit
    cfg["ovrhovoditelj_filter"] = ov_cfg

    today = monitor.download_csv()
    df = monitor.filter_active(monitor.load_snapshot(today))
    cutoff = datetime.now() + timedelta(days=7)
    records = [monitor.to_record(row, set(), cutoff) for _, row in df.iterrows()]
    records = [r for r in records if r["id"]]

    live = monitor.fetch_live_tijek_data()
    for r in records:
        d = live.get(r["id"])
        if d:
            r["uuid"] = d.get("uuid")

    ovrh = process_ovrhovoditelji(records, cfg)

    by_id = {r["id"]: r for r in records}
    print(f"\n{'ID':>6} | {'POSLOVNI BROJ':<16} | {'ŽUPANIJA':<22} | "
          f"{'VRIJEDNOST':>12} | {'OVRHOVODITELJ':<40} | {'OIB':<11} | KATEGORIJA")
    print("-" * 130)
    for id_, st in sorted(ovrh.items(), key=lambda kv: kv[1].get("kategorija", "")):
        r = by_id.get(id_, {})
        ovs = st.get("ovrhovoditelji") or [{}]
        for o in ovs:
            print(f"{id_:>6} | {st.get('poslBroj', ''):<16} | {r.get('zupanija', ''):<22} | "
                  f"{(r.get('vrijednost') or 0):>10,.0f} € | {o.get('naziv', '—'):<40} | "
                  f"{o.get('oib', '—'):<11} | {st.get('kategorija', '')}")
    print(f"\nUkupno: {len(ovrh)} dražbi | agregat: {summary_counts(ovrh)}")
    statusi = {}
    for p in STATE_DIR.glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8")).get("status")
            statusi[s] = statusi.get(s, 0) + 1
        except Exception:
            pass
    print(f"State statusi: {statusi}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Detekcija ovrhovoditelja s FINA Javne objave")
    ap.add_argument("--dry-run", action="store_true", help="ispiši tablicu, ne šalji ništa")
    ap.add_argument("--limit", type=int, default=10, help="max PDF-ova za obradu (default 10)")
    ap.add_argument("--import-pdf", nargs=2, metavar=("ID", "PDF_PATH"),
                    help="ručno ubaci skinuti poziv: ID nadmetanja + putanja do PDF-a")
    ap.add_argument("--interactive", action="store_true",
                    help="kod captcha izazova čekaj do 3 min da ga korisnik riješi u prozoru")
    args = ap.parse_args()
    if args.interactive:
        os.environ["PONIP_OVRH_INTERACTIVE"] = "1"
    if args.import_pdf:
        id_, pdf = args.import_pdf
        st = import_manual_pdf(id_, Path(pdf))
        print(f"ID {id_} -> [{st['kategorija']}]")
        for o in st["ovrhovoditelji"]:
            print(f"  ovrhovoditelj: {o['naziv']} (OIB: {o['oib']}) [{o['kategorija']}]")
    elif args.dry_run:
        _dry_run(args.limit)
    else:
        ap.error("koristi --dry-run ili --import-pdf (produkcija ide kroz monitor.py)")
