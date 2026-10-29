#!/usr/bin/env python3
"""Unit testovi za ovrhovoditelj.py (testovi iz briefa + rubni slučajevi).

Pokretanje:  python test_ovrhovoditelj.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ovrhovoditelj import (
    valid_oib, parse_stranke, classify, load_registry,
    match_nekretnina_filter, pick_poziv,
)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"OK   {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILS.append(name)


# --------------------------------------------------------------------------
# 1. OIB validacija (ISO 7064 MOD 11,10) — testovi iz briefa
# --------------------------------------------------------------------------
check("OIB 04315656111 prolazi", valid_oib("04315656111"))
check("OIB 04315656112 ne prolazi", not valid_oib("04315656112"))
check("OIB B2 KAPITAL prolazi", valid_oib("57509775367"))
check("OIB Zaba prolazi", valid_oib("92963223473"))
check("prekratak OIB ne prolazi", not valid_oib("1234567890"))
check("None/prazno ne prolazi", not valid_oib("") and not valid_oib(None))

# --------------------------------------------------------------------------
# 2. Parser — primjer iz briefa (ID 61476, OVR-157/2022, OS Pula)
# --------------------------------------------------------------------------
PRIMJER = """
Financijska agencija, na temelju zaključka Općinskog suda u Puli-Pola, u
pravnoj stvari ovrhovoditelja
    PERO PRIMJERKO, 51211 MATULJI, BRANKA LAGINJE 16, OIB: 04315656111
protiv ovršenika
    MARA MODELIĆ, 52100 PULA, VERUDELLA 8, OIB: 55555666667, zastupanog po
    punomoćniku
