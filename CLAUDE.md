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
│   ├── history.py               # Append-only CSV history logging (trend tracking)
│   ├── remediation.py           # Auto-remediation alternatív fetch stratégiák
│   └── main.py                  # Belépési pont – ezt hívja a GitHub Actions
├── tests/
│   ├── test_scraper.py
│   ├── test_notifier.py
│   ├── test_state.py
│   ├── test_history.py
│   └── fixtures/                # Mentett HTML mintaadatok teszteléshez
├── .github/
│   └── workflows/
│       └── monitor.yml          # 30 percenkénti cron
├── state.json                   # Állapot (auto-commitelt)
├── history.csv                  # Long-term trend tracking (append-only, auto-commitelt)
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

### 2. Long-term history (trend tracking)
Minden sikeres scrape után egy sor kerül a `history.csv` végére (append-only). Oszlopok: `timestamp,value,threshold,alert_active`. A timestamp Europe/Budapest TZ-vel ISO 8601 formátumban. A threshold mentése biztosítja, hogy küszöbváltozás után is megmaradjon a régi adatpontok kontextusa. A fájlt a workflow a `state.json`-nal együtt commitolja. Hibás scrape-nél NEM íródik sor. Ha a CSV írás elbukik, csak WARNING logot generál — a core funkció nem akad meg. Méretkorlát: 5 MB felett WARNING a logban (évi ~17 500 sor ≈ 700 KB–1 MB). Modul: `src/history.py`.

### 3. Duplikáció-mentes riasztás
Csak akkor küld riasztást, amikor **eléri vagy átlépi** a küszöböt (előző érték < küszöb, új >= küszöb). Helyreállás: amikor visszaesik a küszöb alá (új < küszöb). Ezt a `state.json` biztosítja.

**Visszamenőleges spike detektálás**: Ha a chart adatpontok között (az utolsó `last_checked` óta) bármelyik átlépte a küszöböt, de a jelenlegi érték már alatta van, a rendszer `send_retroactive_alert()` üzenetet küld. Ez a `_detect_retroactive_spike()` függvény feladata. Az `alert_active` nem változik (nem kell recovery ciklus), és a napi összefoglalóban `*` jellel jelölt időpont jelzi a visszamenőleges riasztást.

### 4. Heartbeat & napi összefoglaló (catchup logika)
A `HEARTBEAT_HOURS` env var-ban megadott óráknál (Budapest TZ) küld üzenetet:
- **Korábbi órák** (pl. 9): egyszerű heartbeat (aktuális hibaszám, küszöb)
- **Utolsó óra** (pl. 19): napi összefoglaló (napi max + mikor, aktuális, küszöb, volt-e alert)
- **Napi max**: a Downdetector chart 96 adatpontjából (24h, 15 perces intervallumok) számítja, nem csak a 30 perces futások értékéből – így a futások közötti csúcsok sem vesznek el
- **Catchup**: ha a GitHub Actions cron kihagyja a konfigurált órát, a következő futás pótlólag elküldi (feltétel: `current_hour >= configured_hour` és ma még nem küldtük). Deduplikáció: óránként max 1 üzenet naponta (`state.json` `heartbeat_sent` dict).

### 5. Redundáns ütemezés (belső + külső cron)
A GitHub Actions cron megbízhatatlan (±5-10 perces késés, néha 1-2 órás kimaradás). Ezért **két triggerrel** dolgozunk:
- **cron-job.org** (elsődleges): `:00` és `:30`-kor (`*/30`), workflow_dispatch API híváson keresztül
- **GitHub Actions cron** (backup): `:15` és `:45`-kor (`15,45`), offset-elve az ütközés elkerüléséhez
- **Concurrency group** (`monitor`, `cancel-in-progress: false`): ha mégis átfedés lenne, a második futás sorba áll
- A script idempotens, a heartbeat deduplikált (`state.json`) → dupla futás nem okoz dupla értesítést
- A cron-job.org egy **fine-grained GitHub PAT**-on keresztül hívja a workflow dispatch API-t (csak Actions write scope, csak erre a repóra). A token lejárata: **2026-07-25** — lejárat előtt rotálni kell.

### 6. Graceful failure + azonnali remediation
Ha a solver nem tudja lekérdezni az adatot, a script **azonnal** alternatív stratégiákkal próbálkozik (auto-remediation). Ha valamelyik sikerül, normál feldolgozás folytatódik. Ha egyik sem → csendben vár, és **~30 perc + 2 hiba** után küld értesítést a részletes diagnosztikával. Amikor a lekérdezés helyreáll, **fetch recovery értesítést** küld.

### 7. Cloudflare-reziliens scraping (solver-agnosztikus, többrétegű védelem)
A `fetch_html()` solver-agnosztikus: FlareSolverr-rel és ByParr-ral egyaránt működik. A `_solver_fetch()` mindkét timeout formátumot küldi (`maxTimeout` ms-ben + `max_timeout` másodpercben), így a solver-csere deploy nélkül, GitHub variable-ből megoldható.

