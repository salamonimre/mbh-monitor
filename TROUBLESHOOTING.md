# Troubleshooting Guide – MBH Downdetector Monitor

Ez a dokumentum leírja a scraper architektúráját, a tipikus hibákat, és hogyan lehet debug-olni a rendszert. Ha fél év múlva térsz vissza, innen indulj el.

---

## 1. Architektúra áttekintés

```
Triggerek: cron-job.org (:00/:30) + GitHub Actions cron (:15/:45)
    │
    ▼
┌─────────────────────────────────────┐
│  FlareSolverr (Docker service)      │
│  ghcr.io/flaresolverr/flaresolverr │
│  Port 8191                          │
│  Feladat: Cloudflare challenge      │
│  megoldása headless Chromium-mal    │
│  Eredmény: cf_clearance cookie +   │
│  User-Agent string (Chrome verzió)  │
└──────────────┬──────────────────────┘
               │ cookies + UA
               ▼
┌─────────────────────────────────────┐
│  curl_cffi (Chrome TLS fingerprint) │
│  impersonate=CURL_CFFI_IMPERSONATE               │
│  Feladat: raw SSR HTML letöltése    │
│  a FlareSolverr cookie-kkal         │
│  Miért: Cloudflare a cookie-t a     │
│  TLS fingerprinthez köti, ezért     │
│  nem elég a sima requests lib       │
│  Eredmény: ~282 KB raw HTML         │
│  403 → részletes log (cf-ray, body) │
└──────────────┬──────────────────────┘
               │ raw HTML
               ▼
┌─────────────────────────────────────┐
│  Parse stratégia lánc               │
│  1. RSC: __next_f.push() → 96 pont │
│  2. JSON anywhere: bármilyen JSON   │
│     tömb timestampUtc/reportsValue  │
│  3. aria-label: 24h csúcs           │
│  4. heading: "no current problems"  │
│  5. ParseError → debug HTML mentés  │
│  Nem-RSC → degradáció Telegram alert│
└──────────────┬──────────────────────┘
               │ ParseResult (points + strategy)
               ▼
┌─────────────────────────────────────┐
│  Állapotgép (main.py)              │
│  decide_action() + heartbeat        │
│  degradáció-érzékelés               │
│  PAT lejárat figyelmeztetés         │
│  state.json: commit-back pattern    │
│  Telegram értesítések               │
└─────────────────────────────────────┘
```

### Miért kell FlareSolverr?

A Downdetector Cloudflare mögött fut. Normál HTTP kérések (requests, curl) 403-at kapnak. A FlareSolverr egy headless Chromium-ot futtat, ami megoldja a Cloudflare JS challenge-et, és visszaadja a bypass cookie-kat.

### Miért kell curl_cffi?

Cloudflare a `cf_clearance` cookie-t a böngésző TLS fingerprintjéhez (JA3/JA4) köti. Ha a cookie-t egy eltérő TLS fingerprintű kliensből használjuk (pl. Python `requests`), 403-at kapunk. A `curl_cffi` a Chrome TLS fingerprintjét utánozza (konfigurálható: `CURL_CFFI_IMPERSONATE` env var, default `chrome136`), így a cookie érvényes marad.

### Miért nem elég a FlareSolverr válasz HTML-je?

A FlareSolverr a renderelt DOM-ot adja vissza (JS futtatás után). A Next.js RSC stream (`__next_f.push` script tagek) erre már nincsenek benne, mert a React hydration felhasználta őket. A raw SSR HTML-ben viszont még benne vannak a pontos `reportsValue` adatok.

### Miért raw_decode és nem json.loads?

A `chartData` objektumban a `dataPoints` tömb után további mezők vannak. A regex-alapú extrakció túl sokat vagy túl keveset fog. A `json.JSONDecoder.raw_decode()` pontosan a `[...]` tömböt parse-olja és megáll, függetlenül attól, mi jön utána.

---

## 2. Komponensek és felelősségük

| Komponens | Fájl | Feladat |
|---|---|---|
| Config | `src/config.py` | Env var-ok beolvasása, default értékek |
| Scraper | `src/scraper.py` | FlareSolverr hívás, curl_cffi fetch, parse stratégia lánc (RSC → JSON anywhere → aria-label → heading) |
| State | `src/state.py` | JSON state load/save, dataclass serialize |
| Notifier | `src/notifier.py` | Telegram üzenetek (alert, recovery, heartbeat, daily summary, parse degradáció, fetch error) |
| Main | `src/main.py` | Orchestrator: fetch → parse → degradáció check → decide → notify → heartbeat/summary → save |
| Workflow | `.github/workflows/monitor.yml` | Cron, FlareSolverr service, secrets, state commit-back |

