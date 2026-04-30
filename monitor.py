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
    Aktivnih dražba = one kod kojih:
      - nadmetanje još nije završilo (ili nema datuma završetka), I
      - rok za uplatu jamčevine još nije prošao (ako je definiran).

    Ako je rok jamčevine prošao, ne možeš više sudjelovati pa nema smisla
    da se dražba prikazuje u listi.
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ok = (
        df["Datum i vrijeme završetka nadmetanja"].isna()
        | (df["Datum i vrijeme završetka nadmetanja"] > now - timedelta(days=1))
    )
    jamc_ok = (
        df["Datum valute jamčevine"].isna()
        | (df["Datum valute jamčevine"] >= today_start)
    )
    return df[end_ok & jamc_ok].copy()


# --- FINA live data (broj sudionika, trenutna cijena) ---------------------

FINA_TIJEK_URL = "https://ponip.fina.hr/ocevidnik-web/pregled/tijek"
FINA_LISTING_URL = "https://ponip.fina.hr/ocevidnik-web/pregled/nadmetanja-u-tijeku"
_TIJEK_RECORD_RE = re.compile(r'\{[^{}]*"idNdmt":(\d+)[^{}]*\}')


def fetch_live_tijek_data() -> dict[str, dict]:
    """
    Skida live podatke s FINA-e za sva nadmetanja u tijeku.
    Vraća dict {id: {'sudionici': N, 'trenutnaCijena': X, 'noBids': bool}}.
    Endpoint vrati cijelu listu odjednom — jedan HTTP po run-u.
    Ako padne (timeout, format change), vraća prazan dict — non-fatal.
    """
    try:
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 ponip-monitor"})
        # 1. session cookie
        sess.get(FINA_LISTING_URL, timeout=30)
        # 2. tijek HTML s embedded JSON-om
        r = sess.get(FINA_TIJEK_URL, timeout=60)
        r.raise_for_status()
        out: dict[str, dict] = {}
        for m in _TIJEK_RECORD_RE.finditer(r.text):
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
                "trenutnaCijena": rec.get("trenutnaCijena"),
                "noBids": rec.get("noBids"),
            }
        log.info(f"FINA tijek: {len(out)} dražbi s live podacima")
        return out
    except Exception as e:
        log.warning(f"Ne mogu skinuti FINA tijek podatke (sudionici): {e}")
        return {}


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
        "score": None,
        "detailed": None,
        "sudionici": None,            # popunjava se iz fetch_live_tijek_data
        "trenutnaCijena": None,       # iz iste live AJAX rute
        "finaUrl": (
            f"https://www.google.com/search?q=ponip.fina.hr+{id_}"
            if id_ else None
        ),
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


def build_email_html(new_items: list[dict], urgent_items: list[dict], meta: dict, public_url: str | None) -> str:
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


def _write_dashboard(records: list[dict], cutoff_days: int) -> None:
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

    # Live podaci s FINA-e (broj sudionika + trenutna cijena)
    live = fetch_live_tijek_data()
    if live:
        for r in records:
            d = live.get(r["id"])
            if d:
                r["sudionici"] = d.get("sudionici")
                r["trenutnaCijena"] = d.get("trenutnaCijena")

    # PRE-SCORING: napuni iz cache-a pa napiši dashboard odmah (safety-net
    # za slučaj da scoring padne na timeoutu — makar imamo svježi dashboard
    # s današnjim recordima i postojećim cached scoreovima).
    for r in records:
        r["score"] = load_cached(r["id"], detailed=False)
        r["detailed"] = load_cached(r["id"], detailed=True)
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
    _write_dashboard(records, cutoff_days)

    new_records = [r for r in records if r["nova"]]
    urgent_records = [r for r in records if r["urgent"]]
    urgent_records.sort(key=lambda r: r["rokJamcevina"] or "")
    new_records.sort(
        key=lambda r: (
            -((r.get("score") or {}).get("score_overall") or 0),
            r["pocetakNadm"] or "9999",
        )
    )

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
    email_html = build_email_html(new_records, urgent_records, meta, public_url)
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
        f"({len(new_records)} novih, {len(urgent_records)} hitnih)"
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
