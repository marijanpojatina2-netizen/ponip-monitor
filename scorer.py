#!/usr/bin/env python3
"""
scorer.py — AI scoring za PONIP nekretnine (GHA edition).

Koristi `claude --print` s CLAUDE_CODE_OAUTH_TOKEN env var — Max subscription.
VAŽNO: Workflow mora NE postaviti ANTHROPIC_API_KEY, inače će biti billing preko API-ja.
"""

import subprocess
import json
import hashlib
import logging
import os
from pathlib import Path
from datetime import datetime

log = logging.getLogger("scorer")

BASE_DIR = Path(os.environ.get("PONIP_BASE", Path(__file__).resolve().parent))
SCORES_DIR = BASE_DIR / "scores"
BASIC_DIR = SCORES_DIR / "basic"
DETAILED_DIR = SCORES_DIR / "detailed"
BASIC_DIR.mkdir(parents=True, exist_ok=True)
DETAILED_DIR.mkdir(parents=True, exist_ok=True)

DETAILED_THRESHOLD_EUR = 500_000
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT_BASIC = 360
CLAUDE_TIMEOUT_DETAILED = 360


# ============================================================================
# CACHE
# ============================================================================

def input_hash(record: dict) -> str:
    key = f"{record.get('pocetna')}-{record.get('runda')}-{record.get('opis', '')[:300]}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def basic_path(id_: str) -> Path:
    return BASIC_DIR / f"{id_}.json"


def detailed_path(id_: str) -> Path:
    return DETAILED_DIR / f"{id_}.json"


def load_cached(id_: str, detailed: bool) -> dict | None:
    path = detailed_path(id_) if detailed else basic_path(id_)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_score(record: dict, score: dict, detailed: bool):
    score["input_hash"] = input_hash(record)
    score["scored_at"] = datetime.now().isoformat()
    path = detailed_path(record["id"]) if detailed else basic_path(record["id"])
    path.write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")


def needs_rescoring(record: dict, cached: dict | None) -> bool:
    return cached is None or cached.get("input_hash") != input_hash(record)


# ============================================================================
# CLAUDE WRAPPER
# ============================================================================

def call_claude(prompt: str, timeout: int, allow_web: bool = False) -> str:
    # SAFETY: ako je ANTHROPIC_API_KEY postavljen, upozorimo i obrišemo — idemo preko OAuth
    env = os.environ.copy()
    if env.pop("ANTHROPIC_API_KEY", None):
        log.warning("ANTHROPIC_API_KEY je bio postavljen — uklonjen da se koristi OAuth token")
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        log.error("CLAUDE_CODE_OAUTH_TOKEN nije postavljen — claude --print neće raditi bez autentifikacije!")

    cmd = [CLAUDE_BIN, "--print"]
    if allow_web:
        cmd += ["--allowed-tools", "web_search"]
    # Prompt ide preko stdin-a umjesto pozicijskog argumenta — izbjegava probleme
    # s --allowed-tools koji konzumira previše argumenata, i podržava jako dugačke promptove
    log.debug(f"claude call: {len(prompt)} chars prompt via stdin, web={allow_web}")
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude exit {result.returncode}: {result.stderr[:500]}")
    return result.stdout


def extract_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rsplit("```", 1)[0]
    t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in [("[", "]"), ("{", "}")]:
        start = t.find(open_c)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(t)):
            if t[i] == open_c:
                depth += 1
            elif t[i] == close_c:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"Ne mogu parsirati JSON iz: {text[:300]}")


# ============================================================================
# PROMPTS
# ============================================================================

