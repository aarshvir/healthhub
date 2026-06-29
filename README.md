# healthhub

A small, safety-first core for a health-data platform. It has two layers:

1. **`integrity.py` — the single trust boundary.** Every other module obtains *trusted*
   readings, timestamps, cached last-known-good values and *validated* narration **only**
   through it. This routing is not a convention — it is **enforced** by
   `tests/test_architecture.py`, which fails the build if any other module bypasses it.
2. **`clinical.py` — glucose analytics** computed over a chosen window from the glucose
   store, written to `metrics.json`, and **routed through `integrity`** for validation,
   freshness and last-known-good caching. **`narration.py`** turns those metrics into
   patient-facing text and routes every sentence through the narration guard.

Design principles: **fail closed**, never guess (a timezone or a unit), **single source of
truth** for ranges/thresholds, **immutable verdicts**, never mutate caller input, and
explicit float/NaN/inf discipline.

## `integrity.py` public API

| Function | Purpose |
|---|---|
| `validate_readings(df, *, raise_on_error=True, drop_duplicates=True)` | Validate a tidy readings frame (`subject, metric, value, measured_at`) against the physiological range table. Returns a `ValidationReport` (clean frame + every per-row `RowError`); raises `ValidationError` by default. Never mutates the input; handles NaN / non-numeric / ±inf / unknown-metric / bad-timestamp / duplicates with distinct `Severity`. |
| `freshness(ts, *, now=None, assume_tz=None, fresh_within=6h, stale_within=72h, future_skew=5m)` | Signed age + `FreshnessState` (`FRESH` / `STALE` / `EXPIRED` / `FUTURE`). Fails closed on naive timestamps unless `assume_tz` is given. |
| `cache_set / cache_get / cache_get_value / cache_clear` | Thread-safe (RLock) **last-known-good** cache storing *value + measurement time*. Monotonic by measurement (older/equal writes refused), future writes refused, read-time freshness gate with self-eviction of `EXPIRED`. `cache_get` raises `CacheMiss` (never returns `None`). |
| `sanitize_narration(text, allowed_numbers, *, redact=False, allow_dates=False, abs_tolerance=0.0)` | Guarantees every numeric token in `text` is grounded in `allowed_numbers` within the token's own display-rounding band; raises `NarrationIntegrityError` on the first ungrounded number (or redacts). Blocks invented numbers — including those glued to unit suffixes (`999mg`). |
| `assert_validated(df)` / `now_utc()` | Runtime tripwire that data crossed the boundary; the one sanctioned wall clock. |
| `RANGES` | Read-only view of the single-source-of-truth range table. |

### The rounding rule (`sanitize_narration`)

A token is accepted iff it lies within `±(0.5 · 10^(−d) + abs_tolerance)` of some allowed
value, where `d` is the **token's own** displayed decimal count. Keying the tolerance to the
*token* (not the allowed value) blocks fabricated extra digits: with grounded `98.6`, the
token `98.6` passes but `98.64` is blocked (band ±0.005). Clock times (`14:30`) are skipped;
dotted versions (`v1.2.3`) yield no tokens; everything else, including unit-suffixed numbers,
is challenged (fail-closed).

## `clinical.py` metrics (→ `metrics.json`, each as `{value, source, ts, valid}`)

GMI, mean / SD (ddof=1) / CV% (+ flag), **TIR** (70–180) and **TITR** (70–140, the tight
metric), TBR/VLOW/LOW, TAR/VHIGH/HIGH, **MAGE** (>1 SD excursions), **GRI** (+ hypo/hyper
components), **dawn delta** (Asia/Dubai), **AGP** 10/25/50/75/90 modal-day bands over 96
fifteen-minute slots, **daily metabolic score** (transparent weighted blend), and spike count.

Category bounds form a clean partition (sum to 100%): `VLOW<54 ≤ LOW<70 ≤ TIR≤180 < HIGH≤250 < VHIGH`.

The **daily metabolic score** is reproducible, not a black box:
`0.40·TITR + 0.25·CV_score + 0.20·spike_score + 0.15·walk_score`, each component mapped to a
documented 0–100 goodness (see `clinical.metabolic_score`).

### Clinical citations

- **GMI** — Bergenstal RM et al., *Diabetes Care* 2018;41:2275–2280.
- **TIR/TITR/TBR/TAR cutpoints** — Battelino T et al., *Diabetes Care* 2019;42:1593–1603.
- **MAGE** — Service FJ et al., *Diabetes* 1970;19:644–655 (definition); Baghurst PA,
  *Diabetes Technol Ther* 2011;13:296–302 (reproducible turning-point algorithm, incl. plateau handling).
- **GRI** — Klonoff DC et al., *J Diabetes Sci Technol* 2022 (constants verified:
  `3.0·Hypo + 1.6·Hyper ≡ 3.0·VLow + 2.4·Low + 1.6·VHigh + 0.8·High`).

## Enforcement: "every other module routes through integrity"

`tests/test_architecture.py` statically scans every app module and fails if it reads the
clock directly (`.now`/`.utcnow`), coerces values itself (`pandas.to_numeric`), re-defines
the range/tokenizer primitives, or imports the private `_rules`/`dateutil` or private
integrity symbols. The public surface (`integrity.__all__`) is frozen and asserted, plus a
runtime tripwire confirms the guards are actually exercised.

## Install & build

```bash
pip install -r requirements.txt      # pinned runtime + test deps (reproducible)
# or, as a package:
pip install -e ".[dev]"              # editable install + dev extras
python -m build                      # -> dist/healthhub-0.1.0-{.whl,.tar.gz}
```

## Run (CLI)

The `healthhub-metrics` console script reads a glucose CSV
(`subject, measured_at, glucose_mgdl`; `measured_at` must be timezone-aware) and writes
`metrics.json`:

```bash
python examples/generate_sample.py                 # writes examples/glucose_sample.csv (3 days, 15-min)
healthhub-metrics examples/glucose_sample.csv -o metrics.json --walk-adherence 0.8
# options: --subject, --window-start/--window-end (ISO), --source-label, --quiet
```

`make help` lists the convenience targets (`install`, `test`, `build`, `install-wheel`, `example`, `clean`).

## Develop / test

```bash
make test        # pytest: 126 tests incl. the architecture enforcement test
```

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.11/3.12 and builds + smoke-tests the wheel.

## Layout

```
integrity.py   _rules.py (private primitives)   clinical.py   narration.py   cli.py
pyproject.toml   requirements.txt   pytest.ini   Makefile
examples/  generate_sample.py   glucose_sample.csv
tests/  test_sanitize.py test_freshness.py test_validate.py test_cache.py
        test_clinical.py test_narration.py test_architecture.py test_requirements.py
.github/workflows/ci.yml
```
