# Data sources (Part 4) — what each provides, its cadence, and its freshness threshold

Every source lands in the **same 500-day store** through `integrity`-routed parsers. A feed
older than its freshness threshold is flagged stale on the dashboard and in `health.json`
(never shown as live, §A rule 6).

| Source | Module | Provides | Real cadence | Stream | Freshness (stale after) |
|---|---|---|---|---|---|
| Dexcom G7 / Nightscout | `glucose.py` | CGM glucose (mg/dL) | 5 min | `glucose` (store) | **30 min** |
| Health Connect export sheet | `wearables.py` | steps, HR, SpO₂, sleep, weight, hydration | hourly (Activity/Vitals), daily (Sleep) | `wearables.json` | **6 h** |
| Telegram logger | `journal.py` (+ `phone/apps_script`) | meals (carbs+tags), interventions, supplements, symptoms, mood, energy, notes | manual | `log` | **36 h** |
| Bearable | `bearable.py` | mood, energy, symptoms, supplement adherence | nightly CSV export | `log` | **36 h** |
| HealthifyMe | `healthifyme.py` | food / macros (carbs, protein, fat, fiber, kcal) | per-meal log + periodic CSV | `log` | **24 h** |
| Oura Ring (optional) | `oura.py` | sleep stages, resting HR, activity | daily (API v2) | `wearables.json` | **18 h** |
| Smart scale (optional) | `wearables.py` (Health Connect) | weight, body-fat | per weigh-in | `wearables.json` | per device |
| Labs | `store` events `labs` | lab markers (value, unit, ref range) | ad-hoc | `labs` (store) | **180 d** |

## Stream conventions
- **`log`** (journal records): Telegram logger, Bearable, HealthifyMe all append here. Read by
  `food_impact` / `experiments` / `symptoms` / `mood_energy`. Idempotent by record `key`.
- **`wearables.json`**: Health Connect export + Oura, deduped to one preferred source per day.
- **`labs`** / **`supplement`** (store event streams): structured feeds surfaced in the Excel
  workbook (Labs / Supplements sheets) and the heartbeat. Bearable *supplement* rows go to the
  `log` stream (as `type="supplement"`), not the structured `supplement` events stream.

## Bearable export (`bearable.py`)
Long CSV: `date, time of day, category, detail, rating, notes`. Categories map to journal
fields — Mood→`mood_1to5`, Energy→`energy_1to5`, Symptom→`symptom`+`symptom_sev_1to5`,
Supplement/Medication→`supplements`, Sleep→`sleep_h`. Ratings parsed from `Label (n)`.
Drop-in as a `log_source`: `bearable.CsvBearableSource(path=...)`.

## HealthifyMe export (`healthifyme.py`)
CSV: `date, meal, food, calories, carbs, protein, fat, fiber`. Each food → a `type="meal"`
journal record with `net_carbs_g` set, so it appears in the next food-impact computation.
If HealthifyMe writes Nutrition to **Health Connect**, the wearables sheet carries it instead.

## Oura (`oura.py`)
`OuraSource(token, days=14)` emits rows in the Health Connect export shape, so it's a drop-in
`wearables_source` (zero engine change). One consolidated sleep row per day. `OURA_TOKEN` is a
secret (env only).

### Acceptance (Part 4)
A Bearable export ingests as correct mood/energy/symptom rows; a logged meal marker (Telegram
or HealthifyMe) appears in the next food-impact computation.
