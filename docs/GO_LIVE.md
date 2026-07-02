# Go live — a private URL you refresh anytime, fed by ALL your live data

One-time setup (~15 minutes of clicking). After it, the dashboard rebuilds every 15 minutes
from your live sources and the URL always shows the latest cycle — network-first, no lag; if
you're offline it shows the last cycle with its true age.

**Architecture (all free):** GitHub Actions cron (`*/15`) runs `healthhub-cycle` → deploys to
**Cloudflare Pages**, gated by **Cloudflare Access** (only your email can open it). The public
GitHub Pages URL only ever serves **demo data** — the workflow refuses to publish real data
there (and won't even upload it as a downloadable artifact). No secret or health value ever
lands in the repo (`cycle.py`'s leak gate aborts the publish if one does).

## Data sources — all enabled, engine picks the freshest

| Stream | Source | How it flows |
|---|---|---|
| Glucose (5-min) | **Nightscout** or **Dexcom Share** | live pull each cycle (preferred when configured) |
| Glucose (sparse) | **Health Log sheet** CGM checks | always merged — your logged readings are real readings, so the dashboard runs on YOUR data even before a 5-min feed is wired |
| Journal (meals/mood/symptoms/supplements/sex/walks) | **Health Log — (data)** Google Sheet | every cycle |
| Wearables (steps/sleep/HR/SpO₂/weight) | **Health Data Export** Google Sheet | every cycle — ALL tabs (Activity/Body/Sleep/Vitals) are read |
| Labs (liver/inflammation/hormones) | a **labs** tab or sheet (`date,marker,value,unit,ref_high`) | every cycle → Reversal tab |
| Oura (later) | `OURA_TOKEN` | drop-in when you get the ring |

Demo fixture is used ONLY when nothing real is configured — never mixed with real data.

## Step 1 — Cloudflare (the private live URL)
1. Free account at dash.cloudflare.com → **Workers & Pages → Create → Pages** → name it
   something non-identifying, e.g. `hh-cockpit` (this becomes `hh-cockpit.pages.dev`).
2. **My Profile → API Tokens → Create Token** → template *"Cloudflare Pages — Edit"*.
3. In GitHub → Settings → Secrets and variables → Actions:
   - secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` (dash → right sidebar)
   - variable: `CF_PAGES_PROJECT` = the project name
4. Gate it: Pages project → **Settings → Access policy → Enable** → policy = your email.
   Now the URL 404s/redirects for everyone but you (one-time email PIN per device).

## Step 2 — Google Sheets (journal + wearables + labs)
1. console.cloud.google.com → new project → enable **Google Sheets API** → create a
   **service account** → add a **JSON key**.
2. Share each sheet (Viewer) with the service account's `…@…iam.gserviceaccount.com` email:
   - *Health Log — Aarsh Vir Gupta (data)* → variable `HEALTH_LOG_SHEET_ID`
   - *Health Data Export* → variable `WEARABLES_SHEET_ID`
   - your labs sheet/tab → variable `LABS_SHEET_ID` (can be the Health Log sheet's ID with a
     tab named `labs`; columns `date, marker, value, unit, ref_high[, ref_low]` — markers like
     `HbA1c, ALT, AST, GGT, hs-CRP, Testosterone, Prolactin, Vitamin D, Homocysteine`)
3. GitHub secret `GOOGLE_SA_JSON` = the JSON key file's contents.
   (Sheet IDs = the long string in each sheet's URL; set as **variables**, not committed.)

## Step 3 — 5-minute glucose (when ready; sheet CGM checks flow meanwhile)
- **Nightscout** (recommended): managed (T1Pal/Nightscout Pro ~£5/mo) or free Oracle VM, with
  the Dexcom Share bridge (`CONNECT_SHARE_REGION=ous`). Secrets: `NS_URL`, `NS_TOKEN`.
- **Dexcom Share direct**: enable one follower in the G7 app. Secrets: `DEXCOM_USERNAME`,
  `DEXCOM_PASSWORD` (not all-numeric); variable `DEXCOM_REGION=ous`.

## Step 4 — first run + phone
- Actions → **"Cycle → live dashboard"** → *Run workflow*. Open `https://<project>.pages.dev`,
  pass the Access check — that's your live cockpit.
- On the phone: open it in Chrome → **Add to Home screen** (installs as a standalone app;
  reopens on your last tab; auto-refreshes when you return to it).

## Step 5 — alerts (recommended)
Secret `TELEGRAM_TOKEN` + variable `TELEGRAM_CHAT_ID` (or SMTP secrets). A stale feed alerts
you within one cycle, and the dashboard flags exactly which source went quiet.

---

### Secrets vs variables (GitHub → Settings → Secrets and variables → Actions)
| Secrets | Variables |
|---|---|
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `GOOGLE_SA_JSON`, `NS_TOKEN`/`DEXCOM_PASSWORD`, `TELEGRAM_TOKEN` | `CF_PAGES_PROJECT`, `HEALTH_LOG_SHEET_ID`, `WEARABLES_SHEET_ID`, `LABS_SHEET_ID`, `DEXCOM_USERNAME`\*, `DEXCOM_REGION`, `NS_URL`\*, `TELEGRAM_CHAT_ID` |

\* fine as secrets too if you prefer.

### Acceptance
Manual run → the `pages.dev` URL (behind Access) shows your real glucose/journal/wearables/labs
with per-stream freshness; the public GitHub Pages URL still shows only demo data; kill one
feed → within a cycle that source flips stale + you get an alert, the rest stays fresh.
