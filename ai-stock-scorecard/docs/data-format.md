# Data format

This file defines the shape of every generated data file. Scripts must write exactly
these fields; the dashboard reads exactly these fields. If a format changes, update
this file and bump `schema_version` in the same commit.

Example numbers below are illustrative only — not real prices or scores.

## Rules

- Files under `data/` are written only by pipeline scripts, never by hand.
- All timestamps are UTC, ISO 8601 (e.g. `2026-10-19T20:52:11Z`).
- All dates are the US market date, `YYYY-MM-DD`.
- Percentages ending in `_pct` are in percent (`4.2` = 4.2%).
- Fields copied from Alpha Vantage keep their original names and units
  (e.g. `ProfitMargin: 0.50` = 50%).
- A missing value is `null`, never `0`.

## 1. `data/latest.json` — today's scores

Written by `pipeline/compute.py`. Read by `pipeline/build.py`.

### Top level

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | number | Version of this format. Currently `1`. |
| `market_date` | date | Trading day these scores are for. |
| `generated_at` | timestamp | When compute.py ran. |
| `engine_version` | string | Version of `engine/score.js`. |
| `scoring_config_version` | string | `version` from `config/scoring.json`. |
| `watchlist` | string[] | Tickers in watchlist.json for this run. |
| `tickers` | object | One entry per ticker, keyed by symbol. |

### Each ticker (`tickers.<SYMBOL>`)

| Field | Type | Meaning |
|---|---|---|
| `status` | `"scored"` or `"not_scored"` | Whether a score was produced. |
| `reason` | string | Only when `not_scored`: why. |
| `name` | string | Company name (from OVERVIEW). |
| `price` | number | Latest close. |
| `change_1d_pct` | number | Close vs previous close. |
| `composite` | number 0–100 | Weighted average of layer scores. |
| `composite_change_1d` | number or null | Points vs previous day; null if no previous day. |
| `rating` | string | Label from `ratings` in config/scoring.json. |
| `layers` | object | Six layers, each `{ score, inputs }` (below). |
| `zone` | object | Price zones (below). |
| `missing_layers` | string[] | Layers skipped because data was missing. |
| `flags` | string[] | Alerts, e.g. `composite_jump`, `layer_jump`. |
| `data_as_of` | object | `fetched_at` of each raw source used. |

### Layers (`layers.<name>`)

Each layer is `{ "score": 0–100, "inputs": { ... } }`.

| Layer | Source | Inputs |
|---|---|---|
| `trend` | TIME_SERIES_DAILY | `price_vs_sma50_pct`, `sma50_slope_20d_pct`, `price_vs_sma100_pct` |
| `momentum` | TIME_SERIES_DAILY | `rsi14`, `return_20d_pct`, `macd_hist` |
| `risk` | TIME_SERIES_DAILY | `atr14_pct`, `volatility_20d_pct`, `drawdown_from_high_pct` (higher score = lower risk) |
| `valuation` | OVERVIEW | `ForwardPE`, `PEGRatio`, `EVToEBITDA` |
| `growth_quality` | OVERVIEW + EARNINGS | `QuarterlyRevenueGrowthYOY`, `QuarterlyEarningsGrowthYOY`, `ProfitMargin`, `ReturnOnEquityTTM`, `avg_surprise_pct_4q` |
| `sentiment` | NEWS_SENTIMENT | `weighted_sentiment_7d`, `article_count_7d` |

### Zone (`zone`)

| Field | Meaning |
|---|---|
| `current` | One of `accumulate`, `hold`, `extended`, `breakdown`. |
| `sma50`, `sma100` | 50- and 100-day simple moving averages. |
| `atr14` | 14-day average true range. |
| `accumulate_low` / `accumulate_high` | sma50 − 1×ATR to sma50. |
| `extended_above` | sma50 + 2×ATR. |
| `breakdown_below` | sma100. |

### Example

```json
{
  "schema_version": 1,
  "market_date": "2026-10-19",
  "generated_at": "2026-10-19T20:52:11Z",
  "engine_version": "1.0.0",
  "scoring_config_version": "1.0.0",
  "watchlist": ["NVDA", "TSLA"],
  "tickers": {
    "NVDA": {
      "status": "scored",
      "name": "NVIDIA Corp",
      "price": 100.00,
      "change_1d_pct": 1.25,
      "composite": 68.4,
      "composite_change_1d": 2.1,
      "rating": "Positive",
      "layers": {
        "trend":          { "score": 74, "inputs": { "price_vs_sma50_pct": 4.2, "sma50_slope_20d_pct": 1.8, "price_vs_sma100_pct": 9.5 } },
        "momentum":       { "score": 61, "inputs": { "rsi14": 58.3, "return_20d_pct": 5.1, "macd_hist": 0.42 } },
        "risk":           { "score": 45, "inputs": { "atr14_pct": 3.1, "volatility_20d_pct": 42.0, "drawdown_from_high_pct": -6.4 } },
        "valuation":      { "score": 52, "inputs": { "ForwardPE": 30.0, "PEGRatio": 1.2, "EVToEBITDA": 25.0 } },
        "growth_quality": { "score": 88, "inputs": { "QuarterlyRevenueGrowthYOY": 0.60, "QuarterlyEarningsGrowthYOY": 0.55, "ProfitMargin": 0.50, "ReturnOnEquityTTM": 0.90, "avg_surprise_pct_4q": 5.0 } },
        "sentiment":      { "score": 70, "inputs": { "weighted_sentiment_7d": 0.22, "article_count_7d": 48 } }
      },
      "zone": {
        "current": "hold",
        "sma50": 96.00,
        "sma100": 91.30,
        "atr14": 3.10,
        "accumulate_low": 92.90,
        "accumulate_high": 96.00,
        "extended_above": 102.20,
        "breakdown_below": 91.30
      },
      "missing_layers": [],
      "flags": [],
      "data_as_of": {
        "TIME_SERIES_DAILY": "2026-10-19T20:47:03Z",
        "OVERVIEW": "2026-10-14T20:47:20Z",
        "EARNINGS": "2026-10-14T20:47:35Z",
        "NEWS_SENTIMENT": "2026-10-19T20:47:50Z"
      }
    },
    "TSLA": {
      "status": "not_scored",
      "reason": "OVERVIEW fetch failed: empty overview",
      "missing_layers": ["valuation", "growth_quality"]
    }
  }
}
```

## 2. `data/history/YYYY-MM-DD.json` — one file per trading day

An exact copy of that day's `latest.json`. Never edited after the day it was written.
`build.py` reads the last 90 files for the trend chart; `evaluate.py` reads all of them.

## 3. `data/raw/YYYY-MM-DD/<TICKER>/<FUNCTION>.json` — raw API data

Written by `pipeline/fetch.py`. The Alpha Vantage response is stored unchanged in `data`.

| Field | Meaning |
|---|---|
| `ticker`, `function` | What was requested. |
| `params` | Request parameters (never the API key). |
| `source` | API URL. |
| `fetched_at` | When it was downloaded. |
| `data` | The unchanged API response. |
| `reused_on` | Only on reused weekly copies: the date it was reused. |

## 4. `data/raw/YYYY-MM-DD/manifest.json` — fetch run summary

| Field | Meaning |
|---|---|
| `market_date`, `started_at`, `finished_at` | When the run happened. |
| `tickers` | Tickers fetched. |
| `results` | One entry per ticker × function: `status` = `fetched`, `already saved today`, `reused copy fetched …`, or `FAILED` (with `error`). |
| `api_calls` | Number of API calls used. |
| `ok` | `false` if any required source failed — the pipeline must stop. |
