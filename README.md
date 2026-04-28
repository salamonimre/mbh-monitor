# MBH Monitor – Step-by-Step Setup

Ez az útmutató végigvezet a projekt felállításán a nulláról az első működő értesítésig. Az egész folyamat ~1-2 óra, nagy részét Claude Code fogja csinálni.

## Mit fogsz kapni a végére

- Egy GitHub repo, ami ~15 percenként lekéri a Downdetector adatot (dupla trigger: GitHub Actions cron + cron-job.org)
- Telegram értesítés, ha a bejelentett hibák száma átlépi a küszöböt (alapértelmezés: 10)
- Helyreállás üzenet, amikor visszamegy a szám
- Reggeli heartbeat + esti napi összefoglaló (napi max, volt-e alert, PAT lejárat figyelmeztetés)
- Automatikus degradáció-érzékelés: Telegram alert ha a scraper fallback stratégiára vált
- Debug HTML artifact ha a parser nem talál adatot
- Strukturált logolás: a GitHub Actions logból visszaállítható teljes napi riport (post-mortem elemzés)
- Havi költség: **0 Ft**

---

## 0. fázis – Előkészületek (10 perc, kézzel)

### 0.1. Amire szükséged lesz

- [ ] GitHub fiók
- [ ] Git telepítve helyben
- [ ] Claude Code telepítve ([telepítő útmutató](https://docs.claude.com))
- [ ] Telegram app a telefonodon

### 0.2. Telegram bot létrehozása

1. Nyisd meg a Telegramot, keress rá: **@BotFather**
2. Indítsd el a beszélgetést, írd be: `/newbot`
3. Adj neki nevet (pl. `MBH Monitor`) és username-t (pl. `mbh_monitor_kovacs_bot` – `_bot`-ra kell végződjön, és egyedi kell legyen)
4. Kapsz egy tokent – **mentsd el átmenetileg**, később kell. Kb. így néz ki: `7654321:AAHxyz...`

### 0.3. Chat ID megszerzése

A bot csak akkor tud neked üzenni, ha tudja a chat ID-dat.

1. A Telegramon keresd meg a most létrehozott botodat (a username alapján), nyomj rá `Start`-ot, és küldj neki bármilyen üzenetet (pl. "hello").
2. Nyisd meg a böngészőben (cseréld ki a tokent):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. Keresd a JSON-ban ezt: `"chat":{"id":123456789,...` – ez a szám a chat ID-d. **Mentsd el.**

**Alternatív módszer**: a @userinfobot megmondja a saját user ID-dat (ami magánchatnél azonos a chat ID-val).

---

## 1. fázis – Repo létrehozása (5 perc, kézzel)

### 1.1. Új GitHub repó

1. GitHub → New repository
2. Név: `mbh-monitor` (vagy ami tetszik)
3. **Privát** (mert a state.json és a commit history látható lesz, de nem nyilvánosságnak való)
4. README nélkül, gitignore nélkül (mi hozzuk)

### 1.2. Klónozás helyben

```bash
git clone git@github.com:<felhasznalonev>/mbh-monitor.git
cd mbh-monitor
```

### 1.3. A kapott fájlok bemásolása

Másold be a kapott fájlokat a repo gyökerébe:
- `CLAUDE.md`
- `README.md` (ez a fájl)
- `.claude/` mappa az összes skillel

Ellenőrzés:
```bash
ls -la
# látnod kell: CLAUDE.md, README.md, .claude/
ls .claude/skills/
# látnod kell: chief-of-staff, chief-architect, test-manager, operator
```

Első commit:
```bash
git add .
git commit -m "chore: initial project setup with CLAUDE.md and skills"
git push
```

---

## 2. fázis – Claude Code megépíti a projektet (30-60 perc)

### 2.1. Claude Code indítása

A repo mappában:
```bash
claude
```

### 2.2. Első kérés: építtessük meg

Írd be Claude Code-nak (szó szerint, vagy hasonlóan):

> Olvasd el a CLAUDE.md fájlt és a `.claude/skills/` alatti skilleket. A Chief of Staff szerint kezdjük el a projektet: kérem, hogy a Chief Architect tervezze meg a modulokat a CLAUDE.md-ben vázolt struktúra szerint, majd implementáljuk, a Test Manager írja meg a teszteket, az Operator pedig állítsa be a GitHub Actions workflow-t. Használjunk Python 3.11-et.

**Mi fog történni:**
- A Chief of Staff skill triggerelődik, átveszi az irányítást
- Behívja a Chief Architect-et, aki megtervezi és kódolja a modulokat (`src/` alatt)
- Behívja a Test Manager-t, aki megírja a teszteket
- Behívja az Operator-t, aki létrehozza a GitHub Actions workflow-t

**A te feladatod közben**: válaszolj a tisztázó kérdésekre. Pl. "Hétvégén is küldjön értesítést?" – te döntesz.

### 2.3. HTML fixture-öket manuálisan kell szerezni

Egy ponton a Test Manager kérni fogja, hogy adj neki valódi HTML mintát a Downdetector oldalról. Így csináld:

1. Nyisd meg Chrome/Firefox-ban: `https://downdetector.hu/problema/mbh-bank/`
2. Jobbklikk → **"Az oldal forrásának megjelenítése"** (NE az Inspect, hanem a view-source!)
3. Mentsd el `tests/fixtures/normal_response.html` néven
4. Commitold

Ha történetesen épp sok bejelentés van, azt is mentsd el külön `high_alert_response.html` néven – az aranyat ér tesztelésre.

### 2.4. Első lokális futtatás

Miután Claude Code megcsinálta a kódot:

```bash
# Függőségek
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Környezeti változók lokálisan
export TELEGRAM_BOT_TOKEN="a_te_tokened"
export TELEGRAM_CHAT_ID="a_te_chat_id_d"
export ALERT_THRESHOLD="1"  # ALACSONY, hogy biztosan triggereljen tesztelésre

# Futtatás
python -m src.main
```

Ha minden jól megy, kapsz egy Telegram üzenetet. Ha nem, a Claude Code-nak mondd: "Az Operator nézzen rá, nem kapok Telegram üzenetet a lokális futtatásból, itt a log: [másold be]"

### 2.5. Tesztek lefuttatása

```bash
pytest tests/ -v
pytest tests/ --cov=src --cov-report=term-missing
```

Ha valami piros, Claude Code javítja.

---

## 3. fázis – GitHub Secrets beállítása (5 perc, kézzel)

A GitHub Actions-nek szüksége van a Telegram adatokra.

1. Menj a repóba GitHub-on
2. **Settings → Secrets and variables → Actions**
3. **New repository secret**, add hozzá:
   - Név: `TELEGRAM_BOT_TOKEN`, érték: a BotFather token
   - Név: `TELEGRAM_CHAT_ID`, érték: a chat ID-d
4. Opcionális: **Variables** fül → New variable:
   - Név: `ALERT_THRESHOLD`, érték: `10`

---

## 4. fázis – Első éles futás (5 perc)

### 4.1. Manuális trigger

GitHub-on a repo → **Actions** fül → `MBH Monitor` workflow → **Run workflow** gomb.

Nézd meg a logot:
- Zöld pipa: minden OK
- Piros X: kattints rá, nézd a lépéseket, másold az Operator-nak

### 4.2. Alacsony küszöb teszttel

Először állítsd be ideiglenesen alacsonyra a küszöböt (pl. 1), hogy biztosan kapj üzenetet:

- GitHub → Settings → Variables → `ALERT_THRESHOLD` → érték `1`
- Actions → Run workflow
- **Ha kapsz Telegram üzenetet: működik!** 🎉
- Állítsd vissza 30-ra

### 4.3. Várd meg az első automatikus cron futást

A workflow `15,45 * * * *`-vel van ütemezve (GitHub Actions belső cron, `:15` és `:45`-kor). A GitHub ingyenes cron **pár perces késéssel** szokott indulni. Nézd meg 20 perc múlva az Actions fület, hogy van-e ott új futás magától.

**Opcionális**: cron-job.org külső triggert is beállíthatsz `:00` és `:30`-ra, így ~15 percenként lesz coverage. Lásd: [TROUBLESHOOTING.md](TROUBLESHOOTING.md) – "cron-job.org trigger leállása" szekció a setup részletekért.

---

## 5. fázis – Üzemeltetés (ongoing)

### 5.1. Mit figyelj

- **Actions fül hetente egyszer**: vannak-e piros pipák?
- **Telegram üzenetek**: kapsz-e "helyreállás" üzenetet mindig alert után?

### 5.2. Amikor baj van

Nyisd meg Claude Code-ot a repóban, és mondd:

> Nem kapok értesítést 2 napja, pedig látszik hogy volt MBH hiba. Operator, derítsd ki mi van.

Az Operator skill triggerelődik, megnézi a logokat, a state fájlt, és javít (vagy elküldi Architect-hez, ha kódbeli hiba).

### 5.3. 3 havonta

A Test Manager frissítse a HTML fixture-öket. Mondd Claude Code-nak:

> Test Manager, frissítsük a tests/fixtures/-öket új Downdetector HTML-lel. Itt az új minta: [paste vagy töltsd fel]

---

## Gyakori problémák

### "A GitHub Actions cron nem fut"

60 napig inaktív repók ütemezése automatikusan kikapcsol. Megoldás: commitolj valamit, vagy futtass manuálisan egyszer.

### "Cloudflare blokkol"

A Downdetector néha szűri a bot trafficot. A jelenlegi megoldás FlareSolverr + curl_cffi Chrome TLS fingerprint.

**Első lépés**: próbáld módosítani a `CURL_CFFI_IMPERSONATE` GitHub Actions változót (pl. `chrome136` → `chrome131`). Ha ez sem segít: frissítsd a curl_cffi és FlareSolverr verziókat. Ha rendszeresen blokkol: ezzel a projekttel ennyit lehet, át kellene térni fizetős API-ra.

### "A state.json konfliktusban van"

Fut két példány párhuzamosan, valamelyik visszacommitolt közben. A `concurrency` beállítás ezt megakadályozza, de ha véletlen mégis: GitHub-on `gh` CLI-vel, vagy a felületen reszeted a state.json-t.

### "Túl sok a Telegram üzenet"

Az állapotgép hibás. Mondd a Test Manager-nek: "Az alert logika flappinget okoz, reprodukálj tesztben és javítsuk."

---

## Összefoglaló: a Claude Code workflow

| Mit akarsz csinálni | Mit írj Claude Code-nak |
|---|---|
| Új feature | "Chief of Staff, szeretném hogy [X]. Hogy csináljuk?" |
| Bug jelentés | "Nem megy [X], itt a log: [...]. Operator kezdd el, vond be a Test Manager-t ha reprod kell." |
| Kód átnézés | "Chief Architect, review-old át az src/scraper.py-t" |
| Státusz | "Chief of Staff, hol állunk?" |
| Deploy | A merge maga a deploy – csak push a main-re |

A **Chief of Staff** skill általában az első, aki bekapcsolódik, és ő hívja a többit. Neked csak egy személlyel kell beszélned – vele.

Sikeres monitorozást! 🚀