objavljuje se Poziv na sudjelovanje u elektroničkoj javnoj dražbi
"""
s = parse_stranke(PRIMJER)
check("primjer: parse_ok", s["parse_ok"])
check("primjer: 1 ovrhovoditelj", len(s["ovrhovoditelji"]) == 1, str(s["ovrhovoditelji"]))
o = s["ovrhovoditelji"][0] if s["ovrhovoditelji"] else {}
check("primjer: naziv PERO PRIMJERKO", o.get("naziv") == "PERO PRIMJERKO", repr(o.get("naziv")))
check("primjer: OIB 04315656111", o.get("oib") == "04315656111")
check("primjer: ovršenik MARA MODELIĆ",
      s["ovrsenici"] and s["ovrsenici"][0]["naziv"] == "MARA MODELIĆ", str(s["ovrsenici"]))

registry = load_registry()
check("registar učitan (44 OIB-a)", len(registry) == 44, f"got {len(registry)}")
check("primjer: kategorija privatna_osoba",
      classify(o.get("naziv", ""), o.get("oib"), registry) == "privatna_osoba")

# --------------------------------------------------------------------------
# 3. Klasifikacija — testovi iz briefa + oblici
# --------------------------------------------------------------------------
check("57509775367 -> agencija", classify("B2 KAPITAL d.o.o.", "57509775367", registry) == "agencija")
check("92963223473 -> banka", classify("Zagrebačka banka d.d.", "92963223473", registry) == "banka")
check("52634238587 -> drzava", classify("REPUBLIKA HRVATSKA", "52634238587", registry) == "drzava")
check("nepoznata firma d.o.o. -> firma_ostalo",
      classify("NEKA FIRMA d.o.o.", "11111111119", registry) == "firma_ostalo")
check("S.A. -> firma_ostalo", classify("XYZ CAPITAL S.A.", "11111111119", registry) == "firma_ostalo")
check("GmbH -> firma_ostalo", classify("ALPHA GmbH", "11111111119", registry) == "firma_ostalo")
check("fizička osoba -> privatna_osoba", classify("IVAN HORVAT", "11111111119", registry) == "privatna_osoba")
check("prazan naziv -> nepoznato", classify("", "11111111119", registry) == "nepoznato")

# --------------------------------------------------------------------------
# 4. Parser — varijante (ovrhovoditeljica, predlagatelj osiguranja,
#    stečajni dužnik, više ovrhovoditelja)
# --------------------------------------------------------------------------
V1 = "u pravnoj stvari ovrhovoditeljice ANA ANIĆ, 10000 ZAGREB, ILICA 1, OIB: 04315656111 protiv ovršenika PERO PERIĆ, OIB: 55555666667, radi naplate"
s = parse_stranke(V1)
check("ovrhovoditeljica varijanta", s["parse_ok"] and s["ovrhovoditelji"][0]["naziv"] == "ANA ANIĆ", str(s))

V2 = "u pravnoj stvari predlagatelja osiguranja ERSTE&STEIERMARKISCHE BANK d.d., RIJEKA, OIB: 23057039320 protiv protustranke NN, OIB: 55555666667, radi osiguranja"
s = parse_stranke(V2)
check("predlagatelj osiguranja varijanta",
      s["parse_ok"] and s["ovrhovoditelji"][0]["oib"] == "23057039320", str(s))
check("predlagatelj -> banka", classify(s["ovrhovoditelji"][0]["naziv"], "23057039320", registry) == "banka")

V3 = "u postupku stečajnog dužnika PROPALA FIRMA d.o.o., OSIJEK, OIB: 04315656111 objavljuje se poziv"
s = parse_stranke(V3)
check("stečajni dužnik varijanta", s["parse_ok"] and s["ovrhovoditelji"][0]["uloga"] == "stečajni dužnik", str(s))

V4 = ("u pravnoj stvari ovrhovoditelja 1. B2 KAPITAL d.o.o., ZAGREB, OIB: 57509775367, "
      "2. H-ABDUCO d.o.o., ZAGREB, OIB: 13667298928 protiv ovršenika PERO PERIĆ, OIB: 55555666667")
s = parse_stranke(V4)
check("dva ovrhovoditelja", len(s["ovrhovoditelji"]) == 2, str(s["ovrhovoditelji"]))
check("drugi ovrhovoditelj H-ABDUCO",
      len(s["ovrhovoditelji"]) == 2 and s["ovrhovoditelji"][1]["naziv"] == "H-ABDUCO d.o.o.",
      str(s["ovrhovoditelji"]))

# Stvarna stečajna formulacija (iz PDF-a poziva ST-9999/2025)
V7 = ("povodom zahtjeva za prodaju kojeg je dostavio Trgovački sud u Zagrebu poslovni broj "
      "ST-9999/2025 u stečajnom postupku nad dužnikom PRIMJER GRADNJA d.o.o. za proizvodnju, "
      "trgovinu i usluge u stečaju, 10163 ZAGREB KULMERSKA ULICA 30, OIB: 12345678903, "
      "zastupanom po stečajnom upravitelju IVA UPRAVITELJ, 10000 ZAGREB, OIB: 98765432106 "
      "radi provedbe prodaje u stečajnom postupku")
s = parse_stranke(V7)
check("stečaj 'nad dužnikom': parse_ok", s["parse_ok"], str(s))
check("stečaj: dužnik iz poziva, ne upravitelj",
      len(s["ovrhovoditelji"]) == 1 and s["ovrhovoditelji"][0]["oib"] == "12345678903",
      str(s["ovrhovoditelji"]))
check("stečaj: uloga 'stečajni dužnik'",
      s["ovrhovoditelji"] and s["ovrhovoditelji"][0]["uloga"] == "stečajni dužnik")

# Stečajna masa + pravni oblik ispisan riječima
check("'Stečajna masa iza X društvo s ograničenom odgovornošću' -> firma_ostalo",
      classify("Stečajna masa iza XY GRADNJA društvo s ograničenom odgovornošću",
               "55555666667", registry) == "firma_ostalo")

V5 = "nema nikakvih stranaka u ovom tekstu"
s = parse_stranke(V5)
check("tekst bez stranaka -> parse_ok False", not s["parse_ok"])

# OIB s neispravnom kontrolnom znamenkom u PDF-u se preskače
V6 = "u pravnoj stvari ovrhovoditelja KRIVI OIB d.o.o., ZAGREB, OIB: 04315656112 protiv ovršenika X, OIB: 55555666667"
s = parse_stranke(V6)
check("neispravan OIB entitet se preskače", not s["parse_ok"], str(s))

# --------------------------------------------------------------------------
# 5. Filter nekretnina
# --------------------------------------------------------------------------
NK = {
    "kljucne_rijeci": ["stan", "kuća", "apartman"],
    "zupanije": ["Istarska", "Primorsko-goranska", "Grad Zagreb i okolica"],
    "min_vrijednost_eur": 50000,
    "max_vrijednost_eur": None,
}
R_OK = {"zupanija": "Istarska", "vrijednost": 157245, "vrsta": "nekretnina",
        "opis": "Trosoban stan u Puli, 67,07 m2, prvi kat"}
check("filter: stan u Puli prolazi", match_nekretnina_filter(R_OK, NK))
check("filter: kuća s dijakriticima",
      match_nekretnina_filter({**R_OK, "opis": "Obiteljska kuća u Rovinju"}, NK))
check("filter: 'kuca' bez dijakritika",
      match_nekretnina_filter({**R_OK, "opis": "Obiteljska kuca u Rovinju"}, NK))
check("filter: kriva županija pada",
      not match_nekretnina_filter({**R_OK, "zupanija": "Osječko-baranjska"}, NK))
check("filter: ispod min vrijednosti pada",
      not match_nekretnina_filter({**R_OK, "vrijednost": 30000}, NK))
check("filter: oranica pada (nema ključne riječi)",
      not match_nekretnina_filter({**R_OK, "opis": "oranica i livada u k.o. Žejane"}, NK))
check("filter: 'stanje' nije 'stan'",
      not match_nekretnina_filter({**R_OK, "opis": "objekt u lošem stanje, zemljište"}, NK))

# --------------------------------------------------------------------------
# 6. pick_poziv
# --------------------------------------------------------------------------
ROWS = [
    {"sifVrPism": 27, "sts": 8, "datObjaveFmt": "01.10.2026"},
    {"sifVrPism": 25, "sts": 10, "datObjaveFmt": "05.10.2026"},  # poništen
    {"sifVrPism": 25, "sts": 8, "datObjaveFmt": "03.10.2026"},
    {"sifVrPism": 25, "sts": 8, "datObjaveFmt": "07.10.2026"},
]
p = pick_poziv(ROWS)
check("pick_poziv: najnoviji važeći", p and p["datObjaveFmt"] == "07.10.2026", str(p))
check("pick_poziv: bez poziva -> None", pick_poziv([{"sifVrPism": 27, "sts": 8}]) is None)

print()
print("SVE OK" if not FAILS else f"{len(FAILS)} FAILOVA: {FAILS}")
sys.exit(1 if FAILS else 0)