BASIC_INSTRUCTIONS = """Ti si iskusni hrvatski analitičar nekretnina s 15 godina iskustva.
Procjenjuješ nekretnine iz javnih dražbi FINA za investitora.

KRITERIJI (svi 0-10, decimale .5 dozvoljene):

1. LOKACIJA:
   - 8-10: tražena turistička zona (Istra obala, otoci, Dubrovnik, Hvar, Brač)
   - 6-8: veći gradovi (Zagreb, Split, Rijeka, Zadar centar)
   - 4-6: manji gradovi, periferije velikih gradova
   - 1-4: ruralno, depopulacijska područja, slabija Slavonija

2. ROI POTENCIJAL:
   - Popust od procjene: >40% = 8+, 20-40% = 6-8, <20% = 4-6
   - Treća/četvrta dražba daje dodatnih +0.5 do +1
   - Likvidnost regije utječe
   - Vrsta predmeta (nekretnina > pokretnina za dugoročni ROI)

3. STANJE (iz opisa):
   - 8+: useljivo, renovirano, bazen, dobra infrastruktura
   - 5-7: standardno, manji zahvati
   - 2-4: za temeljito renoviranje, ruševno
   - 1-3: neuseljivo, nerealizirani objekti

4. FLAGS (kratki opisi red flag-ova, ako postoje):
   - "u zakupu / u najmu"
   - "osoba stanuje"
   - "prava koja ne prestaju prodajom"
   - "sporno vlasništvo"
   - specifični rizici iz opisa

RECOMMENDATION: "pass" | "watch" | "bid" | "deep_dive"
- pass: slab investicijski case
- watch: prati, ne ulazi aktivno
- bid: realan kandidat za licitaciju
- deep_dive: veliki potencijal, zaslužuje detaljnu analizu"""


def build_basic_batch_prompt(records: list[dict]) -> str:
    blocks = []
    for i, r in enumerate(records, 1):
        discount = None
        if r.get("vrijednost") and r.get("pocetna"):
            try:
                discount = round((1 - r["pocetna"] / r["vrijednost"]) * 100, 1)
            except ZeroDivisionError:
                discount = None
        blocks.append(
            f"""=== NEKRETNINA {i} (ID: {r['id']}) ===
Sud: {r.get('sud', '—')}
Vrsta: {r.get('vrsta', '—')}
Redni broj dražbe: {r.get('runda', '—')}
Utvrđena vrijednost: {r.get('vrijednost', 'N/A')} EUR
Početna cijena: {r.get('pocetna', 'N/A')} EUR
Popust od procjene: {discount}%
Jamčevina: {r.get('jamcevina', 'N/A')} EUR
Opis: {r.get('opis', '')}"""
        )

    return f"""{BASIC_INSTRUCTIONS}

Procijeni sljedećih {len(records)} nekretnina. Za SVAKU vrati jedan JSON objekt u istom JSON array-u.

NEKRETNINE:

{chr(10).join(blocks)}

VRATI SAMO JSON ARRAY (bez markdown, bez objašnjenja, bez code fence):
[
  {{"id": "<ID>", "score_overall": <0-10>, "score_lokacija": <0-10>, "score_roi": <0-10>, "score_stanje": <0-10>, "recommendation": "pass|watch|bid|deep_dive", "reasoning": "<2 rečenice u jednoj liniji>", "flags": ["<flag>", ...]}},
  ...
]"""


def build_detailed_prompt(r: dict) -> str:
    discount = None
    if r.get("vrijednost") and r.get("pocetna"):
        try:
            discount = round((1 - r["pocetna"] / r["vrijednost"]) * 100, 1)
        except ZeroDivisionError:
            discount = None

    return f"""{BASIC_INSTRUCTIONS}

DETALJNA ANALIZA — NEKRETNINA VRIJEDNOSTI >500K EUR.
Koristi web_search tool za istraživanje tržišta:
- Pretraži prosječne cijene po m² u mikrolokaciji
- Nađi 3-5 sličnih aktivnih oglasa na nekretnine.net, njuskalo.hr, index.hr oglasi
- Procijeni trendove u regiji za 2025-2026
- Budi konkretan — navodi stvarne brojke i URL-ove gdje je moguće

PODACI O NEKRETNINI:
ID: {r['id']}
Sud: {r.get('sud', '—')}
Vrsta: {r.get('vrsta', '—')}
Redni broj dražbe: {r.get('runda', '—')}
Utvrđena vrijednost: {r.get('vrijednost')} EUR
Početna cijena: {r.get('pocetna')} EUR
Popust: {discount}%
Jamčevina: {r.get('jamcevina')} EUR
Opis: {r.get('opis', '')}
Razgledavanje: {r.get('razgledavanje', '—')}

VRATI SAMO JSON (bez markdown, bez objašnjenja):
{{
  "id": "{r['id']}",
  "score_overall": <0-10>,
  "score_lokacija": <0-10>,
  "score_roi": <0-10>,
  "score_stanje": <0-10>,
  "recommendation": "pass|watch|bid|deep_dive",
  "reasoning": "<3-4 rečenice>",
  "flags": ["..."],
  "market_estimate": {{
    "eur_min": <int>,
    "eur_max": <int>,
    "confidence": "low|medium|high",
    "basis": "<kratko objašnjenje na čemu se procjena bazira>"
  }},
  "comparables": [
    {{"opis": "<kratki opis>", "cijena": <int>, "m2": <int ili null>, "izvor": "<portal>", "url": "<url>"}}
  ],
  "investment_strategy": "<kratkoročna preprodaja / dugoročno iznajmljivanje / reno-flip / razvoj + obrazloženje>",
  "entry_price_eur": <maks. cijena po kojoj je deal atraktivan>,
  "detailed_risks": ["<specifičan rizik s obrazloženjem>", ...],
  "upside_scenario": "<optimistični scenarij 2-3 god>",
  "downside_scenario": "<što ako ne prođe>"
}}"""


