# Cloud / always-on layer (Part 3)

Stand up the always-on backbone: Nightscout (or Dexcom Share) → a 15-minute engine cron →
an access-gated dashboard → a pipeline heartbeat with alerts. **No secret ever lives in the
repo or the rendered output** — `cycle.py`'s leak gate (`config.scan_paths`) aborts the
publish if any secret value reaches an artifact.

## 1. Nightscout backbone (recommended) or Dexcom Share
- **Managed**: T1Pal / Nightscout Pro. **Free**: Oracle Cloud Always-Free VM running Nightscout.
- Dexcom **Share bridge**: `CONNECT_SHARE_REGION=ous` (outside-US). `BRIDGE_USER_NAME` /
  `BRIDGE_PASSWORD` = your Dexcom account.
- **`API_SECRET` must NOT equal your Dexcom password.** Set `AUTH_DEFAULT_ROLES=denied` and
  issue a read **token** for the engine (`NS_TOKEN`) — the public site exposes nothing without it.
- The engine reads via `glucose.NightscoutSource(NS_URL, token=NS_TOKEN)`; if Nightscout is
  absent it falls back to `glucose.PydexcomSource(region="ous")`, then to the demo fixture.

## 2. The 15-minute cron → GitHub Pages
- `.github/workflows/pages.yml` runs `healthhub-cycle --out-dir publish` every `*/15` (and on
  `workflow_dispatch`), copies `dashboard.html`→`index.html`, and deploys `publish/` to Pages.
- The cycle writes `metrics.json`, `trend.json`, `wearables.json`, `health.json`,
  `dashboard.html`, and `HealthOS_500d.xlsx` — all from the **same store** (§A rule 7).
- `run.json` records the run; `.nojekyll` lets Pages serve `_`-prefixed files.

### Access gate (no name in the URL)
GitHub Pages has **no native auth**, so gate it at the edge — pick one:
- **Cloudflare Access** (recommended for Pages): put the Pages site behind a Cloudflare
  Zero-Trust application; require your email / a one-time PIN. The public URL stays
  unguessable and unauthenticated requests are blocked before they reach Pages.
- **Vercel** instead of Pages: deploy `publish/` to a Vercel project with **Password
  Protection** (Pro) or Vercel Authentication. Swap the deploy job for `vercel deploy --prebuilt`.
- **Token query gate** (lightweight): serve via a Cloudflare Worker that checks `?k=<token>`
  and sets a cookie. Weakest option; combine with an unguessable URL.

Either way: use a random project/site name (no personal name in the URL) and keep the repo
private.

## 3. Secrets — platform secret store only
Set these as **GitHub Actions secrets** (sensitive) / **variables** (non-sensitive), never in
the repo. See `.env.example` for local `.env` use.

| Secret | Variable |
|---|---|
| `DEXCOM_PASSWORD`, `NS_TOKEN`, `NS_API_SECRET`, `GOOGLE_SA_JSON`, `ANTHROPIC_API_KEY`, `TELEGRAM_TOKEN`, `SMTP_PASSWORD`, `OURA_TOKEN` | `DEXCOM_USERNAME`, `DEXCOM_REGION`, `NS_URL`, `HEALTH_LOG_SHEET_ID`, `WEARABLES_SHEET_ID`, `TELEGRAM_CHAT_ID`, `MODEL_FAST`, `MODEL_DEEP` |

`config.assert_no_secrets` / `config.scan_paths` guarantee none of the *secret values* appear
in `publish/`.

## 4. Pipeline heartbeat + alerts
- Each cycle computes `heartbeat.status` per source (glucose 30 m, wearables 6 h, log 36 h,
  labs 180 d) and writes `health.json` (a machine-readable endpoint you can ping externally).
- The dashboard shows a per-source heartbeat strip (🟢/🟡/🔴) under the header; a stale feed
  flips to amber/red with its true age — **never shown as live** (§A rule 6).
- Any stale/down feed fires an alert via `heartbeat.default_alerter()` (Telegram when
  `TELEGRAM_*` is set, email when `SMTP_*`/`ALERT_EMAIL_TO` is set).

### Acceptance (Part 3)
Kill one feed (stop the Dexcom bridge / clear the sheet) → within one 15-min cycle the
dashboard flags **that** source `stale (age)`, `health.json.overall_ok` is `false`, you get a
Telegram/email alert, and the other tiles stay fresh and correct.