---

## 3. Tipikus hibák és tüneteik

### "Parse degradáció" alert Telegramon (vagy fallback WARNING a logban)

**Tünet**: Telegram degradáció alert, vagy a log `json_anywhere`/`aria-label` fallback-et jelez `RSC strategy` helyett.

**Ok**: A raw HTML nem tartalmazza az RSC `__next_f.push` adatokat, vagy a parser regex nem találja. A rendszer automatikusan fallback stratégiára vált és Telegram értesítést küld.

**Teendő**:
1. Töltsd le a debug HTML-t a GitHub Actions artifactokból (`debug-html-<run_id>`)
2. Ellenőrizd a HTML méretét – ha ~6 KB, Cloudflare blokkolt (nem raw HTML, hanem challenge page)
3. Ha ~280 KB de nincs RSC: a Downdetector változtatta a formátumot. Keresd a `reportsValue` környezetét az új HTML-ben
4. Ha `json_anywhere` stratégia működik, az adatok még pontosak – van időd javítani az RSC parsert
5. Ha `aria_label` fallback: csak 24h csúcsérték, nem pontos – sürgős javítás kell

**Megjegyzés**: A `degraded_parse_alert_sent` state flag megakadályozza a spam-et. Ha az RSC parser újra működik, a flag automatikusan resetelődik.

### "HTTP 403 despite cookies"

**Tünet**: FlareSolverr sikeresen ad cookie-kat, de a curl_cffi fetch 403-at kap. A log részletes diagnosztikát tartalmaz: `cf-ray`, `body snippet`, `impersonate` érték, és Cloudflare error code (pl. 1020).

**Ok**:
- A cookie lejárt (FlareSolverr lassú volt, és a cookie érvényessége rövid)
- TLS fingerprint mismatch (curl_cffi verzió nem egyezik a FlareSolverr Chrome verziójával)
- Cloudflare frissített és az aktuális impersonation profil elavult
- Cloudflare 1020 error: access denied, valószínűleg TLS mismatch

**Teendő**:
1. Nézd a logban a Chrome verziót (`Got N cookies from FlareSolverr (Chrome 136)`) — ha a FlareSolverr Chrome verziója és a `CURL_CFFI_IMPERSONATE` értéke nagyon eltér, az a baj
2. Próbáld módosítani a `CURL_CFFI_IMPERSONATE` env var-t (pl. `chrome131`, `chrome136`)
3. Frissítsd a curl_cffi-t: `pip install --upgrade curl_cffi`
4. Frissítsd a FlareSolverr image-et: `ghcr.io/flaresolverr/flaresolverr:latest`
5. Ellenőrizd a FlareSolverr logot – ha "Challenge detected!" áll benne, a FlareSolverr sem tudja megoldani

### "FlareSolverr timeout" / ConnectionError

**Tünet**: A FlareSolverr nem válaszol, vagy timeout-ol.

**Teendő**:
1. GitHub Actions-ben nézd a "Wait for FlareSolverr" lépés logját
2. Ha a FlareSolverr container nem indult el: Docker image probléma, próbáld fixálni a verziót (pl. `ghcr.io/flaresolverr/flaresolverr:v3.4.6`)
3. Lokálisan: `docker run -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest` és nézd a logot

### "Fetch failed (N consecutive)"

**Tünet**: Ismétlődő fetch hibák, végül error alert Telegramon.

**Ok**: Bármi a fentiekből. 3 egymás utáni hiba (~1.5 óra) után kapsz értesítést.

**Teendő**: Nézd meg a GitHub Actions logot, azonosítsd melyik lépés bukik (FlareSolverr, curl_cffi, vagy parser).

### "Forbidden: bot is not a member of the group"

**Tünet**: A Telegram API `403 Forbidden` hibát ad `bot is not a member of the group` szöveggel.

**Ok**: A bot eltávolították a Telegram csoportból, vagy a csoport ID megváltozott (pl. csoport → supergroupra konvertálás után).

