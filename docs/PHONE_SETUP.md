# Phone layer setup (Android · Watch4 Classic · Dexcom G7)

The repo ships the **software** for the phone layer; this doc covers the on-device wiring and
the acceptance checks. No extra hardware beyond the phone, the watch, and the G7.

## 1. Glucose: xDrip+ direct + Dexcom Share cloud path
- **xDrip+ reads the G7 directly** (true 5-min): xDrip+ → Settings → Hardware Data Source →
  *Dexcom G7/ONE+* → Bluetooth scan → pair the transmitter. Disable battery optimization for
  xDrip+ so the 5-min collector isn't killed.
- **Cloud path (one follower):** in the Dexcom G7 app enable **Share** and add a single
  follower account. The engine reads this via `glucose.PydexcomSource(region="ous")` (Dexcom
  Share, outside-US region). Alternatively run Nightscout and use `glucose.NightscoutSource`.
- Both paths land in the **500-day idempotent store** keyed by timestamp, so the direct and
  cloud streams de-duplicate automatically.

## 2. Wearables: Health Data Export → Google Sheet
- Install **Health Data Export** (Health Connect reader). Configure an automatic export to a
  Google Sheet: **Activity + Vitals hourly, Sleep daily** (weight/hydration as available).
- The sheet matches the schema `wearables.py` parses (Activity / Body / Hydration / Sleep /
  Vitals tables, `Asia/Dubai`). Multiple source apps report the same day
  (`android`, `com.sec.android.app.shealth`, `com.android.healthconnect…`, `life.simple`);
  `wearables.py` **de-duplicates to one preferred source per day**.
- **Samsung auto-sync is flaky.** Samsung Health → Health Connect sync sometimes stalls for
  hours and Health Data Export can silently export a stale day. Mitigations:
  - Treat the sheet as **last-known-good**: `wearables.self_check` quarantines future-dated
    rows; the dashboard shows each tile's freshness, never a fake "live" number.
  - **Manual-export fallback:** when a day looks stale, open Health Data Export → *Export now*
    (or Samsung Health → Health Connect → *Sync now*), then re-run the cycle. The store is
    idempotent, so a manual re-export never creates duplicates.

## 3. Logging: Telegram bot → `logs` sheet (Apps Script)
- Create a bot via **@BotFather**, copy the token.
- Open the target Spreadsheet → Extensions → **Apps Script**, paste
  [`phone/apps_script/Code.gs`](../phone/apps_script/Code.gs). Set Script Properties
  `TELEGRAM_TOKEN`, optional `ALLOWED_CHAT_ID`, optional `SHEET_ID`.
- Deploy as a **Web app** (execute as you, anyone) and register the webhook:
  `https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WEB_APP_/exec_URL>`.
- Message grammar (one per line):
  ```
  /meal Paneer carbs=38 protein=40 fat=47 kcal=746 tags=+walk,+vegfirst
  /intervention +acv          /supplement Triphala
  /symptom feverish sev=2      /mood 3      /energy 2      /glucose 150
  /note free text…
  ```
  Each message appends one row in the exact Health Log schema `journal.py` reads; `+acv`,
  `+walk`, `+methi`, `+vegfirst` are captured as tags that `experiments.py` compares.

## 4. Offline PWA
- Files: [`phone/pwa/manifest.json`](../phone/pwa/manifest.json),
  [`phone/pwa/service-worker.js`](../phone/pwa/service-worker.js). The cycle writes
  `dashboard.html` (+ `metrics.json`, `trend.json`, `wearables.json`) next to them.
- Host them together (GitHub Pages / any static host). Open the URL on the phone →
  **Add to Home Screen**. The service worker caches the shell + artifacts so it opens with no
  connection.
- **§A rule 6:** the dashboard recomputes every value's **age + freshness on the device
  clock** from the embedded `as of` timestamp. Airplane mode shows the last-known-good values
  with correct ages and a 🟡 STALE / 🔴 EXPIRED badge — it never claims to be live.

## Acceptance tests
1. **Glucose within 5 min** — start a sensor session; a new value appears in xDrip+ and (after
   a cycle) on the dashboard within one 5-minute cycle.
2. **Telegram → logs row** — send `/meal Rice carbs=60 tags=+walk`; confirm a correctly-typed
   row (type=meal, item=Rice, net_carbs_g=60, tags=+walk) appears in the `logs` sheet.
3. **Airplane mode** — enable airplane mode, open the PWA from the home screen; the dashboard
   opens and shows the last-known-good values with correct "as of" ages and STALE/EXPIRED
   badges. (Automated proxy for this rule: `tests/test_pwa.py` + the dashboard freshness
   tests.)
