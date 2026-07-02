# Go live — a URL you can refresh anytime, fed by your real data

This is the shortest path from the repo to a **private, always-on dashboard** at a URL you open
on your phone. Everything below is one-time setup; after it, the dashboard rebuilds itself every
15 minutes from your live data and you just refresh.

The engine already supports all of this — the only things it needs from you are (a) credentials
in the platform secret store and (b) a hosting choice with an access gate. **No secret ever lives
in the repo or the published page** (`cycle.py`'s leak gate aborts the publish if one does).

---

## 0. Make the repo private first (it currently is public)

A health dashboard must not be world-readable. In GitHub → **Settings → General → Danger Zone →
Change visibility → Private**. GitHub Pages still works on private repos (Pro/Team).

## 1. Glucose — the live 5-minute feed

Pick one (both are UAE-OK):
- **Nightscout** (recommended backbone): stand up a managed (T1Pal / Nightscout Pro) or free
  (Oracle Always-Free) instance with the Dexcom **Share bridge**, `CONNECT_SHARE_REGION=ous`.
  Set repo secrets `NS_URL`, `NS_TOKEN`.
- **Dexcom Share directly**: enable one follower in the G7 app; set `DEXCOM_USERNAME` /
  `DEXCOM_PASSWORD` (not all-numeric) and variable `DEXCOM_REGION=ous`.

With neither set, the cron falls back to the committed demo fixture (safe to test the pipeline).

## 2. Your Google Sheets (journal + wearables + labs)

The engine reads your sheets via a **Google service account** (server-side, no phone dependency):

1. Google Cloud Console → create a **service account** → create a **JSON key**.
2. Enable the **Google Sheets API** for the project.
3. **Share each sheet** (View access) with the service account's email
   (`…@…iam.gserviceaccount.com`):
   - **Health Log — Aarsh Vir Gupta (data)** → variable `HEALTH_LOG_SHEET_ID`
   - **Health Data Export** → variable `WEARABLES_SHEET_ID`
   - A **Labs** sheet/tab (columns: `date, marker, value, unit, ref_high[, ref_low]`;
     markers like `HbA1c, ALT, AST, GGT, hs-CRP, Testosterone, Prolactin, Vitamin D`) →
     variable `LABS_SHEET_ID`
4. Paste the JSON key into repo secret `GOOGLE_SA_JSON`.

The sheet *IDs* are the long string in each sheet's URL. They're not secrets, but set them as
Actions **variables** (not committed) so nothing personal lands in the repo.

## 3. Turn on the 15-minute cron → your dashboard URL

- Settings → **Pages** → Source = **GitHub Actions**.
- `.github/workflows/pages.yml` already runs `healthhub-cycle` every `*/15` and on demand
  (Actions → "Cycle → Pages" → *Run workflow*). It builds `dashboard.html` (+ `metrics.json`,
  `labs.json`, `reversal.json`, `correlation.json`, `HealthOS_500d.xlsx`) from the **same store**
  and deploys `publish/`.
- The PWA is **network-first**: opening/refreshing the URL shows the latest cycle; offline it
  shows the last cached one with true "as of" ages. Returning to the tab auto-refreshes.

### Access gate (Pages has no built-in auth)
Put the site behind one of these so only you can see it:
- **Cloudflare Access** (recommended) — Zero-Trust app in front of the Pages site; require your
  email / one-time PIN. Use a random project name (no personal name in the URL).
- **Vercel** — deploy `publish/` to a Vercel project with **Password Protection** (swap the
  deploy step for `vercel deploy --prebuilt`).

## 4. Add it to your phone
Open the URL in Chrome → **Add to Home screen**. It installs as a standalone app (the manifest +
service worker are already wired) and opens on the last tab you used.

## 5. Alerts (optional but recommended)
Set `TELEGRAM_TOKEN` + variable `TELEGRAM_CHAT_ID` (or `SMTP_*` + `ALERT_EMAIL_TO`). If any feed
goes stale, you get a message within one cycle and the dashboard flags **that** source — the rest
stays fresh.

---

### Secrets vs variables (GitHub → Settings → Secrets and variables → Actions)
| Secrets (sensitive) | Variables (non-sensitive) |
|---|---|
| `GOOGLE_SA_JSON`, `NS_TOKEN` / `DEXCOM_PASSWORD`, `TELEGRAM_TOKEN`, `SMTP_PASSWORD` | `HEALTH_LOG_SHEET_ID`, `WEARABLES_SHEET_ID`, `LABS_SHEET_ID`, `DEXCOM_USERNAME`, `DEXCOM_REGION`, `NS_URL`, `TELEGRAM_CHAT_ID` |

### Acceptance
Run the workflow once by hand → the deployed URL shows your real glucose/labs/wearables with
freshness chips; kill one feed → within a cycle that source flips to stale and you get an alert,
while the others stay correct.
