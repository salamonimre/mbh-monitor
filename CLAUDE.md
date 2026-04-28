# MBH Bank Downdetector Monitor

Ez a projekt 30 percenként lekérdezi az [MBH Bank Downdetector oldalt](https://downdetector.hu/problema/mbh-bank/), és Telegram értesítést küld, ha a bejelentett hibák száma eléri vagy meghaladja a konfigurálható küszöböt (alapértelmezésben 10).

## Gyors áttekintés

- **Adatforrás**: downdetector.hu/problema/mbh-bank/ (scraping, mert nincs nyilvános API)
- **Futtatókörnyezet**: GitHub Actions cron + cron-job.org külső trigger (ingyenes, redundáns)
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
Csak akkor küld riasztást, amikor **eléri vagy átlépi** a küszöböt (előző érték < küszöb, új >= küszöb). Helyreállás: amikor visszaesik a küszöb alá (új < küszöb). Ezt a `state.json` biztosítja.

### 3. Heartbeat & napi összefoglaló (catchup logika)
A `HEARTBEAT_HOURS` env var-ban megadott óráknál (Budapest TZ) küld üzenetet:
- **Korábbi órák** (pl. 9): egyszerű heartbeat (aktuális hibaszám, küszöb)
- **Utolsó óra** (pl. 19): napi összefoglaló (napi max + mikor, aktuális, küszöb, volt-e alert)
- **Napi max**: a Downdetector chart 96 adatpontjából (24h, 15 perces intervallumok) számítja, nem csak a 30 perces futások értékéből – így a futások közötti csúcsok sem vesznek el
- **Catchup**: ha a GitHub Actions cron kihagyja a konfigurált órát, a következő futás pótlólag elküldi (feltétel: `current_hour >= configured_hour` és ma még nem küldtük). Deduplikáció: óránként max 1 üzenet naponta (`state.json` `heartbeat_sent` dict).

### 4. Redundáns ütemezés (belső + külső cron)
A GitHub Actions cron megbízhatatlan (±5-10 perces késés, néha 1-2 órás kimaradás). Ezért **két triggerrel** dolgozunk:
- **cron-job.org** (elsődleges): `:00` és `:30`-kor (`*/30`), workflow_dispatch API híváson keresztül
- **GitHub Actions cron** (backup): `:15` és `:45`-kor (`15,45`), offset-elve az ütközés elkerüléséhez
- **Concurrency group** (`monitor`, `cancel-in-progress: false`): ha mégis átfedés lenne, a második futás sorba áll
- A script idempotens, a heartbeat deduplikált (`state.json`) → dupla futás nem okoz dupla értesítést
- A cron-job.org egy **fine-grained GitHub PAT**-on keresztül hívja a workflow dispatch API-t (csak Actions write scope, csak erre a repóra). A token lejárata: **2026-07-25** — lejárat előtt rotálni kell.

### 5. Graceful failure
Ha a Downdetector nem elérhető / változott a formátum / Cloudflare blokkol, a script **nem buktatja el a GitHub Actions futást** – hanem hibát logol, és ha több egymás utáni futás is elbukik, értesít róla. Monitoring dashboard nélkül ez a legolcsóbb "van-e baj" jelzés.

### 6. Scraping respectful
- User-Agent reális
- 30 percnél gyakrabban SOHA nem kérdez le
- Ha 429-et kapunk, exponenciális backoff

### 7. Strukturált logolás (post-mortem rekonstrukció)
Minden futás teljes log-trace-t hagy a GitHub Actions logban, amiből visszaállítható egy napi riport. Kulcs log sorok:
- **Run started**: küszöb, heartbeat órák, state összefoglaló (value, failures, alert_active)
- **Chart max today**: napi chart maximum + korábbi daily_max összehasonlítás
- **Daily max updated**: mikor/miért változik a napi csúcs
- **No action needed / ALERT / RECOVERY**: döntés indoklása (value, threshold, alert_active)
- **Telegram eredmény**: minden send hívás után `-> ok=True/False`
- **Üzenettípus**: a `_send_telegram` `msg_type` keyword-only paraméterrel logol (`alert`, `recovery`, `heartbeat`, `daily_summary`, `parse_degradation`, `fetch_failure`)
- **Heartbeat catchup**: tényleges küldési idő (`actual: HH:MM`) és típus (heartbeat/summary)
- **Recent points**: RSC parse után az utolsó 5 adatpont (`value@HH:MM` formátum)
- **State loaded**: betöltéskori állapot összefoglaló
- **Run complete**: sikeres út (`value, daily_max, action, strategy`) és hiba út (`failures, error_alert_sent`)

Post-mortem parancs: `gh run view <ID> --log | grep INFO` → teljes napi kép.

### 8. Változásra érzékeny (parse stratégia lánc + degradáció-érzékelés)
A Downdetector HTML formátuma bármikor változhat. A `parse_reports()` stratégia lánca:
1. **RSC** (`rsc`): Next.js `__next_f.push()` payloadokból `dataPoints` tömb — legpontosabb, 96 adatpont
2. **JSON anywhere** (`json_anywhere`): bármilyen JSON tömb a HTML-ben ami `timestampUtc`/`reportsValue` mezőket tartalmaz — ha az RSC delivery megváltozik de az adatstruktúra nem
3. **aria-label** (`aria_label`): chart aria-label attribútumból 24h csúcs — közelítő érték
4. **heading** (`heading`): "no current problems" felirat → 0 reports
5. **ParseError**: ha semmi sem talál

Ha az RSC stratégia nem működik és fallback-re kerül a sor, a rendszer:
- Telegram értesítést küld (`send_parse_degradation_alert`) a használt stratégiáról
- Debug HTML-t ment (`/tmp/debug_response.html`) és feltölti GitHub Actions artifactként
- A `degraded_parse_alert_sent` state flag megakadályozza a spam-et (RSC visszaállásakor resetelődik)

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
| `PAT_EXPIRY_DATE` | nem | cron-job.org PAT lejárati dátum (`YYYY-MM-DD`), default `2026-07-25`. 30 napon belül figyelmeztet a napi összefoglalóban. |
| `CURL_CFFI_IMPERSONATE` | nem | curl_cffi TLS fingerprint, default `chrome136`. Változtasd ha Cloudflare blokkolja. |

GitHub-on ezek **Secrets**-ként vannak tárolva (Settings → Secrets and variables → Actions).

## Jogi figyelmeztetés

A Downdetector Fair Use szabályzata kereskedelmi célú scraping-et nem enged. Ez a projekt **kizárólag személyes monitorozásra készült**, alacsony frekvenciával (30 percenként egy kérés). Nyilvános szolgáltatásként vagy kereskedelmi célra nem szabad üzemeltetni – ahhoz a hivatalos (fizetős) Ookla Enterprise API kell.