- **Solver**: a `SOLVER_IMAGE` GitHub variable határozza meg (FlareSolverr vagy ByParr). A kód session management nélkül működik – a solver maga kezeli a browser lifecycle-t.
- **Timeout**: 60s a Cloudflare challenge megoldására (`FLARESOLVERR_MAX_TIMEOUT`)
- **Retry**: 5 kísérlet exponenciális backoff-fal (2s, 4s, 8s, 16s) – `MAX_RETRIES` env var-ral állítható
- **Jitter**: 0-90s véletlenszerű várakozás minden futás elején, hogy a lekérdezés ne mindig ugyanabban a pillanatban induljon
- **Health check**: a retry loop előtt gyors GET a solver-hez – ha nem elérhető, azonnali `FetchError` a 5×60s timeout helyett
- **Proxy support**: opcionális `FLARESOLVERR_PROXY` env var, ha a GitHub Actions IP blokkolva van
- **ZenRows**: a remediation modul kezeli (nem a `fetch_html()`), 4 stratégiával: no-premium (1 kredit), premium HU (10-25 kredit), alt-country (DE/AT/US), direkt HTTP. Kredit figyelmeztetés ha a ZenRows egyenleg `ZENROWS_CREDIT_WARNING_THRESHOLD` alá esik.
- **Napi SLA szint**: napi (`daily_total_fetches`/`daily_failed_fetches`) és kumulatív (`total_fetches`/`failed_fetches`) számlálók, napi SLA % a napi összefoglalóban

### 8. Scraping respectful
- User-Agent reális (FlareSolverr Chrome)
- 30 percnél gyakrabban SOHA nem kérdez le
- Ha 429-et kapunk, exponenciális backoff

### 9. Strukturált logolás (post-mortem rekonstrukció)
Minden futás teljes log-trace-t hagy a GitHub Actions logban, amiből visszaállítható egy napi riport. Kulcs log sorok:
- **Run started**: küszöb, heartbeat órák, state összefoglaló (value, failures, alert_active)
- **Chart max today**: napi chart maximum + korábbi daily_max összehasonlítás
- **Daily max updated**: mikor/miért változik a napi csúcs
- **No action needed / ALERT / RECOVERY**: döntés indoklása (value, threshold, alert_active)
- **Telegram eredmény**: minden send hívás után `-> ok=True/False`
- **Üzenettípus**: a `_send_telegram` `msg_type` keyword-only paraméterrel logol (`alert`, `recovery`, `retroactive_alert`, `heartbeat`, `daily_summary`, `parse_degradation`, `fetch_failure`, `fetch_recovery`)
- **Heartbeat catchup**: tényleges küldési idő (`actual: HH:MM`) és típus (heartbeat/summary)
- **Recent points**: RSC parse után az utolsó 5 adatpont (`value@HH:MM` formátum)
- **State loaded**: betöltéskori állapot összefoglaló
- **Jitter delay**: `Jitter delay: X.Xs` – véletlenszerű indulási késleltetés
- **Health check**: `Solver health check OK` – solver elérhetőség
- **Fetch recovery**: `Fetch recovery notification -> ok=True/False` – helyreállás értesítés eredménye
- **Run complete**: sikeres út (`value, daily_max, action, strategy`) és hiba út (`failures, error_alert_sent`)

Post-mortem parancs: `gh run view <ID> --log | grep INFO` → teljes napi kép.

### 10. Változásra érzékeny (parse stratégia lánc + degradáció-érzékelés)
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

**Skeleton page detektálás és automatikus re-fetch**: Ha a `parse_reports()` a `heading` stratégiára esik vissza (ami csak "no current problems" → 0-t tud jelezni, chart adat nélkül), a `run()` automatikusan meghívja az `attempt_remediation()`-t alternatív forrásból (ZenRows, direkt HTTP) a valódi chart adatokért. Ha a remediation jobb stratégiát ad (nem heading) → azt használja (`via_remediation` jelöléssel). Ha a remediation is heading-et ad → az eredeti eredménnyel folytatja (+ degradáció alert). A skeleton HTML debug-ként mentésre kerül.

### 11. Auto-remediation (azonnali javítási kísérlet)
Ha a solver elbukik, a rendszer **azonnal** alternatív stratégiákkal próbálkozik (nem vár 3-4 hibáig):

