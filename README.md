# PONIP Monitor — GitHub Actions edition

Autonomni dnevni monitoring javnih dražbi FINA s AI scoringom, hostan na **GitHub Actions + GitHub Pages**.

**Trošak:** €0 — sve u free tier-u (public repo = unlimited Actions minuta, GitHub Pages free).

**Kvaliteta:** identična VPS verziji — isti Claude Opus model preko tvog Max plan-a.

## Arhitektura

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Actions (cron Mon-Fri 06:00 UTC ~ 07:00 Zagreb)      │
│                                                              │
│  1. Checkout repo                                            │
│  2. Install Python, Node.js, Claude Code CLI                 │
│  3. Validate auth (OAuth token OK, API key NIJE postavljen)  │
│  4. python monitor.py                                        │
│       ├── skine CSV s ponip.fina.hr                          │
│       ├── usporedi s jučerašnjim snimkom (iz cache-a)        │
│       ├── claude --print (batch 15) → basic scoring          │
│       ├── claude --print --allowed-tools web_search          │
│       │     → detailed scoring za nekretnine >500k           │
│       ├── generira docs/index.html (dashboard)               │
│       └── SMTP pošalje digest email                          │
│  5. Commit scores/, docs/, reports/ back to repo             │
│  6. Deploy GitHub Pages → dashboard live                     │
└──────────────────────────────────────────────────────────────┘
```

## Struktura repoa

```
ponip-monitor/
├── .github/workflows/
│   └── ponip-monitor.yml       # GHA workflow
├── monitor.py                  # orkestrator
├── scorer.py                   # AI scoring via claude --print
├── emailer.py                  # SMTP slanje
├── dashboard_template.html     # HTML template
├── config.yaml                 # scoring limiti
├── .env.example                # template za lokalne testove
├── .gitignore
├── README.md                   # ovaj file
├── docs/
│   └── index.html              # dashboard (GitHub Pages serve)
├── scores/
│   ├── basic/*.json            # keš basic scoringa (committed)
│   └── detailed/*.json         # keš detaljnih (committed)
└── reports/
    └── email_*.html            # arhiva poslanih mailova (committed)
```

**Ne commit-a se:**
- `data/` (CSV snapshots) — cache-iraju se preko GHA cache-a (privacy + repo size)
- `logs/` — GHA ima vlastiti log UI
- `.env` — lokalno-only

## Setup — 6 koraka

### 1. Stvori repo na GitHub-u

```
https://github.com/new
  Name:           ponip-monitor
  Visibility:     Public  ← mora biti public za besplatni unlimited Actions
  Don't initialize with README
```

Kloniraj lokalno:
```bash
git clone https://github.com/TVOJ-USERNAME/ponip-monitor.git
cd ponip-monitor
```

### 2. Dodaj fajlove i push

Otpakiraj zip ili kopiraj fajlove ovamo (monitor.py, scorer.py, emailer.py, dashboard_template.html, config.yaml, .env.example, .gitignore, README.md, .github/workflows/ponip-monitor.yml). Onda:

```bash
git add .
git commit -m "initial commit"
git push origin main
```

### 3. Generiraj Claude Max OAuth token

Ovo radiš LOKALNO (jednom, valjan 1 godinu):

```bash
# Ako nemaš Claude Code instaliran:
npm install -g @anthropic-ai/claude-code

# Uloguj se u Max account (interaktivno, browser flow):
claude
# (u claude-u: /login → slijedi OAuth)
# (onda: exit)

# Generiraj long-lived token:
claude setup-token
# → ispiše: sk-ant-oat01-xxxxx...xxxxx
# → SPREMI NEKUD SIGURNO, token se ne prikazuje više
```

**Zašto OAuth a ne API key:** `claude setup-token` generira OAuth token koji koristi tvoj Max subscription. `ANTHROPIC_API_KEY` bi trošio pay-per-token API kredit — **nemoj ga koristiti** (bio je slučaj gdje je Max korisnik dobio $1800+ billinga misleći da mu je Max dovoljan).

### 4. Dodaj GitHub Secrets

`Settings → Secrets and variables → Actions → New repository secret`

Dodaj SVE ove:

| Ime secreta | Vrijednost | Napomena |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | `sk-ant-oat01-...` | Iz koraka 3 |
| `SMTP_HOST` | `smtp.gmail.com` | ili tvoj SMTP |
| `SMTP_PORT` | `465` | SSL; ili 587 za STARTTLS |
| `SMTP_USER` | `tvoj@gmail.com` | |
| `SMTP_PASS` | `abcdxxxxxxxxxxxx` | **Gmail App Password** (16 znakova, bez razmaka) |
| `SMTP_FROM` | `tvoj@gmail.com` | obično isto kao USER |
| `SMTP_USE_TLS` | `0` | `0` za port 465, `1` za 587 |
| `PONIP_EMAIL_TO` | `marijan@firma.hr` | Kome ide digest |

**NEMOJ** dodavati `ANTHROPIC_API_KEY` — workflow će ga odbiti jer bi ukinuo Max billing.

#### Gmail App Password

1. https://myaccount.google.com/security → uključi 2-Step Verification
2. https://myaccount.google.com/apppasswords → "Generate app password"
3. Naziv: `PONIP Monitor`
4. Dobiješ 16 znamenki (ABCD EFGH IJKL MNOP) → paste u `SMTP_PASS` **bez razmaka**: `ABCDEFGHIJKLMNOP`

### 5. Aktiviraj GitHub Pages

`Settings → Pages`:
- **Source:** `GitHub Actions` (a ne "Deploy from branch")
- Klikni Save

Prvi workflow run će automatski deploy-ati Pages.

### 6. Prvi run

Idi u `Actions` tab → PONIP Monitor workflow → **Run workflow** (desni gumb)
- ✅ Označi "Dry run" za prvi put (ne šalje email, samo testira)
- Klikni "Run workflow"

Gledaj progress. Trebao bi potrošiti 15-25 min (zbog backfill scoringa za ~100 nekretnina).

Nakon uspjeha:
- Dashboard: https://TVOJ-USERNAME.github.io/ponip-monitor/
- Provjeri da je `docs/index.html` commit-an u repo
- Provjeri da je `scores/basic/*.json` commit-an (trebalo bi biti ~100 fajlova)

Ako sve OK, sljedeći scheduled run (sutra ujutro) šalje stvarni email.

## Operacije

### Ručno pokretanje bilo kada

`Actions` tab → PONIP Monitor → Run workflow → Run

### Promjena rasporeda

Edit `.github/workflows/ponip-monitor.yml`, linija `cron: '0 6 * * 1-5'`:

| Cron | Vrijeme Zagreb |
|---|---|
| `'0 5 * * 1-5'` | 06:00 zima / 07:00 ljeto, radni dani |
| `'0 6 * * 1-5'` | 07:00 zima / 08:00 ljeto, radni dani (default) |
| `'0 6 * * *'` | 07:00 zima / 08:00 ljeto, SVI dani |
| `'0 6,18 * * 1-5'` | 07:00 i 19:00 radni dani |

*GitHub Actions cron je UTC, Zagreb je UTC+1 (zima) / UTC+2 (ljeto).*

### Provjera statusa

`Actions` tab → klikni najnoviji run → vidiš sve step-ove s logovima.

Za detaljnije logove, download `logs-XXXX` artifact iz run-a.

### Promjena scoring limita

Edit `config.yaml`:
```yaml
max_basic_per_run: 100      # koliko basic scoringa po run-u
max_detailed_per_run: 10    # koliko detailed scoringa po run-u
batch_size_basic: 15        # koliko u jednom claude --print pozivu
```

Commit → push → sljedeći run pokupi novu config.

### Ručno brisanje cache-iranih scoringa (forsiraj re-scoring)

Lokalno:
```bash
rm -rf scores/basic/* scores/detailed/*
git add -A && git commit -m "reset scoring cache" && git push
```

Ili pojedinačno: `rm scores/basic/3506.json` + push.

### OAuth token istekao (svakih 12 mjeseci)

Lokalno:
```bash
claude setup-token
```

Onda GitHub `Settings → Secrets → CLAUDE_CODE_OAUTH_TOKEN → Update`.

## Troubleshooting

### Workflow pada na "Validate auth setup"
- `CLAUDE_CODE_OAUTH_TOKEN` nije postavljen kao secret
- Ili je postavljen ali pogrešne vrijednosti (provjeri da počinje s `sk-ant-oat01-`)

### Workflow pada na "Test Claude Code auth"
- Token istekao → generiraj novi s `claude setup-token`
- Rate limit → pričekaj ~15 min i pokreni opet

### Email ne stiže
- Provjeri Gmail App Password (16 znakova, BEZ razmaka)
- Provjeri da `SMTP_PORT=465` + `SMTP_USE_TLS=0` (ili 587 + 1)
- Pogledaj logs artifact za SMTP greške
- Provjeri spam folder

### Dashboard prazan / ne mijenja se
- Otvori Actions → zadnji run → provjeri je li "Deploy to Pages" prošao
- Pričekaj 2-3 minute da se Pages cache izbrisao
- Force refresh u browseru (Ctrl+Shift+R)

### "No scoring happening" u logovima
- Možda je `ANTHROPIC_API_KEY` secret slučajno postavljen — workflow to sprječava
- Ili je CSV skinut krivo — provjeri logs

### Repo postaje prevelik

`scores/basic/` može narasti na ~10k fajlova kroz godinu. 30MB max.
`reports/` također raste — možeš povremeno obrisati stare:
```bash
find reports -name "email_*.html" -mtime +60 -delete
git add -A && git commit -m "prune old reports" && git push
```

## Privacy napomene

- Repo je javan → **svatko tko zna URL može vidjeti dashboard i AI scoringe**
- Podaci su ionako javni (FINA ih publicira), ali agregirani + AI mišljenja su nova informacija
- AI scoring reasoning ne otkriva ništa osobno o tebi
- Tvoj watchlist/presetovi su u browser localStorage → samo tebi vidljivi
- Ako te to smeta: pretvori repo u privatni (Settings → Change visibility), ali tada dashboard moraš hostati negdje drugo (Cloudflare Pages može servirati iz privatnog GitHub repoa)

## Lokalni razvoj / testiranje

```bash
# Clone
git clone https://github.com/TVOJ-USERNAME/ponip-monitor.git
cd ponip-monitor

# Python deps
pip install pandas pyyaml requests python-dotenv

# Claude Code
npm install -g @anthropic-ai/claude-code
claude  # auth flow (samo prvi put)

# Env
cp .env.example .env
nano .env  # popuni kredencijale
chmod 600 .env

# Run
PONIP_DRY_RUN=1 python3 monitor.py

# Vidi rezultat
open dashboard.html  # macOS
xdg-open dashboard.html  # Linux
```

## Proširenja (spreman za dodati)

- **Telegram/WhatsApp notifikacija** za deep_dive ili score >8.5
- **Watchlist** u repo (watchlist.yaml s ID-ovima) + poseban alert workflow
- **PDF weekly export** u poseban `reports/pdf/` folder
- **Re-scoring** nakon isteka dražbe — evaluacija koliko je AI bio točan
- **Heatmap po županijama** sa prosječnim AI scoreom
