# MBH Bank Downdetector Monitor

Ez a projekt 30 percenként lekérdezi az [MBH Bank Downdetector oldalt](https://downdetector.hu/problema/mbh-bank/), és Telegram értesítést küld, ha a bejelentett hibák száma meghalad egy konfigurálható küszöböt (alapértelmezésben 10).

## Gyors áttekintés

- **Adatforrás**: downdetector.hu/problema/mbh-bank/ (scraping, mert nincs nyilvános API)
- **Futtatókörnyezet**: GitHub Actions cron (ingyenes)
- **Nyelv**: Python 3.11+
- **Értesítés**: Telegram bot
- **Állapottárolás**: JSON fájl a repóban (commit-back pattern)
- **Havi költség**: 0 Ft

## Projekt struktúra

```
mbh-monitor/
├── CLAUDE.md                    # Ez a fájl – projekt kontextus
├── README.md                    # Step-by-step setup útmutató
├── src/
│   ├── scraper.py               # Downdetector adatlekérés
│   ├── notifier.py              # Telegram üzenetküldés
│   ├── state.py                 # Állapot olvasás/írás
│   ├── config.py                # Konfiguráció (küszöb, URL, stb.)
│   └── main.py                  # Belépési pont – ezt hívja a GitHub Actions
├── tests/
│   ├── test_scraper.py
│   ├── test_notifier.py
│   ├── test_state.py
│   └── fixtures/                # Mentett HTML mintaadatok teszteléshez
├── .github/
│   └── workflows/
│       └── monitor.yml          # 30 percenkénti cron
├── state.json                   # Állapot (auto-commitelt)
├── requirements.txt
└── .claude/
    └── skills/
        ├── chief-of-staff/      # Koordinátor – user ezzel beszél
        ├── chief-architect/     # Rendszerterv, kódminőség
        ├── test-manager/        # Tesztelés, QA
        └── operator/            # GitHub Actions, deploy, monitoring
```

## A négy szerepkör

A projekten egy kis "csapat" dolgozik, mindegyiknek megvan a saját felelősségi köre. **A felhasználó mindig a Chief of Staff-nak ír**, aki aztán bevonja a megfelelő specialistát.

| Szerep | Skill neve | Felelősség |
|---|---|---|
| Chief of Staff | `chief-of-staff` | Koordinál, kérdéseket tisztáz, feladatokat oszt ki, folyamatot követ |
| Chief Architect | `chief-architect` | Rendszertervezés, technológiai döntések, kódreview architektúra szempontból |
| Test Manager | `test-manager` | Teszt stratégia, teszt esetek, QA, acceptance kritériumok |
| Operator | `operator` | GitHub Actions, secrets, deploy, monitoring, incidensek |

**Fontos**: A skillek akkor triggerelődnek, amikor egy user kérés a fenti szerepek valamelyikének felelősségi körébe esik. A Chief of Staff általában az első, aki bekapcsolódik egy új kérésnél.

## Fejlesztési munkafolyamat

1. A felhasználó megfogalmaz egy kérést vagy feladatot
2. Chief of Staff értelmezi, kérdéseket tesz fel, felbontja altaszkokra
3. Chief of Staff kiosztja a megfelelő szerepre (architect / test / operator)
4. A szerep elvégzi a munkát
5. Chief of Staff ellenőriz, visszajelez, összefoglalja az eredményt a felhasználónak
6. Változások commitolása értelmes üzenettel

## Kulcs alapelvek

### 1. Idempotens futás
A `main.py` minden indításnál ugyanazt csinálja: lekér, összehasonlít, értesít ha kell, állapotot frissít. Nincs rejtett állapot a memóriában.

### 2. Duplikáció-mentes riasztás
Csak akkor küld riasztást, amikor **átlépi** a küszöböt (előző érték ≤ küszöb, új > küszöb). Helyreállás külön üzenet. Ezt a `state.json` biztosítja.

### 3. Heartbeat & napi összefoglaló (catchup logika)
A `HEARTBEAT_HOURS` env var-ban megadott óráknál (Budapest TZ) küld üzenetet:
- **Korábbi órák** (pl. 9): egyszerű heartbeat (aktuális hibaszám, küszöb)
- **Utolsó óra** (pl. 19): napi összefoglaló (napi max + mikor, aktuális, küszöb, volt-e alert)
- **Napi max**: a Downdetector chart 96 adatpontjából (24h, 15 perces intervallumok) számítja, nem csak a 30 perces futások értékéből – így a futások közötti csúcsok sem vesznek el
- **Catchup**: ha a GitHub Actions cron kihagyja a konfigurált órát, a következő futás pótlólag elküldi (feltétel: `current_hour >= configured_hour` és ma még nem küldtük). Deduplikáció: óránként max 1 üzenet naponta (`state.json` `heartbeat_sent` dict).

### 4. Graceful failure
Ha a Downdetector nem elérhető / változott a formátum / Cloudflare blokkol, a script **nem buktatja el a GitHub Actions futást** – hanem hibát logol, és ha több egymás utáni futás is elbukik, értesít róla. Monitoring dashboard nélkül ez a legolcsóbb "van-e baj" jelzés.

### 5. Scraping respectful
- User-Agent reális
- 30 percnél gyakrabban SOHA nem kérdez le
- Ha 429-et kapunk, exponenciális backoff

### 6. Változásra érzékeny
A Downdetector HTML formátuma bármikor változhat. Az adatkinyerő kódnak legyen több stratégiája (elsődleges JSON parse → fallback regex → hiba), és a teszteknek valódi HTML fixture-ön kell futniuk.

## Kulcs parancsok

```bash
# Lokális futtatás (dev)
python -m src.main

# Tesztek
pytest tests/ -v

# Coverage
pytest tests/ --cov=src --cov-report=term-missing

# Lint
ruff check src/ tests/

# Manuális GitHub Actions trigger (gh cli)
gh workflow run monitor.yml
```

## Környezeti változók

| Név | Kötelező | Leírás |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | igen | @BotFather által adott token |
| `TELEGRAM_CHAT_ID` | igen | Telegram csoport ID (negatív szám, `-100...` prefixszel) vagy egyéni chat ID |
| `ALERT_THRESHOLD` | nem | Küszöbérték, default 10 |
| `DOWNDETECTOR_URL` | nem | Default: MBH Bank URL |
| `HEARTBEAT_ENABLED` | nem | Napi heartbeat, default `true` |
| `HEARTBEAT_HOURS` | nem | Heartbeat órák vesszővel (Budapest TZ), default `9,19`. Az utolsó óra napi összefoglalót küld. |

GitHub-on ezek **Secrets**-ként vannak tárolva (Settings → Secrets and variables → Actions).

## Jogi figyelmeztetés

A Downdetector Fair Use szabályzata kereskedelmi célú scraping-et nem enged. Ez a projekt **kizárólag személyes monitorozásra készült**, alacsony frekvenciával (30 percenként egy kérés). Nyilvános szolgáltatásként vagy kereskedelmi célra nem szabad üzemeltetni – ahhoz a hivatalos (fizetős) Ookla Enterprise API kell.