```
except ág:
  ├─ Minden hiba: AZONNALI REMEDIATION
  │   ├─ Hiba kategorizálás (CF block / network / rate limit / stb.)
  │   ├─ Stratégiák (progresszív cooldown-al):
  │   │   1. ZenRows premium proxy nélkül (1 kredit)
  │   │   2. ZenRows premium HU proxy-val (10-25 kredit)
  │   │   3. ZenRows más ország proxy-val (DE/AT/US)
  │   │   4. Direkt HTTP kérés (solver nélkül, 0 kredit)
  │   ├─ Ha sikerül → normál feldolgozás + siker riport + kredit figyelmeztetés
  │   └─ Ha mind bukik → csend, amíg:
  │       elapsed >= 30 perc AND hibák >= 2 → diagnosztikai riport
  └─ Eszkaláció: 6., 12., 24. hibánál újra küld riportot
```

- **Modul**: `src/remediation.py` – `ErrorCategory` enum, `classify_error()`, stratégia függvények, `attempt_remediation()` orchestrator
- **ZenRows optimalizáció**: a `fetch_html()` csak a solver-t használja, a ZenRows kizárólag remediation stratégia → nincs dupla ZenRows hívás
- **Hiba kategóriák**: `SOLVER_UNREACHABLE`, `CLOUDFLARE_BLOCK`, `RATE_LIMITED`, `ZENROWS_CREDITS`, `TARGET_DOWN`, `NETWORK_ERROR`, `PARSE_FAILURE`, `UNKNOWN`
- **Progresszív cooldown**: bukott stratégia `min(30 * fail_count, REMEDIATION_COOLDOWN_MINUTES)` percig nem próbálkozik újra (state-ben: `remediation_attempts`)
- **Időalapú értesítés**: `NOTIFICATION_DELAY_MINUTES` (30) perc + `NOTIFICATION_MIN_FAILURES` (2) hiba után küld részletes diagnosztikát, nem azonnal
- **Kredit védelem**: `ZENROWS_CREDIT_WARNING_THRESHOLD` alatti egyenlegnél `send_zenrows_credit_warning()` Telegram üzenet
- **Spam-védelem**: `error_alert_sent` + `remediation_report_sent` state flag-ek, eszkaláció csak 6/12/24 hibánál
- **Logolás**: minden stratégia-próbálkozás strukturált logot hagy (`category`, `strategy`, `duration_s`, `result`, `credits`)
- **Értesítés**: `send_remediation_report()` két variáns (sikeres/sikertelen), `msg_type="remediation_report"`

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
| `MAX_RETRIES` | nem | Fetch retry kísérletek száma, default `5`. |
| `FLARESOLVERR_MAX_TIMEOUT` | nem | Solver challenge timeout ms-ben, default `60000`. Növeld ha a Cloudflare challenge timeout-ol. |
| `FLARESOLVERR_PROXY` | nem | Proxy URL a FlareSolverr-nek (pl. `http://user:pass@proxy:8080`). Ha a GitHub Actions IP blokkolva van. |
| `JITTER_MAX_SECONDS` | nem | Max indulási késleltetés másodpercben, default `90`. Cloudflare timing-detection ellen. |
| `ZENROWS_API_KEY` | nem | ZenRows API key a fizetős fallback-hez. Ha üres, nincs ZenRows fallback. |
| `ZENROWS_PROXY_COUNTRY` | nem | ZenRows proxy ország kód, default `HU`. |
| `SOLVER_IMAGE` | nem | Docker image a challenge solver-hez (GitHub Actions variable). Default: `ghcr.io/flaresolverr/flaresolverr:latest`. ByParr csere: `ghcr.io/thephaseless/byparr:latest`. |
| `REMEDIATION_TRIGGER_THRESHOLD` | nem | Hány egymás utáni hiba után indul az auto-remediation, default `1` (azonnali). |
| `REMEDIATION_COOLDOWN_MINUTES` | nem | Progresszív cooldown felső korlát (percben), default `120`. Tényleges cooldown: `min(30 * fail_count, max)`. |
| `NOTIFICATION_DELAY_MINUTES` | nem | Mennyi idő elteltével küld értesítést sikertelen remediáció esetén, default `30`. |
| `NOTIFICATION_MIN_FAILURES` | nem | Minimum hány egymás utáni hiba kell az értesítéshez (az időkorlát mellett), default `2`. |
| `ZENROWS_CREDIT_WARNING_THRESHOLD` | nem | ZenRows kredit figyelmeztetési küszöb, default `50`. Ha a maradék kredit ez alá esik, Telegram értesítés. |

GitHub-on ezek **Secrets**-ként vannak tárolva (Settings → Secrets and variables → Actions).

## Jogi figyelmeztetés

A Downdetector Fair Use szabályzata kereskedelmi célú scraping-et nem enged. Ez a projekt **kizárólag személyes monitorozásra készült**, alacsony frekvenciával (30 percenként egy kérés). Nyilvános szolgáltatásként vagy kereskedelmi célra nem szabad üzemeltetni – ahhoz a hivatalos (fizetős) Ookla Enterprise API kell.