**Teendő**:
1. Ellenőrizd, hogy a bot tagja-e a csoportnak (Telegram → csoport → Members)
2. Ha eltávolították, add vissza a botot a csoportba
3. Ha a csoport supergroupra konvertálódott, az ID megváltozott — futtasd a `getUpdates`-t, keresd meg az új chat ID-t, és frissítsd a GitHub Secrets-ben:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   gh secret set TELEGRAM_CHAT_ID --body "<ÚJ_CHAT_ID>"
   ```

**Megjegyzés**: A `TELEGRAM_CHAT_ID` egy Telegram csoport ID (negatív szám, tipikusan `-100`-zal kezdődik), nem egyéni chat ID. Ha bármikor tokent rotálsz (biztonsági okból új tokent generálsz @BotFather-nél), a `TELEGRAM_BOT_TOKEN` secretet is frissíteni kell.

### "Csak 0 értéket kapok mindig"

**Tünet**: A log `RSC strategy` sikert jelez, de az érték mindig 0.

**Ok**: Valóban nincs bejelentés, VAGY a parser rossz adatpontot olvas.

**Teendő**:
1. Nézd meg a Downdetector oldalt böngészőben – tényleg 0?
2. Ha nem: mentsd el a HTML-t (view-source!), és hasonlítsd össze a fixture-ökkel

---

## 4. Debug parancsok

### GitHub Actions logok megtekintése

```bash
# Legutóbbi futás logja
gh run list --repo salamonimre/mbh-monitor -L 5
gh run view <RUN_ID> --repo salamonimre/mbh-monitor --log

# Csak a lényeg
gh run view <RUN_ID> --repo salamonimre/mbh-monitor --log | grep -E "(INFO|WARNING|ERROR)"
```

### State megtekintése

```bash
cat state.json
# Tartalmaz: last_value, last_checked, alert_active, consecutive_fetch_failures,
#            error_alert_sent, heartbeat_sent, daily_max_*, degraded_parse_alert_sent
```

### Manuális workflow trigger

```bash
gh workflow run monitor.yml --repo salamonimre/mbh-monitor
```

---

## 5. Lokális futtatás lépésenként

### Előfeltétel

```bash
cd mbh-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1. FlareSolverr indítása

```bash
docker run --rm -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
```

Várd meg a "Serving on http://0.0.0.0:8191" sort.

### 2. Scraper tesztelése Python shell-ben

```python
from src.scraper import fetch_html, parse_reports

html = fetch_html("https://downdetector.hu/problema/mbh-bank/")
print(f"HTML méret: {len(html)} byte")

result = parse_reports(html)
print(f"Stratégia: {result.strategy}")  # "rsc" ha minden OK
print(f"Adatpontok: {len(result.points)}")
print(f"Utolsó érték: {result.points[-1].value} at {result.points[-1].timestamp}")
```

### 3. Teljes futás

```bash
export TELEGRAM_BOT_TOKEN="<token>"
export TELEGRAM_CHAT_ID="<chat_id>"
export ALERT_THRESHOLD="1"          # alacsony, hogy triggereljen
export FLARESOLVERR_URL="http://localhost:8191/v1"

python -m src.main
```

### 4. Tesztek

```bash
pytest tests/ -v
pytest tests/ --cov=src --cov-report=term-missing
```

---

## 6. Külső függőségek és verzióik

| Függőség | Verzió | Szerepe |
|---|---|---|
| FlareSolverr | `latest` (jelenleg v3.4.6) | Cloudflare bypass, headless Chromium |
| curl_cffi | `>=0.7,<1` (jelenleg 0.13.0) | Chrome TLS fingerprint impersonation |
| requests | `>=2.31,<3` | FlareSolverr API hívás, Telegram API |
| Python | 3.11 (GitHub Actions) | Runtime |
| Chromium | FlareSolverr belseje (142) | FlareSolverr böngészője |

---

## 7. Ismert törékenységi pontok

### Cloudflare frissítések (4-8 hetente)

Cloudflare rendszeresen frissíti a bot-detection logikáját. Amikor ez történik:
- A FlareSolverr nem tudja megoldani a challenge-et ("Challenge detected!" a logban)
- Megoldás: FlareSolverr image frissítése (`docker pull ghcr.io/flaresolverr/flaresolverr:latest`)
- A FlareSolverr fejlesztők általában 1-2 héten belül kiadnak patch-et

### TLS fingerprint eltérés

