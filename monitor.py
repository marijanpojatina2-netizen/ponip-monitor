#!/usr/bin/env python3
"""
PONIP Monitor v3 — GitHub Actions edition
FINA Očevidnik javnih dražbi s AI scoringom.

Pokreće ga GitHub Actions workflow svaki dan u 07:00 Europe/Zagreb.
Može se pokrenuti i lokalno za testiranje.
"""

import json
import logging
import gzip
import re
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yaml
from dotenv import load_dotenv

from scorer import score_items, DETAILED_THRESHOLD_EUR, load_cached
from emailer import send_email

# ============================================================================
# KONFIGURACIJA PUTANJA
# ============================================================================
# PONIP_BASE default: direktorij gdje živi ova skripta (repo root).
# Za GHA: workflow postavlja PONIP_BASE=${{ github.workspace }}
# Za lokalno: nije potrebno postaviti (defaultira na script dir)

BASE_DIR = Path(os.environ.get("PONIP_BASE", Path(__file__).resolve().parent))
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config.yaml"
TEMPLATE_PATH = BASE_DIR / "dashboard_template.html"
ENV_PATH = BASE_DIR / ".env"

# Dashboard output: PONIP_DASHBOARD_OUT ga može override-ati
# Za GHA: workflow postavlja na docs/index.html (GitHub Pages root)
DASHBOARD_OUT = Path(
    os.environ.get("PONIP_DASHBOARD_OUT", BASE_DIR / "dashboard.html")
)

CSV_URL = "https://ponip.fina.hr/ocevidnik-web/preuzmi/csv"

for d in (DATA_DIR, REPORTS_DIR, LOGS_DIR, DASHBOARD_OUT.parent):
    d.mkdir(parents=True, exist_ok=True)

# Učitaj .env samo ako postoji (GHA koristi env vars iz secrets)
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

