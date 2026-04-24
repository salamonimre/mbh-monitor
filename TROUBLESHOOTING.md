# Troubleshooting Guide – MBH Downdetector Monitor

Ez a dokumentum leírja a scraper architektúráját, a tipikus hibákat, és hogyan lehet debug-olni a rendszert. Ha fél év múlva térsz vissza, innen indulj el.

---

## 1. Architektúra áttekintés

```
GitHub Actions cron (*/30 * * * *)
    │
    ▼
┌─────────────────────────────────────┐
│  FlareSolverr (Docker service)      │
│  ghcr.io/flaresolverr/flaresolverr │
│  Port 8191                          │
│  Feladat: Cloudflare challenge      │
│  megoldása headless Chromium-mal    │
│  Eredmény: cf_clearance cookie +   │
│  User-Agent string                  │
└──────────────┬──────────────────────┘
               │ cookies + UA
               ▼
┌─────────────────────────────────────┐
│  curl_cffi (Chrome TLS fingerprint) │
│  impersonate="chrome"               │
│  Feladat: raw SSR HTML letöltése    │
│  a FlareSolverr cookie-kkal         │
│  Miért: Cloudflare a cookie-t a     │
│  TLS fingerprinthez köti, ezért     │
│  nem elég a sima requests lib       │
│  Eredmény: ~282 KB raw HTML         │
└──────────────┬──────────────────────┘
               │ raw HTML
               ▼
┌─────────────────────────────────────┐
│  RSC Parser                         │
│  Keresés: self.__next_f.push()      │
│  payloadokban "dataPoints" tömb     │
│  json.JSONDecoder.raw_decode()      │
│  Eredmény: 96 adatpont             │
│  (15 percenkénti bontás, 24 óra)   │
│  Fallback: aria-label → heading     │
└──────────────┬──────────────────────┘
               │ current_value (utolsó adatpont)
               ▼
┌─────────────────────────────────────┐
│  Állapotgép (main.py)              │
│  decide_action() + heartbeat        │
│  state.json: commit-back pattern    │
│  Telegram értesítések               │
└─────────────────────────────────────┘
```

### Miért kell FlareSolverr?

A Downdetector Cloudflare mögött fut. Normál HTTP kérések (requests, curl) 403-at kapnak. A FlareSolverr egy headless Chromium-ot futtat, ami megoldja a Cloudflare JS challenge-et, és visszaadja a bypass cookie-kat.

### Miért kell curl_cffi?

Cloudflare a `cf_clearance` cookie-t a böngésző TLS fingerprintjéhez (JA3/JA4) köti. Ha a cookie-t egy eltérő TLS fingerprintű kliensből használjuk (pl. Python `requests`), 403-at kapunk. A `curl_cffi` a Chrome TLS fingerprintjét utánozza (`impersonate="chrome"`), így a cookie érvényes marad.

### Miért nem elég a FlareSolverr válasz HTML-je?

A FlareSolverr a renderelt DOM-ot adja vissza (JS futtatás után). A Next.js RSC stream (`__next_f.push` script tagek) erre már nincsenek benne, mert a React hydration felhasználta őket. A raw SSR HTML-ben viszont még benne vannak a pontos `reportsValue` adatok.

### Miért raw_decode és nem json.loads?

A `chartData` objektumban a `dataPoints` tömb után további mezők vannak. A regex-alapú extrakció túl sokat vagy túl keveset fog. A `json.JSONDecoder.raw_decode()` pontosan a `[...]` tömböt parse-olja és megáll, függetlenül attól, mi jön utána.

---

## 2. Komponensek és felelősségük

| Komponens | Fájl | Feladat |
|---|---|---|
| Config | `src/config.py` | Env var-ok beolvasása, default értékek |
| Scraper | `src/scraper.py` | FlareSolverr hívás, curl_cffi fetch, RSC/aria-label/heading parse |
| State | `src/state.py` | JSON state load/save, dataclass serialize |
| Notifier | `src/notifier.py` | Telegram üzenetek (alert, recovery, heartbeat, error) |
| Main | `src/main.py` | Orchestrator: fetch → decide → notify → heartbeat → save |
| Workflow | `.github/workflows/monitor.yml` | Cron, FlareSolverr service, secrets, state commit-back |

---

## 3. Tipikus hibák és tüneteik

### "Parsed via aria-label fallback" (WARNING a logban)

**Tünet**: A log `aria-label fallback`-et jelez `RSC strategy` helyett.

**Ok**: A raw HTML nem tartalmazza az RSC `__next_f.push` adatokat, vagy a parser regex nem találja.

**Teendő**:
1. Ellenőrizd a letöltött HTML méretét – ha ~6 KB, Cloudflare blokkolt (nem raw HTML, hanem challenge page)
2. Ha ~280 KB de nincs RSC: a Downdetector változtatta a formátumot. Mentsd el az aktuális HTML-t, elemezd a `reportsValue` környezetét

### "HTTP 403 despite cookies"

**Tünet**: FlareSolverr sikeresen ad cookie-kat, de a curl_cffi fetch 403-at kap.

**Ok**:
- A cookie lejárt (FlareSolverr lassú volt, és a cookie érvényessége rövid)
- TLS fingerprint mismatch (curl_cffi verzió nem egyezik a FlareSolverr Chrome verziójával)
- Cloudflare frissített és az aktuális impersonation profil elavult

**Teendő**:
1. Frissítsd a curl_cffi-t: `pip install --upgrade curl_cffi`
2. Frissítsd a FlareSolverr image-et: `ghcr.io/flaresolverr/flaresolverr:latest`
3. Ellenőrizd a FlareSolverr logot – ha "Challenge detected!" áll benne, a FlareSolverr sem tudja megoldani

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
#            error_alert_sent, last_heartbeat_date
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

points = parse_reports(html)
print(f"Adatpontok: {len(points)}")
print(f"Utolsó érték: {points[-1].value} at {points[-1].timestamp}")
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
- Tünet: 403 a curl_cffi fetch-nél, pedig FlareSolverr sikeresen ad cookie-t
- Megoldás: curl_cffi `impersonate` paraméterét fixáld egy verzióra (pl. `"chrome120"`)

### Downdetector frontend változások

A Downdetector Next.js alkalmazás, és a RSC formátum változhat deploy-onként.
- Tünet: parser nem talál `reportsValue`-t, aria-label fallback-re esik
- Megoldás: mentsd a nyers HTML-t, keresd meg az új formátumot, frissítsd a parsert
- Várható gyakoriság: 2-6 havonta

### GitHub Actions cron pontossága

A GitHub cron ±5-10 perces késéssel fut. Ez a heartbeat ablaknál fontos:
- A heartbeat ablak 30 perces (9:00-9:30 Budapest), ami lefedi ezt a pontatlanságot
- Ha a cron-t sűríted, az ablakot is szűkítheted

### GitHub Actions inaktivitás

60 napig inaktív repók cron-ja automatikusan kikapcsol.
- Megoldás: alkalmanként commitolj (a state.json auto-commit ezt megoldja)

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
     "last_heartbeat_date": null
   }
   ```
6. **Lokális teszt**: Kövesd az 5. pont lépéseit feljebb
7. **Kulcs fájlok**: `src/scraper.py` (fetch + parse), `src/main.py` (logika), `.github/workflows/monitor.yml` (cron)