# ============================================================================
# SCORING
# ============================================================================

def score_basic_batch(records: list[dict]) -> list[dict]:
    if not records:
        return []
    prompt = build_basic_batch_prompt(records)
    raw = call_claude(prompt, timeout=CLAUDE_TIMEOUT_BASIC, allow_web=False)
    parsed = extract_json(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"Očekivan JSON array, dobio {type(parsed).__name__}")
    by_id = {str(s.get("id")): s for s in parsed if isinstance(s, dict)}
    return [by_id[r["id"]] for r in records if r["id"] in by_id]


def score_detailed_one(r: dict) -> dict:
    prompt = build_detailed_prompt(r)
    raw = call_claude(prompt, timeout=CLAUDE_TIMEOUT_DETAILED, allow_web=True)
    result = extract_json(raw)
    if not isinstance(result, dict):
        raise ValueError(f"Očekivan JSON objekt, dobio {type(result).__name__}")
    return result


def priority_basic(r: dict):
    return (
        0 if r.get("nova") else 1,
        0 if r.get("urgent") else 1,
        -(r.get("vrijednost") or 0),
    )


def score_items(
    records: list[dict],
    max_basic: int = 100,
    max_detailed: int = 10,
    batch_size: int = 15,
) -> tuple[dict, dict]:
    basic: dict[str, dict] = {}
    detailed: dict[str, dict] = {}
    needs_basic: list[dict] = []
    needs_detailed: list[dict] = []

    for r in records:
        cached_b = load_cached(r["id"], detailed=False)
        if cached_b and not needs_rescoring(r, cached_b):
            basic[r["id"]] = cached_b
        else:
            needs_basic.append(r)

        if (r.get("vrijednost") or 0) >= DETAILED_THRESHOLD_EUR:
            cached_d = load_cached(r["id"], detailed=True)
            if cached_d and not needs_rescoring(r, cached_d):
                detailed[r["id"]] = cached_d
            else:
                needs_detailed.append(r)

    needs_basic.sort(key=priority_basic)
    needs_detailed.sort(key=lambda r: -(r.get("vrijednost") or 0))

    to_basic = needs_basic[:max_basic]
    to_detailed = needs_detailed[:max_detailed]
    log.info(
        f"Scoring queue: basic={len(to_basic)}/{len(needs_basic)} "
        f"detailed={len(to_detailed)}/{len(needs_detailed)}"
    )

    for i in range(0, len(to_basic), batch_size):
        batch = to_basic[i:i + batch_size]
        try:
            scores = score_basic_batch(batch)
            for r in batch:
                s = next((x for x in scores if str(x.get("id")) == r["id"]), None)
                if s:
                    save_score(r, s, detailed=False)
                    basic[r["id"]] = s
                else:
                    log.warning(f"Basic score missing za ID {r['id']} u batch-u")
            log.info(f"Batch {i // batch_size + 1}: scored {len(scores)}/{len(batch)}")
        except subprocess.TimeoutExpired:
            log.error(f"Timeout na batch-u od {len(batch)} — preskačem")
        except Exception as e:
            log.exception(f"Batch failed: {e}")

    for r in to_detailed:
        try:
            score = score_detailed_one(r)
            save_score(r, score, detailed=True)
            detailed[r["id"]] = score
            log.info(f"Detailed OK: ID {r['id']} (vrijednost {r.get('vrijednost'):,.0f})")
        except subprocess.TimeoutExpired:
            log.error(f"Detailed timeout za ID {r['id']}")
        except Exception as e:
            log.exception(f"Detailed failed za ID {r['id']}: {e}")

    return basic, detailed