Ha a curl_cffi és a FlareSolverr más Chrome verziót utánoz, Cloudflare eldobja a cookie-t. Ez a curl_cffi frissítésekor fordulhat elő.
- Tünet: 403 a curl_cffi fetch-nél, pedig FlareSolverr sikeresen ad cookie-t. A log mutatja a Chrome verziót és az impersonate értéket.
- Megoldás: `CURL_CFFI_IMPERSONATE` GitHub Actions változót állítsd a FlareSolverr Chrome verziójához közelire (pl. `chrome131`, `chrome136`). Nem kell kód módosítás.

### Downdetector frontend változások

A Downdetector Next.js alkalmazás, és a RSC formátum változhat deploy-onként.
- Tünet: Telegram degradáció alert + parse fallback a logban
- A rendszer automatikusan:
  1. Megpróbálja a `json_anywhere` stratégiát (ha az adatstruktúra változatlan)
  2. Debug HTML-t ment és feltölti GitHub Actions artifactként
  3. Telegram értesítést küld a degradációról
- Teendőd: töltsd le a debug HTML-t, keresd meg az új formátumot, frissítsd a parsert
- Ha `json_anywhere` működik: az adatok pontosak, van időd javítani
- Várható gyakoriság: 2-6 havonta

### GitHub Actions cron pontossága

A GitHub cron ±5-10 perces késéssel fut, és néha 1-2 órát is kihagyhat. Ezért **külső cron trigger** is van (cron-job.org), offset-elve:
- cron-job.org: `:00` és `:30` (elsődleges, megbízható)
- GitHub Actions cron: `:15` és `:45` (backup)
- Eredmény: ~15 percenként van legalább egy futás, ütközés nélkül

### cron-job.org trigger leállása

**Tünet**: A GitHub Actions-ben csak `:15`/`:45`-ös futások vannak, `:00`/`:30` hiányzik.

**Lehetséges okok**:
- A GitHub fine-grained PAT lejárt (**2026-07-25**) → cron-job.org 401-et kap
- cron-job.org túl sok hibát észlelt és letiltotta a job-ot
- cron-job.org szolgáltatás leállás

**Teendő**:
1. Ellenőrizd a cron-job.org dashboardon a job HISTORY-ját – 200-at (siker) vagy 401/403-at kap-e?
2. Ha 401: a PAT lejárt → GitHub Settings → Fine-grained tokens → `mbh-monitor-cron-trigger` → regeneráld, és frissítsd a cron-job.org Authorization headerben
3. Ha a job disabled: engedélyezd újra a cron-job.org dashboardon
4. A GitHub Actions belső cron ez alatt is fut backup-ként, szóval a monitor nem áll le teljesen

### GitHub Actions inaktivitás

60 napig inaktív repók cron-ja automatikusan kikapcsol.
- Megoldás: alkalmanként commitolj (a state.json auto-commit ezt megoldja)
- A cron-job.org trigger ezen felül is életben tartja a repót (workflow_dispatch futások)

---

## 8. Visszatérés fél év után – gyors útmutató

1. **Olvasd el ezt a fájlt** – most itt tartasz
2. **Nézd meg a GitHub Actions**: Actions fül → van-e zöld pipa az utolsó futásoknál?
3. **Ha nem fut**: 60 napos inaktivitás? → Actions → Enable workflow
4. **Ha fut de piros**: Nézd a logot → melyik lépés bukik?
   - FlareSolverr nem indul → frissítsd az image-et
   - 403 → frissítsd curl_cffi-t és FlareSolverr-t
   - Parser hiba → Downdetector formátum változott, kézzel kell frissíteni
5. **State reset** (ha kell): Szerkeszd a `state.json`-t kézzel és commitold:
   ```json
   {
     "last_value": 0,
     "last_checked": null,
     "alert_active": false,
     "alert_started_at": null,
     "consecutive_fetch_failures": 0,
     "error_alert_sent": false,
     "heartbeat_sent": {},
     "daily_max_value": 0,
     "daily_max_time": null,
     "daily_max_date": null,
     "daily_alert_times": [],
     "degraded_parse_alert_sent": false
   }
   ```
6. **Lokális teszt**: Kövesd az 5. pont lépéseit feljebb
7. **Kulcs fájlok**: `src/scraper.py` (fetch + parse), `src/main.py` (logika), `.github/workflows/monitor.yml` (cron)
