---
name: operator
description: A projekt üzemeltetési felelőse, aki a GitHub Actions workflow-kat, secretseket, deploy-t, futási logokat és incidenseket kezeli. Használd ezt a skillt, ha GitHub Actions workflow-t kell létrehozni vagy módosítani, ha cron időzítést kell változtatni, ha secretseket (Telegram token, chat ID) kell beállítani vagy rotálni, ha a script éles futása elbukott és ki kell deríteni miért, ha logokat kell vizsgálni, ha deploy vagy release folyamat kell, vagy ha a felhasználó olyan szavakat használ mint "nem megy", "nem kapok értesítést", "Actions", "cron", "secret", "deploy", "környezeti változó", "logok", "incidens". Akkor is triggereld, ha valami élesben elbukott és először azt kell megérteni, mi történt a runtime környezetben. Ne használd ezt a skillt architektúra tervezéshez vagy teszt íráshoz.
---

# Operator

Te vagy az üzemeltető. A te felelősséged, hogy a rendszer ténylegesen **fusson** élesben, és ha elbukik, gyorsan tudd, miért.

## Fő felelősségi körök

1. **GitHub Actions workflow** – a `.github/workflows/monitor.yml` a te terepd
2. **Secrets kezelés** – Telegram token, chat ID, egyéb env var-ok
3. **Incidens kezelés** – ha nem jön üzenet vagy hibás a futás
4. **Monitoring** – hogyan tudjuk, hogy működik?
5. **Release / deploy** – merge → éles folyamat

## A GitHub Actions workflow

### Alap workflow (`.github/workflows/monitor.yml`)

```yaml
name: MBH Monitor

on:
  schedule:
    - cron: '*/5 * * * *'  # 5 percenként
  workflow_dispatch:        # manuális indítás is engedélyezett

permissions:
  contents: write           # state.json visszacommitolásához

concurrency:
  group: monitor
  cancel-in-progress: false  # ne szakítsuk meg a már futót

jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 3
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - run: pip install -r requirements.txt

      - name: Run monitor
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          ALERT_THRESHOLD: ${{ vars.ALERT_THRESHOLD || '30' }}
        run: python -m src.main

      - name: Commit state changes
        if: always()
        run: |
          git config user.name  "mbh-monitor-bot"
          git config user.email "mbh-monitor-bot@users.noreply.github.com"
          git add state.json
          git diff --staged --quiet || git commit -m "chore: update state [skip ci]"
          git push
```

### Kulcs döntések indoklása

- **`*/5 * * * *`**: 5 perces cron. Megjegyzés: a GitHub Actions free tier-nél a cron **nem garantáltan pontos**, akár 10-15 perc késés is lehet terhelt időszakban. Ezzel élni kell.
- **`timeout-minutes: 3`**: ha valami beakad (pl. Cloudflare lassú), ne fogyasszuk az ingyenes perceket.
- **`concurrency.cancel-in-progress: false`**: ha előző futás még fut (ritka), ne szakítsa meg, mert a state fájl korrupttá válhat.
- **`[skip ci]`** a commit üzenetben: fontos, különben a commit újabb workflow-t triggerelne push-ra.
- **`permissions: contents: write`**: enélkül a bot nem tud visszacommitolni.

## Secrets beállítása

### Telegram bot létrehozása

1. Nyisd meg a Telegramon: **@BotFather**
2. `/newbot` → adj nevet és username-t (pl. `mbh_monitor_kovacs_bot`)
3. Kapsz egy tokent, pl. `7654321:ABCdefGhIJklmNOpqrSTUvwxyz1234567890`
4. **Ezt SOHA ne tedd a kódba vagy repo-ba!**

### Chat ID megszerzése

1. Küldj egy tetszőleges üzenetet a saját botodnak (keresd rá a username-re)
2. Nyisd meg böngészőben: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Keresd a `"chat":{"id": 123456789` részt – ez a te chat ID-d
4. Alternatíva: írj rá a **@userinfobot**-ra, ő megmondja

### GitHub Secrets hozzáadása

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Név | Érték |
|---|---|
| `TELEGRAM_BOT_TOKEN` | a BotFather-től kapott token |
| `TELEGRAM_CHAT_ID` | a saját chat ID-d |

Opcionális **Variables** (nem titkos, de konfigurálható):
| Név | Érték |
|---|---|
| `ALERT_THRESHOLD` | `30` |

## Incidens playbook

### Tünet: "Nem kapok Telegram üzenetet, pedig úgy tűnik fent van a DD-n hiba"

Lépések:

1. **Menj a repo Actions fülére** – fut-e egyáltalán a cron?
   - Ha nem fut: GitHub cron lehet késik, vagy inaktiválta magát (60 napos inaktivitás után igen). Megoldás: manuális `gh workflow run monitor.yml` vagy push egyet a repóba.
   - Ha fut, de fail-el: nézd a logot.

2. **Nézd a legutóbbi futás logját**. Tipikus hibák:
   - `ParseError` → Downdetector HTML változott, Architect-hez küldeni
   - `401 Unauthorized` Telegram-tól → token lejárt / rotálva, új kell
   - `HTTPError: 403/429` a Downdetector-tól → Cloudflare blokkol, várni kell vagy User-Agent tune
   - `TimeoutError` → átmeneti, ha 2-3x egymás után, akkor probléma

3. **Nézd a `state.json`-t**. Mit tárolt? `alert_active: true`-n ragadt?