DRY_RUN = os.environ.get("PONIP_DRY_RUN", "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("monitor")


# ============================================================================
# MAPPING: sud -> županija
# ============================================================================

COURT_TO_COUNTY = [
    (r"Pul[ai]|Pazin|Rovinj|Labin|Poreč|Porec|Buzet", "Istarska"),
    (r"Zagreb|Samobor|Sesvete|Velik[ao] Gorica|Dugo Selo|Ivanić|Jastrebarsko|Zaprešić", "Grad Zagreb i okolica"),
    (r"Rijek[ai]|Opatij[ai]|Crikvenic[ai]|Krk|Delnic[ei]|Mali Lošinj|Rab", "Primorsko-goranska"),
    (r"Split|Trogir|Makarsk|Imotsk|Sinj|Supetar|Vis|Hvar|Brač|Brac", "Splitsko-dalmatinska"),
    (r"Zadar|Biograd|Gračac|Pag|Benkovac|Obrovac", "Zadarska"),
    (r"Šibenik|Sibenik|Knin|Drniš", "Šibensko-kninska"),
    (r"Dubrovni[kc]|Korčul|Ploč[ei]|Metković", "Dubrovačko-neretvanska"),
    (r"Varaždin|Varazdin|Ivane[cz]|Novi Marof|Ludbreg", "Varaždinska"),
    (r"Osijek|Đakov|Djakov|Našic|Nasic|Beli Manastir|Donji Miholjac|Valpov", "Osječko-baranjska"),
    (r"Vukovar|Vinkovc|Županj|Zupanj|Ilok", "Vukovarsko-srijemska"),
    (r"Slavonski Brod|Nova Gradišk|Nova Gradisk", "Brodsko-posavska"),
    (r"Karlov[ac]|Ogulin|Slunj|Duga Resa", "Karlovačka"),
    (r"Sisa[kc]|Kutin|Petrinj|Novsk|Glina", "Sisačko-moslavačka"),
    (r"Bjelovar|Daruvar|Grubišno|Čazm|Cazm", "Bjelovarsko-bilogorska"),
    (r"Koprivnic|Đurđev|Djurdjev|Križev|Krizev", "Koprivničko-križevačka"),
    (r"Čakovec|Cakovec|Prelog|Mursko", "Međimurska"),
    (r"Krapin|Zabok|Zlatar|Donja Stubica|Pregrada|Klanjec", "Krapinsko-zagorska"),
    (r"Požeg|Pozeg|Pakrac", "Požeško-slavonska"),
    (r"Virovitic|Slatin|Orahovic", "Virovitičko-podravska"),
    (r"Gospić|Gospic|Otočac|Otocac|Senj|Korenic", "Ličko-senjska"),
]


def court_to_county(sud: str) -> str:
    if not sud:
        return "Ostalo"
    for pattern, county in COURT_TO_COUNTY:
        if re.search(pattern, sud, re.IGNORECASE):
            return county
    return "Ostalo"


# ============================================================================
# CSV HANDLING
# ============================================================================

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error(f"Config ne postoji: {CONFIG_PATH}")
        sys.exit(1)
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def download_csv() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    out = DATA_DIR / f"snapshot_{today}.csv.gz"
    if out.exists():
        log.info(f"Snimak za danas već postoji: {out.name}")
        return out
    log.info(f"Skidam CSV s {CSV_URL}")
    r = requests.get(CSV_URL, timeout=180, headers={"User-Agent": "Mozilla/5.0 ponip-monitor"})
    r.raise_for_status()
    with gzip.open(out, "wb") as f:
        f.write(r.content)
    log.info(f"Spremljeno: {out.name} ({out.stat().st_size / 1_048_576:.2f} MB)")
    return out


def load_snapshot(path: Path) -> pd.DataFrame:
    with gzip.open(path, "rt", encoding="utf-8-sig") as f:
        df = pd.read_csv(f, sep=";", dtype=str, keep_default_na=False)
    date_cols = [
        "Datum i vrijeme početka",
        "Datum i vrijeme početka nadmetanja",
        "Datum i vrijeme završetka nadmetanja",
        "Datum valute jamčevine",
    ]
    for c in date_cols:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    num_cols = [
        "Utvrđena vrijednost",
        "Početna cijena za nadmetanje",
        "Iznos jamčevine",
        "Iznos dražbenog koraka",
    ]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", "."), errors="coerce")
    return df


def find_previous(today_path: Path) -> Path | None:
    snapshots = sorted(p for p in DATA_DIR.glob("snapshot_*.csv.gz") if p != today_path)
    return snapshots[-1] if snapshots else None


def prune_old_snapshots(keep_last: int = 14):
    """Zadrži samo N najnovijih snapshot-a u data/ da repo ne nabuja."""
    snapshots = sorted(DATA_DIR.glob("snapshot_*.csv.gz"))
    if len(snapshots) <= keep_last:
        return
    to_delete = snapshots[:-keep_last]
    for p in to_delete:
        p.unlink()
        log.info(f"Obrisan stari snapshot: {p.name}")


def filter_active(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aktivne dražbe = nadmetanje još nije završilo (ili nema datuma završetka).

    Past-jamčevina dražbe ostaju vidljive (samo ih frontend može sakriti
    pomoću `pastJamcevina` flag-a) — korisnik može htjeti pratiti sudionike
    i trenutnu cijenu u tijeku, ali ne i u potpunosti zatvorene aukcije.
    """
    now = datetime.now()
    end_ok = (
        df["Datum i vrijeme završetka nadmetanja"].isna()
        | (df["Datum i vrijeme završetka nadmetanja"] > now - timedelta(days=1))
    )
    return df[end_ok].copy()


# --- FINA live data (broj sudionika, trenutna cijena, UUID) --------------

FINA_TIJEK_URL = "https://ponip.fina.hr/ocevidnik-web/pregled/tijek"
FINA_NAJAVA_URL = "https://ponip.fina.hr/ocevidnik-web/pregled/najava"
FINA_LISTING_URL = "https://ponip.fina.hr/ocevidnik-web/pregled/nadmetanja-u-tijeku"
FINA_DETAIL_URL_FMT = "https://ponip.fina.hr/ocevidnik-web/predmet_prodaje/{uuid}?src=2"
_RECORD_RE = re.compile(r'\{[^{}]*"idNdmt":(\d+)[^{}]*\}')


def _parse_fina_records(text: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in _RECORD_RE.finditer(text):
        blob = m.group(0)
        try:
            rec = json.loads(blob)
        except json.JSONDecodeError:
            continue
        id_ = str(rec.get("idNdmt", ""))
        if not id_:
            continue
        out[id_] = {
            "sudionici": rec.get("brUplatitelja"),
            "trenutnaCijena": rec.get("trenutnaCijena"),  # samo /tijek ima
            "noBids": rec.get("noBids"),
            "uuid": rec.get("uuid"),
        }
    return out


def fetch_live_tijek_data() -> dict[str, dict]:
    """
    Skida live podatke s FINA-e: i nadmetanja u tijeku i ona u najavi.
    Vraća dict {id: {'sudionici','trenutnaCijena','noBids','uuid'}}.
    /pregled/tijek pokriva aktivno bidding (231 zapisa, ima trenutna cijena).
    /pregled/najava pokriva upcoming (~624 zapisa, već imaju brUplatitelja
    jer FINA ažurira kako tko uplati jamčevinu).
    Non-fatal ako padne — vraća što ima.
    """
    out: dict[str, dict] = {}
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 ponip-monitor"})
        # 1. session cookie
        sess.get(FINA_LISTING_URL, timeout=30)
        # 2. tijek (in-progress)
        try:
            r = sess.get(FINA_TIJEK_URL, timeout=60)
            r.raise_for_status()
            out.update(_parse_fina_records(r.text))
        except Exception as e:
            log.warning(f"FINA /pregled/tijek failed: {e}")
        # 3. najava (upcoming) — ovaj endpoint pokriva većinu naših drazbi
        try:
            r = sess.get(FINA_NAJAVA_URL, timeout=60)
            r.raise_for_status()
            for k, v in _parse_fina_records(r.text).items():
                # Ako je već iz tijek-a, ne overwrite-aj (tijek ima trenutnaCijena)
                if k not in out:
                    out[k] = v
        except Exception as e:
            log.warning(f"FINA /pregled/najava failed: {e}")
        log.info(f"FINA live: {len(out)} dražbi (tijek + najava)")
        return out
    except Exception as e:
        log.warning(f"Ne mogu skinuti FINA live podatke: {e}")
        return out


def to_record(row: pd.Series, new_ids: set, jamcevina_cutoff: datetime) -> dict:
    def iso(v):
        return v.isoformat() if pd.notna(v) and hasattr(v, "isoformat") else None

    def num(v):
        return float(v) if pd.notna(v) else None

    def txt(v, limit: int = 0):
        s = str(v).strip() if v is not None else ""
        if limit and len(s) > limit:
            s = s[:limit].rstrip() + "..."
        return s

    id_ = txt(row.get("ID nadmetanja", ""))
    rok = row.get("Datum valute jamčevine")
    now = datetime.now()
    urgent = pd.notna(rok) and now < rok.to_pydatetime() <= jamcevina_cutoff
    past_jamcevina = pd.notna(rok) and rok.to_pydatetime() < now.replace(hour=0, minute=0, second=0, microsecond=0)

    return {
        "id": id_,
        "sud": txt(row.get("Nadležno tijelo", "")),
        "zupanija": court_to_county(txt(row.get("Nadležno tijelo", ""))),
        "poslBroj": txt(row.get("Poslovni broj spisa", "")),
        "vrsta": txt(row.get("Vrsta predmeta prodaje", "")),
        "runda": txt(row.get("Oznaka EJD", "")),
        "opis": txt(row.get("Opis", ""), limit=800),
        "nacin": txt(row.get("Način prodaje", "")),
        "vrijednost": num(row.get("Utvrđena vrijednost")),
        "pocetna": num(row.get("Početna cijena za nadmetanje")),
        "jamcevina": num(row.get("Iznos jamčevine")),
        "korak": num(row.get("Iznos dražbenog koraka")),
        "rokJamcevina": iso(rok),
        "pocetakNadm": iso(row.get("Datum i vrijeme početka nadmetanja")),
        "zavrsetakNadm": iso(row.get("Datum i vrijeme završetka nadmetanja")),
        "pocetakPrijava": iso(row.get("Datum i vrijeme početka")),
        "rokKupovnine": txt(row.get("Rok u kojem je kupac dužan položiti kupovninu", "")),
        "razgledavanje": txt(row.get("Razgledavanje", ""), limit=300),
        "nova": id_ in new_ids,
        "urgent": bool(urgent),
        "pastJamcevina": bool(past_jamcevina),
        "score": None,
        "detailed": None,
        "sudionici": None,            # popunjava se iz fetch_live_tijek_data
        "trenutnaCijena": None,       # iz iste live AJAX rute
        "uuid": None,                 # popunjava se iz live podataka (za FINA URL)
        "finaUrl": None,              # postaviti ćemo nakon merge-a (UUID > Google fallback)
    }


# ============================================================================
# OUTPUTS
# ============================================================================

def build_dashboard(records: list[dict], meta: dict) -> str:
    if not TEMPLATE_PATH.exists():
        log.error(f"Template ne postoji: {TEMPLATE_PATH}")
        sys.exit(1)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(
        {"items": records, "meta": meta},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return template.replace('"__DASHBOARD_DATA__"', payload)


def fmt_eur(n):
    if n is None:
        return "—"
    return f"{n:,.0f} €".replace(",", ".")


def fmt_date(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def score_badge_html(score_overall: float | None) -> str:
    if score_overall is None:
        return '<span style="color:#999;font-size:11px;">—</span>'
    v = float(score_overall)
    if v >= 7.5:
        bg, fg = "#d1fae5", "#064e3b"
    elif v >= 5.5:
        bg, fg = "#fef3c7", "#78350f"
    else:
        bg, fg = "#fee2e2", "#7f1d1d"
    return (
        f'<span style="background:{bg};color:{fg};padding:3px 8px;border-radius:10px;'
        f'font-size:11px;font-weight:700;">{v:.1f}</span>'
    )


def ai_score(r: dict) -> float:
    """AI ocjena za sortiranje; 0 ako nije scoreano ili je keš neispravan."""
    try:
        return float((r.get("score") or {}).get("score_overall") or 0)
    except (TypeError, ValueError):
        return 0.0


OVRH_KAT_STIL = {
    # kategorija: (ikona, pozadina, boja teksta, labela)
    "privatna_osoba": ("&#128100;", "#ede9fe", "#4c1d95", "PRIVATNA OSOBA"),
    "agencija": ("&#127974;", "#fef3c7", "#78350f", "AGENCIJA ZA OTKUP"),
    "banka": ("&#127974;", "#dbeafe", "#1e3a8a", "BANKA"),
    "banka_ugasena": ("&#127974;", "#e5e7eb", "#374151", "BANKA (UGAŠENA)"),
    "firma_ostalo": ("&#127970;", "#d1fae5", "#064e3b", "FIRMA"),
    "nepoznato": ("&#10067;", "#fee2e2", "#7f1d1d", "NEPOZNATO — RUČNA PROVJERA"),
}


def build_ovrh_section(ovrh_map: dict[str, dict], by_id: dict[str, dict]) -> str:
    """Sekcija 'kandidati za otkup potraživanja' — samo za privatni email."""
    if not ovrh_map:
        return ""
    # Privatne osobe prve (najzanimljivije za otkup), pa agencije itd.
    redoslijed = ["privatna_osoba", "agencija", "banka", "banka_ugasena",
                  "firma_ostalo", "nepoznato"]
    stavke = sorted(
        ovrh_map.items(),
        key=lambda kv: (redoslijed.index(kv[1].get("kategorija", "nepoznato")),
                        -(by_id.get(kv[0], {}).get("vrijednost") or 0)),
    )
    # Klasificirane dobivaju kartice; "nepoznato" (captcha/parse fail) ide
    # u kompaktnu tablicu — inače 40 praznih kartica zatrpa mail
    klasificirane = [(i, s) for i, s in stavke if s.get("kategorija") != "nepoznato"]
    nepoznate = [(i, s) for i, s in stavke if s.get("kategorija") == "nepoznato"]
    blocks = []
    for id_, st in klasificirane[:20]:
        r = by_id.get(id_, {})
        kat = st.get("kategorija", "nepoznato")
        ikona, bg, fg, label = OVRH_KAT_STIL.get(kat, OVRH_KAT_STIL["nepoznato"])
        ovs = st.get("ovrhovoditelji") or []
        ov_html = "<br>".join(
            (('<span style="color:#6b7280;">stečajni dužnik:</span> '
              if o.get("uloga") == "stečajni dužnik" else "")
             + f'<b>{o.get("naziv", "—")}</b> <span style="color:#6b7280;">(OIB: {o.get("oib", "—")})</span>')
            for o in ovs
        ) or "<i>parsiranje nije uspjelo — provjeri ručno na Javnoj objavi</i>"
        ovrsenici = st.get("ovrsenici") or []
        ovrsenik_html = (
            f'<div style="color:#6b7280;font-size:11px;margin-top:2px;">ovršenik: '
            f'{", ".join(o.get("naziv", "") for o in ovrsenici[:3])}</div>'
            if ovrsenici else ""
        )
        discount = None
        if r.get("vrijednost") and r.get("pocetna"):
            try:
                discount = round((1 - r["pocetna"] / r["vrijednost"]) * 100)
            except ZeroDivisionError:
                pass
        novo_badge = (
            '<span style="background:#eaf3de;color:#27500A;padding:2px 6px;border-radius:8px;'
            'font-size:10px;font-weight:700;margin-left:6px;">NOVO</span>'
            if r.get("nova") else ""
        )
        jo_link = (
            f'https://ponip.fina.hr/javnaobjava-web/pretrazivanje/pismena?pp={st.get("uuid")}'
            if st.get("uuid") else "https://ponip.fina.hr/javnaobjava-web/"
        )
        blocks.append(f"""
<div style="background:#fff;border:1px solid #e5e7eb;border-left:4px solid {fg};border-radius:8px;padding:14px;margin-bottom:10px;">
  <div style="font-size:13px;margin-bottom:6px;">
    {ikona} <span style="background:{bg};color:{fg};padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;">{label}</span>
    {novo_badge}
  </div>
  <div style="font-size:13px;margin-bottom:4px;">{ov_html}</div>
  {ovrsenik_html}
  <div style="font-size:12px;color:#374151;margin-top:8px;">
    &#128205; {r.get("zupanija", "—")} &middot; {(r.get("opis") or "")[:160]}
  </div>
  <div style="font-size:12px;margin-top:6px;">
    &#128182; Vrijednost <b>{fmt_eur(r.get("vrijednost"))}</b> | početna <b>{fmt_eur(r.get("pocetna"))}</b>
    {f'({discount}% popusta)' if discount is not None else ''} | {r.get("runda") or "?"} dražba
  </div>
  <div style="font-size:11px;color:#6b7280;margin-top:4px;">
    &#128197; Jamčevina do <b>{fmt_date(r.get("rokJamcevina"))}</b> | nadmetanje {fmt_date(r.get("pocetakNadm"))}&ndash;{fmt_date(r.get("zavrsetakNadm"))}<br>
    &#9878;&#65039; {r.get("sud", "—")}, {st.get("poslBroj", "—")} | ID {id_}<br>
    &#128279; <a href="{r.get("finaUrl") or "#"}" style="color:#2563eb;">očevidnik</a> &middot;
    <a href="{jo_link}" style="color:#2563eb;">javna objava (pismena)</a>
  </div>
</div>""")
    nep_html = ""
    if nepoznate:
        nep_rows = []
        for id_, st in nepoznate[:40]:
            r = by_id.get(id_, {})
            jo_link = (
                f'https://ponip.fina.hr/javnaobjava-web/pretrazivanje/pismena?pp={st.get("uuid")}'
                if st.get("uuid") else "https://ponip.fina.hr/javnaobjava-web/"
            )
            nep_rows.append(
                f'<tr style="border-bottom:1px solid #f3f4f6;">'
                f'<td style="padding:5px 8px;font-size:11px;white-space:nowrap;">{id_}</td>'
                f'<td style="padding:5px 8px;font-size:11px;">{st.get("poslBroj", "—")}</td>'
                f'<td style="padding:5px 8px;font-size:11px;">{r.get("zupanija", "—")}</td>'
                f'<td style="padding:5px 8px;font-size:11px;text-align:right;white-space:nowrap;">{fmt_eur(r.get("vrijednost"))}</td>'
                f'<td style="padding:5px 8px;font-size:11px;white-space:nowrap;">{fmt_date(r.get("rokJamcevina"))}</td>'
                f'<td style="padding:5px 8px;font-size:11px;white-space:nowrap;">'
                f'<a href="{r.get("finaUrl") or "#"}" style="color:#2563eb;">očevidnik</a> &middot; '
                f'<a href="{jo_link}" style="color:#2563eb;">pismena</a></td></tr>'
            )
        nep_html = f"""
<p style="font-size:12px;color:#374151;margin:14px 0 6px;">
  &#10067; <b>{len(nepoznate)}</b> dražbi prolazi filtre, ali ovrhovoditelj još nije utvrđen
  (captcha na preuzimanju ili parsiranje) — automatski retry za 2 dana; za ručnu provjeru
  klikni "pismena" pa <code>--import-pdf</code>:
</p>
<table role="presentation" style="width:100%;border-collapse:collapse;">
  <thead><tr style="background:#fafaf8;text-align:left;">
    <th style="padding:5px 8px;font-size:10px;color:#888;text-transform:uppercase;">ID</th>
    <th style="padding:5px 8px;font-size:10px;color:#888;text-transform:uppercase;">spis</th>
    <th style="padding:5px 8px;font-size:10px;color:#888;text-transform:uppercase;">županija</th>
    <th style="padding:5px 8px;font-size:10px;color:#888;text-transform:uppercase;text-align:right;">vrijednost</th>
    <th style="padding:5px 8px;font-size:10px;color:#888;text-transform:uppercase;">rok jamč.</th>
    <th style="padding:5px 8px;font-size:10px;color:#888;text-transform:uppercase;">linkovi</th>
  </tr></thead>
  <tbody>{"".join(nep_rows)}</tbody>
</table>"""

    return f"""
<h2 style="font-size:15px;font-weight:600;margin:24px 0 8px;border-left:3px solid #7c3aed;padding-left:10px;">
  &#127974; Ovrhovoditelji — kandidati za otkup potraživanja ({len(klasificirane)} + {len(nepoznate)} neutvrđenih)
</h2>
<p style="font-size:11px;color:#6b7280;margin:0 0 10px;">
  Dražbe koje prolaze filtre (lokacija/vrsta/vrijednost) u kojima je ovrhovoditelj
  agencija za otkup dugova, banka ili privatna osoba — potencijalni otkup potraživanja
  prije ili za vrijeme dražbe.
</p>
{"".join(blocks) if blocks else '<p style="font-size:12px;color:#6b7280;">Danas nema novih klasificiranih.</p>'}
{nep_html}"""


def build_email_html(new_items: list[dict], urgent_items: list[dict], meta: dict, public_url: str | None,
                     ovrh_map: dict[str, dict] | None = None, all_records: list[dict] | None = None) -> str:
    def render_rows(items):
        rows = []
        for r in items[:30]:
            badge_colors = {
                "Prva": ("#E6F1FB", "#0C447C"), "Druga": ("#FAEEDA", "#854F0B"),
                "Treća": ("#FAECE7", "#993C1D"), "Četvrta": ("#FCEBEB", "#791F1F"),
            }
            bg, fg = badge_colors.get(r["runda"], ("#F1EFE8", "#444441"))
            badge = (
                f'<span style="background:{bg};color:{fg};padding:2px 8px;'
                f'border-radius:10px;font-size:10px;font-weight:600;'
                f'text-transform:uppercase;letter-spacing:.03em;">{r["runda"] or "?"}</span>'
            )
            score = r.get("score") or {}
            score_html = score_badge_html(score.get("score_overall")) if score else "—"
            rec = score.get("recommendation", "")
            rec_color = {"bid": "#064e3b", "deep_dive": "#1e3a8a", "watch": "#78350f", "pass": "#6b7280"}.get(rec, "#6b7280")
            rec_html = (
                f'<div style="color:{rec_color};font-size:10px;font-weight:600;'
                f'text-transform:uppercase;margin-top:2px;">{rec}</div>' if rec else ""
            )
            opis_short = r["opis"][:180] + "..." if len(r["opis"]) > 180 else r["opis"]
            reasoning = score.get("reasoning", "") if score else ""
            reasoning_html = (
                f'<div style="color:#4b5563;font-size:11px;font-style:italic;margin-top:4px;">'
                f'AI: {reasoning}</div>' if reasoning else ""
            )
            flags = (score.get("flags") or [])[:4] if score else []
            flags_html = (
                f'<div style="color:#9f1239;font-size:11px;margin-top:3px;">'
                f'&#9873; {" &middot; ".join(flags)}</div>' if flags else ""
            )
            rows.append(f"""
<tr style="border-bottom:1px solid #eee;">
  <td style="padding:10px 8px;vertical-align:top;">
    {badge}<br>
    <div style="margin-top:4px;">{score_html}</div>
    {rec_html}
  </td>
  <td style="padding:10px 8px;vertical-align:top;font-size:12px;">
    <div style="font-weight:600;">{r["zupanija"]}</div>
    <div style="color:#666;font-size:11px;">{r["sud"]}</div>
  </td>
  <td style="padding:10px 8px;vertical-align:top;font-size:12px;max-width:300px;">
    {opis_short}
    {reasoning_html}
    {flags_html}
  </td>
  <td style="padding:10px 8px;vertical-align:top;text-align:right;font-size:12px;white-space:nowrap;">
    <div style="font-weight:600;">{fmt_eur(r["pocetna"])}</div>
    <div style="color:#666;font-size:11px;">jamč. {fmt_eur(r["jamcevina"])}</div>
  </td>
  <td style="padding:10px 8px;vertical-align:top;font-size:11px;white-space:nowrap;">
    <div>rok jamč.: <b>{fmt_date(r["rokJamcevina"])}</b></div>
    <div style="color:#666;">dražba: {fmt_date(r["pocetakNadm"])}</div>
  </td>
</tr>
""")
        return "".join(rows)

    today = datetime.now().strftime("%d.%m.%Y")
    by_id = {r["id"]: r for r in (all_records or new_items + urgent_items)}
    ovrh_html = build_ovrh_section(ovrh_map or {}, by_id)
    urgent_html = render_rows(urgent_items) if urgent_items else '<tr><td colspan="5" style="padding:16px;color:#999;text-align:center;font-size:12px;">Nema jamčevina koje dospijevaju u sljedećih 7 dana.</td></tr>'
    new_html = render_rows(new_items) if new_items else '<tr><td colspan="5" style="padding:16px;color:#999;text-align:center;font-size:12px;">Nema novih nadmetanja od jučer.</td></tr>'

    deep_dives = [r for r in new_items if r.get("detailed")]
    deep_dive_html = ""
    if deep_dives:
        blocks = []
        for r in deep_dives[:5]:
            d = r["detailed"]
            me = d.get("market_estimate", {})
            comps = d.get("comparables", [])
            comps_html = "".join(
                f'<li style="margin-bottom:4px;">{c.get("opis", "")} — <b>{fmt_eur(c.get("cijena"))}</b> <span style="color:#6b7280;font-size:11px;">({c.get("izvor", "")})</span></li>'
                for c in comps[:3]
            )
            risks_html = "".join(f"<li>{x}</li>" for x in d.get("detailed_risks", [])[:4])
            blocks.append(f"""
<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:12px;">
  <div style="font-weight:600;font-size:13px;margin-bottom:8px;">ID {r["id"]} — {r["zupanija"]}</div>
  <div style="font-size:12px;color:#374151;margin-bottom:10px;">{r["opis"][:200]}...</div>
  <table style="width:100%;font-size:12px;">
    <tr>
      <td style="padding:4px 0;color:#6b7280;width:40%;">Procjena tržišne vrijednosti:</td>
      <td style="padding:4px 0;"><b>{fmt_eur(me.get("eur_min"))} – {fmt_eur(me.get("eur_max"))}</b> <span style="color:#6b7280;">({me.get("confidence", "")})</span></td>
    </tr>
    <tr>
      <td style="padding:4px 0;color:#6b7280;">Strategija:</td>
      <td style="padding:4px 0;">{d.get("investment_strategy", "—")}</td>
    </tr>
    <tr>
      <td style="padding:4px 0;color:#6b7280;">Entry price (maks):</td>
      <td style="padding:4px 0;"><b>{fmt_eur(d.get("entry_price_eur"))}</b></td>
    </tr>
  </table>
  {f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;margin:10px 0 4px;">Usporedive nekretnine</div><ul style="margin:0;padding-left:16px;font-size:12px;">{comps_html}</ul>' if comps_html else ""}
  {f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;margin:10px 0 4px;">Rizici</div><ul style="margin:0;padding-left:16px;font-size:12px;color:#991b1b;">{risks_html}</ul>' if risks_html else ""}
</div>""")
        deep_dive_html = f"""
<h2 style="font-size:15px;font-weight:600;margin:32px 0 8px;border-left:3px solid #7c3aed;padding-left:10px;">
  Detaljna AI analiza (>500k EUR)
</h2>
{"".join(blocks)}
"""

    dashboard_link = (
        f'<a href="{public_url}" style="color:#2563eb;">{public_url}</a>'
        if public_url else "(dashboard link nije konfiguriran)"
    )

    return f"""<!DOCTYPE html>
<html lang="hr">
<head><meta charset="UTF-8"><title>PONIP dražbe {today}</title></head>
<body style="margin:0;padding:20px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#f7f7f5;color:#222;">
  <div style="max-width:780px;margin:0 auto;background:white;border-radius:12px;padding:24px;">
    <h1 style="margin:0 0 4px;font-size:20px;font-weight:600;">Dražbe u najavi {today}</h1>
    <p style="margin:0 0 20px;color:#666;font-size:13px;">FINA Očevidnik s AI scoringom</p>

    <table role="presentation" style="width:100%;border-collapse:collapse;margin-bottom:24px;">
      <tr>
        <td style="width:33%;padding:10px 12px;background:#f1efe8;border-radius:8px;text-align:center;">
          <div style="font-size:20px;font-weight:600;">{meta["total_active"]}</div>
          <div style="font-size:11px;color:#666;text-transform:uppercase;">aktivnih</div>
        </td>
        <td style="width:1%;"></td>
        <td style="width:33%;padding:10px 12px;background:#eaf3de;border-radius:8px;text-align:center;">
          <div style="font-size:20px;font-weight:600;color:#27500A;">{len(new_items)}</div>
          <div style="font-size:11px;color:#27500A;text-transform:uppercase;">novih od jučer</div>
        </td>
        <td style="width:1%;"></td>
        <td style="width:33%;padding:10px 12px;background:#fcebeb;border-radius:8px;text-align:center;">
          <div style="font-size:20px;font-weight:600;color:#791F1F;">{len(urgent_items)}</div>
          <div style="font-size:11px;color:#791F1F;text-transform:uppercase;">hitnih jamčevina</div>
        </td>
      </tr>
    </table>

    {ovrh_html}

    <h2 style="font-size:15px;font-weight:600;margin:24px 0 8px;border-left:3px solid #E24B4A;padding-left:10px;">
      Jamčevina dospijeva u sljedećih 7 dana
    </h2>
    <table role="presentation" style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead>
        <tr style="background:#fafaf8;text-align:left;">
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;">runda/AI</th>
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;">lokacija</th>
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;">opis + AI analiza</th>
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;text-align:right;">cijena</th>
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;">datumi</th>
        </tr>
      </thead>
      <tbody>{urgent_html}</tbody>
    </table>

    <h2 style="font-size:15px;font-weight:600;margin:32px 0 8px;border-left:3px solid #185FA5;padding-left:10px;">
      Novo od jučer
    </h2>
    <table role="presentation" style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead>
        <tr style="background:#fafaf8;text-align:left;">
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;">runda/AI</th>
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;">lokacija</th>
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;">opis + AI analiza</th>
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;text-align:right;">cijena</th>
          <th style="padding:8px;font-weight:500;font-size:10px;color:#888;text-transform:uppercase;">datumi</th>
        </tr>
      </thead>
      <tbody>{new_html}</tbody>
    </table>

    {deep_dive_html}

    <p style="margin-top:24px;padding-top:16px;border-top:1px solid #eee;font-size:11px;color:#888;">
      Puni interaktivni dashboard: {dashboard_link}<br>
      Generirao PONIP Monitor preko GitHub Actions {datetime.now().strftime("%Y-%m-%d %H:%M")}
    </p>
  </div>
</body>
</html>"""


def _write_dashboard(records: list[dict], cutoff_days: int,
                     ovrh_summary: dict | None = None) -> None:
    new_records = [r for r in records if r["nova"]]
    urgent_records = [r for r in records if r["urgent"]]
    meta = {
        "generated_at": datetime.now().isoformat(),
        "today": datetime.now().strftime("%Y-%m-%d"),
        "total_active": len(records),
        "new_count": len(new_records),
        "urgent_count": len(urgent_records),
        "alert_days": cutoff_days,
        "scored_count": sum(1 for r in records if r.get("score")),
        "detailed_count": sum(1 for r in records if r.get("detailed")),
        # Javni dashboard: SAMO agregat po kategorijama, nikad imena/OIB-i
        "ovrh_summary": ovrh_summary or {},
    }
    html = build_dashboard(records, meta)
    DASHBOARD_OUT.write_text(html, encoding="utf-8")
    log.info(f"Dashboard: {DASHBOARD_OUT} ({DASHBOARD_OUT.stat().st_size / 1024:.0f} KB)")


# ============================================================================
# MAIN
# ============================================================================

def main():
    log.info("=== PONIP Monitor v3 (GHA) start ===")
    log.info(f"BASE_DIR: {BASE_DIR}")
    log.info(f"DASHBOARD_OUT: {DASHBOARD_OUT}")
    cfg = load_config()

    try:
        today_path = download_csv()
    except Exception as e:
        log.exception(f"Download failed: {e}")
        sys.exit(2)

    current = load_snapshot(today_path)
    log.info(f"Snimak: {len(current):,} redaka")

    prev_path = find_previous(today_path)
    previous = load_snapshot(prev_path) if prev_path else None
    if previous is not None:
        log.info(f"Previous snapshot: {prev_path.name}")
    else:
        log.info("Nema previous snapshot-a — prvi run. Ništa neće biti označeno kao 'novo'.")

    active = filter_active(current)
    log.info(f"Aktivnih: {len(active):,}")

    if previous is not None:
        prev_active = filter_active(previous)
        prev_ids = set(prev_active["ID nadmetanja"].astype(str))
        new_ids = set(active["ID nadmetanja"].astype(str)) - prev_ids
    else:
        # Prvi run: nemamo referenciju, ne označavamo ništa kao "novo"
        # Inače bi jutro prvog run-a bilo poplavljeno s ~3000 stavki
        new_ids = set()
    log.info(f"Novih ID-ova: {len(new_ids)}")

    cutoff_days = cfg.get("alert_jamcevina_dana", 7)
    cutoff = datetime.now() + timedelta(days=cutoff_days)
    records = [to_record(row, new_ids, cutoff) for _, row in active.iterrows()]
    skipped_no_id = sum(1 for r in records if not r["id"])
    if skipped_no_id:
        log.warning(f"Preskačem {skipped_no_id} record-a bez ID-a (FINA CSV nema 'ID nadmetanja')")
        records = [r for r in records if r["id"]]

    # Live podaci s FINA-e (broj sudionika + trenutna cijena + UUID)
    live = fetch_live_tijek_data()
    matched = 0
    for r in records:
        d = live.get(r["id"]) if live else None
        if d:
            r["sudionici"] = d.get("sudionici")
            r["trenutnaCijena"] = d.get("trenutnaCijena")
            r["uuid"] = d.get("uuid")
            matched += 1
        # FINA detail URL ako imamo UUID, inače Google search fallback
        if r["uuid"]:
            r["finaUrl"] = FINA_DETAIL_URL_FMT.format(uuid=r["uuid"])
        elif r["id"]:
            r["finaUrl"] = f"https://www.google.com/search?q=ponip.fina.hr+{r['id']}"
    if live:
        log.info(f"FINA live merge: {matched}/{len(records)} record-a uparili s live podacima")

    # OVRHOVODITELJI (v4): klasifikacija vjerovnika za filtrirane dražbe.
    # Rezultat NAMJERNO ne ide u records (dashboard payload je javan —
    # osobni podaci ostaju u data/ovrh + privatnom emailu); u dashboard
    # meta ide samo agregat po kategorijama.
    ovrh_map: dict[str, dict] = {}
    ovrh_summary: dict[str, int] = {}
    if (cfg.get("ovrhovoditelj_filter") or {}).get("enabled"):
        log.info("Pokrećem detekciju ovrhovoditelja...")
        try:
            from ovrhovoditelj import process_ovrhovoditelji, summary_counts
            ovrh_map = process_ovrhovoditelji(records, cfg)
            ovrh_summary = summary_counts(ovrh_map)
            log.info(f"Ovrhovoditelji agregat: {ovrh_summary}")
        except Exception as e:
            log.exception(f"Detekcija ovrhovoditelja pala: {e} — nastavljam bez nje")

    # PRE-SCORING: napuni iz cache-a pa napiši dashboard odmah (safety-net
    # za slučaj da scoring padne na timeoutu — makar imamo svježi dashboard
    # s današnjim recordima i postojećim cached scoreovima).
    for r in records:
        r["score"] = load_cached(r["id"], detailed=False, record=r)
        r["detailed"] = load_cached(r["id"], detailed=True, record=r)
    _write_dashboard(records, cutoff_days)

    # AI SCORING
    if cfg.get("scoring_enabled", True):
        log.info("Pokrećem AI scoring...")
        try:
            basic_scores, detailed_scores = score_items(
                records,
                max_basic=cfg.get("max_basic_per_run", 100),
                max_detailed=cfg.get("max_detailed_per_run", 10),
                batch_size=cfg.get("batch_size_basic", 15),
            )
            for r in records:
                r["score"] = basic_scores.get(r["id"]) or r["score"]
                r["detailed"] = detailed_scores.get(r["id"]) or r["detailed"]
            log.info(f"Scored: {len(basic_scores)} basic, {len(detailed_scores)} detailed")
        except Exception as e:
            log.exception(f"Scoring failed: {e} — continuing without scores")
    else:
        log.info("Scoring disabled u configu")

    # Finalni dashboard (sa svježim scoreovima iz ove iteracije)
    _write_dashboard(records, cutoff_days, ovrh_summary)

    new_records = [r for r in records if r["nova"]]
    urgent_records = [r for r in records if r["urgent"]]
    # Email: obje sekcije po AI ocjeni (najbolja prva); tiebreaker za hitne
    # je rok jamčevine, za nove početak nadmetanja
    urgent_records.sort(key=lambda r: (-ai_score(r), r["rokJamcevina"] or "9999"))
    new_records.sort(key=lambda r: (-ai_score(r), r["pocetakNadm"] or "9999"))

    meta = {
        "generated_at": datetime.now().isoformat(),
        "today": datetime.now().strftime("%Y-%m-%d"),
        "total_active": len(records),
        "new_count": len(new_records),
        "urgent_count": len(urgent_records),
        "alert_days": cutoff_days,
        "scored_count": sum(1 for r in records if r["score"]),
        "detailed_count": sum(1 for r in records if r["detailed"]),
    }

    # Email arhiva
    public_url = os.environ.get("PONIP_PUBLIC_URL")
    email_html = build_email_html(
        new_records, urgent_records, meta, public_url,
        ovrh_map=ovrh_map, all_records=records,
    )
    email_path = REPORTS_DIR / f"email_{meta['today']}.html"
    email_path.write_text(email_html, encoding="utf-8")
    log.info(f"Email arhiva: {email_path}")

    # Prune old snapshots (repo size control)
    prune_old_snapshots(keep_last=14)

    if DRY_RUN:
        log.info("DRY_RUN=1 — email se ne šalje")
        return

    to_addr = os.environ.get("PONIP_EMAIL_TO")
    if not to_addr:
        log.warning("PONIP_EMAIL_TO nije postavljen — preskačem slanje")
        return

    subject = (
        f"PONIP dražbe {datetime.now().strftime('%d.%m.%Y')} "
        f"({len(new_records)} novih, {len(urgent_records)} hitnih"
        + (f", {len(ovrh_map)} vjerovnik-match" if ovrh_map else "")
        + ")"
    )
    try:
        send_email(subject, email_html, to_addr)
        log.info(f"Email poslan na {to_addr}")
    except Exception as e:
        log.exception(f"Email slanje palo: {e}")
        sys.exit(3)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