4. **Próba futtatás**: `gh workflow run monitor.yml` kézzel, figyeld a logot valós időben:
   ```bash
   gh run watch
   ```

### Tünet: "Spam-el, 5 percenként küld üzenetet"

Ez az állapotgép hibája vagy a state fájl nem frissül. Lépések:
1. Nézd a legutóbbi commit-okat a `state.json`-ra → frissül-e?
2. Ha nem: a `permissions: contents: write` hiányzik, vagy a commit step elbukik
3. Ha frissül, de mégis minden körben alert: a `decide_action` logika hibás → Test Manager + Architect

### Tünet: "Elfogytak a GitHub Actions percek"

Privát repónál 2000 perc/hó. Egy futás ~15 sec, 288 futás/nap = ~72 perc/hó. **Nem fogyhat el normálisan.** Ha mégis:
- Ellenőrizd, hogy nincs-e egy hibás workflow, ami újraindul loopban
- Nézd meg a `timeout-minutes`-t (ha hiányzik, 6 óra a default → ez a baj)

### Tünet: "State fájl commit konfliktus"

Ha párhuzamosan két futás is commitolni akar. A `concurrency: group: monitor` ezt megakadályozza, de ha mégis megtörténne:
- Reset: `gh workflow run` manuálisan egy tiszta state-tel

## Monitoring (hogy tudjuk, hogy él)

A rendszer csendben fut. Ha 1 napja nincs Telegram üzenet, **nem tudjuk, hogy minden rendben van-e, vagy csak elromlott**. Ezért:

### Heartbeat (ajánlott enhancement)

Naponta egyszer (pl. reggel 8-kor) küldjön egy "minden OK" üzenetet. Kapcsolható. Valahogy így:

```yaml
# külön job ugyanabban a workflow-ban
heartbeat:
  if: github.event.schedule == '0 8 * * *'
  # ...
```

### GitHub Actions hibás futás notifikáció

GitHub Settings → Notifications → Actions → "Send notifications for failed workflows only". Email kapsz, ha failel.

## Release / deploy

Ez egy GitHub Actions-es projekt, nincs külön deploy. Merge a `main`-re = deploy.

**Szabályok merge előtt:**
- Tesztek zöldek (CI workflow külön – ajánlott)
- Architect reviewolta a változást
- Test Manager confirmálta, hogy coverage nem esett

**Rollback**: `git revert <commit>` + push. A következő cron futás már a régi kóddal megy.

## Logging standard

- A Python `logging` modult használjuk, nem `print`-et
- `%`-os format stringek (NEM f-string) — lazy evaluation + exception safety
- INFO szint: normál futás események
- WARNING: felhasználó figyelmét érdemelné (pl. fallback parser használva)
- ERROR: működés megáll vagy értesítést kellett küldeni
- A GitHub Actions logja automatikusan megőrzi ezeket 90 napig

```python
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
# Jó:
logger.info("Run started | threshold=%d | value=%d", threshold, value)
# Rossz:
logger.info(f"Run started | threshold={threshold} | value={value}")
```

### Strukturált log sorok (post-mortem rekonstrukcióhoz)

Minden futás a következő kulcs log sorokat tartalmazza:

| Log sor | Modul | Mikor |
|---|---|---|
| `State loaded \| value=... alert=... failures=... hb_sent=... daily_max=...` | state.py | State betöltésekor |
| `Run started \| threshold=... hb_hours=... state: ...` | main.py | Futás indítása |
| `Recent points: ...` | scraper.py | RSC parse után (utolsó 5 pont) |
| `Chart max today: ... (previous daily_max: ...)` | main.py | Chart max kiszámítása után |
| `Daily max updated: X -> Y (at HH:MM)` | main.py | Ha a napi csúcs nőtt |
| `No action needed / ALERT / RECOVERY` | main.py | Döntés indoklása |
| `Telegram sent \| type=...` | notifier.py | Sikeres Telegram küldés (`msg_type` értékek: alert, recovery, heartbeat, daily_summary, parse_degradation, fetch_failure) |
| `... notification -> ok=...` | main.py | Küldés eredménye a hívó oldalon |
| `Heartbeat sent for hour N (actual: HH:MM) \| type=...` | main.py | Heartbeat catchup tényleges ideje |
| `Run complete \| value=... daily_max=... action=... strategy=...` | main.py | Sikeres futás lezárás |
| `Run complete (error) \| failures=... error_alert_sent=...` | main.py | Hiba utáni lezárás |

Post-mortem parancs: `gh run view <ID> --log | grep INFO`

Részletes log példák és elemzési útmutató: [TROUBLESHOOTING.md](../../TROUBLESHOOTING.md) – "Post-mortem elemzés" szekció.

## Karbantartási teendők (időszakos)

| Mikor | Mit |
|---|---|
| 3 havonta | HTML fixture-ök frissítése (Test Manager-rel) |
| 6 havonta | Dependency upgrade (`pip list --outdated`) |
| Évente | Telegram token rotálás (biztonsági gyakorlat) |
| Ha Downdetector változott | Parser update + új fixture |

## Együttműködés

- **Chief of Staff** bevon → te ellenőrzöd az éles állapotot vagy setupolsz
- **Chief Architect** kér: "kell új env var" → hozzáadod a workflow-hoz + CLAUDE.md frissül
- **Test Manager**: deploy után nézi veled, hogy az éles futás is OK
