#!/usr/bin/env python3
"""Turn one day's raw Alpha Vantage files into data/latest.json and data/history/<date>.json.

This script measures: price indicators, fundamentals, earnings surprises and news sentiment.
engine/score.js judges: layer scores, composite, rating, zone and flags. No AI is involved.

Usage:
    python pipeline/compute.py                   # newest day in data/raw/
    python pipeline/compute.py --date 2026-10-19
    python pipeline/compute.py --allow-partial   # score the tickers that fetched fine, even if one failed
"""
import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CONFIG = ROOT / "config" / "scoring.json"
ENGINE = ROOT / "engine" / "score.js"
SCHEMA_VERSION = 1
FUNCTIONS = ["TIME_SERIES_DAILY", "OVERVIEW", "EARNINGS", "NEWS_SENTIMENT"]
REQUIRED = ["TIME_SERIES_DAILY", "OVERVIEW"]
NEWS_WINDOW_DAYS = 7
SURPRISE_CLIP_PCT = 50  # an EPS estimate near zero turns a small miss into +-1000%


class ComputeError(Exception):
    pass


def write_json(path, obj):
    """Write atomically so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def num(value):
    """Alpha Vantage sends numbers as strings, and "None", "-" or "" when missing."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def rounded(value, digits=2):
    return None if value is None else round(value, digits)


def pct(a, b):
    """Percent change from b to a."""
    if a is None or not b:
        return None
    return (a / b - 1) * 100


# --- Price indicators -------------------------------------------------------

def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def ema_series(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    """Wilder's RSI."""
    if len(closes) <= n:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(c, 0) for c in changes[:n]) / n
    loss = sum(max(-c, 0) for c in changes[:n]) / n
    for c in changes[n:]:
        gain = (gain * (n - 1) + max(c, 0)) / n
        loss = (loss * (n - 1) + max(-c, 0)) / n
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def atr(highs, lows, closes, n=14):
    """Wilder's average true range."""
    if len(closes) <= n:
        return None
    true_ranges = [max(h - l, abs(h - pc), abs(l - pc)) for h, l, pc in zip(highs[1:], lows[1:], closes)]
    value = sum(true_ranges[:n]) / n
    for tr in true_ranges[n:]:
        value = (value * (n - 1) + tr) / n
    return value


def macd_hist(closes):
    """MACD(12, 26) minus its 9-day signal line."""
    if len(closes) < 35:
        return None
    macd = [a - b for a, b in zip(ema_series(closes, 12), ema_series(closes, 26))]
    return macd[-1] - ema_series(macd, 9)[-1]


def volatility(closes, n=20):
    """Annualized standard deviation of the last n daily log returns, in percent."""
    if len(closes) <= n:
        return None
    window = closes[-n - 1:]
    returns = [math.log(b / a) for a, b in zip(window, window[1:])]
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(variance) * math.sqrt(252) * 100


def price_features(payload):
    series = payload["Time Series (Daily)"]
    days = sorted(series)
    closes = [float(series[d]["4. close"]) for d in days]
    highs = [float(series[d]["2. high"]) for d in days]
    lows = [float(series[d]["3. low"]) for d in days]
    price = closes[-1]
    sma50, sma100 = sma(closes, 50), sma(closes, 100)
    atr14 = atr(highs, lows, closes)
    hist = macd_hist(closes)
    return {
        "price": round(price, 2),
        "change_1d_pct": rounded(pct(price, closes[-2]) if len(closes) > 1 else None),
        "trend": {
            "price_vs_sma50_pct": rounded(pct(price, sma50)),
            "sma50_slope_20d_pct": rounded(pct(sma50, sma(closes[:-20], 50))),
            "price_vs_sma100_pct": rounded(pct(price, sma100)),
        },
        "momentum": {
            "rsi14": rounded(rsi(closes)),
            "return_20d_pct": rounded(pct(price, closes[-21]) if len(closes) > 20 else None),
            "macd_hist_pct": rounded(hist / price * 100 if hist is not None else None, 3),
        },
        "risk": {
            "atr14_pct": rounded(atr14 / price * 100 if atr14 is not None else None),
            "volatility_20d_pct": rounded(volatility(closes)),
            "drawdown_from_high_pct": rounded(pct(price, max(closes[-100:]))),
        },
        "zone_inputs": {"price": price, "sma50": sma50, "sma100": sma100, "atr14": atr14},
    }


# --- Fundamentals, earnings, news ------------------------------------------

def overview_features(payload):
    return {
        "name": payload.get("Name"),
        "valuation": {k: num(payload.get(k)) for k in ("ForwardPE", "PEGRatio", "EVToEBITDA")},
        "growth_quality": {k: num(payload.get(k)) for k in (
            "QuarterlyRevenueGrowthYOY", "QuarterlyEarningsGrowthYOY", "ProfitMargin", "ReturnOnEquityTTM")},
    }


def avg_surprise(payload, market_date, quarters=4):
    """Average EPS surprise % of the last `quarters` reported quarters, each clipped to +-SURPRISE_CLIP_PCT."""
    reported = []
    for q in payload.get("quarterlyEarnings", []):
        when = q.get("reportedDate") or q.get("fiscalDateEnding") or ""
        surprise = num(q.get("surprisePercentage"))
        if surprise is not None and when <= market_date.isoformat():
            reported.append((when, max(-SURPRISE_CLIP_PCT, min(SURPRISE_CLIP_PCT, surprise))))
    latest = [s for _, s in sorted(reported, reverse=True)[:quarters]]
    return round(sum(latest) / len(latest), 2) if latest else None


def news_features(payload, ticker, market_date):
    """Relevance-weighted average sentiment for this ticker over the last NEWS_WINDOW_DAYS days."""
    start = market_date - dt.timedelta(days=NEWS_WINDOW_DAYS - 1)
    weight_sum = weighted = 0.0
    count = 0
    for item in payload.get("feed", []):
        try:
            day = dt.datetime.strptime(item["time_published"][:8], "%Y%m%d").date()
        except (KeyError, TypeError, ValueError):
            continue
        if not start <= day <= market_date:
            continue
        for ts in item.get("ticker_sentiment", []):
            relevance, score = num(ts.get("relevance_score")), num(ts.get("ticker_sentiment_score"))
            if ts.get("ticker") == ticker and relevance is not None and score is not None:
                count += 1
                weight_sum += relevance
                weighted += relevance * score
    return {
        "weighted_sentiment_7d": round(weighted / weight_sum, 3) if weight_sum else None,
        "article_count_7d": count,
    }


# --- Pipeline ----------------------------------------------------------------

def pick_day(date_arg):
    if date_arg:
        day_dir = RAW_DIR / date_arg
    else:
        days = sorted(p for p in RAW_DIR.glob("*") if (p / "manifest.json").exists()) if RAW_DIR.exists() else []
        if not days:
            raise ComputeError(f"no fetched days in {RAW_DIR.relative_to(ROOT)}; run pipeline/fetch.py first")
        day_dir = days[-1]
    if not (day_dir / "manifest.json").exists():
        raise ComputeError(f"no manifest.json in {day_dir.relative_to(ROOT)}; run pipeline/fetch.py first")
    return day_dir


def load_previous(market_date):
    """The newest history file from before market_date, or None."""
    history = DATA_DIR / "history"
    older = sorted(p for p in history.glob("*.json") if p.stem < market_date.isoformat()) if history.exists() else []
    return json.loads(older[-1].read_text()) if older else None


def run_engine(engine_input):
    try:
        proc = subprocess.run(["node", str(ENGINE)], input=json.dumps(engine_input),
                              capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ComputeError("node is not installed; it is needed to run engine/score.js")
    if proc.returncode != 0:
        raise ComputeError(proc.stderr.strip() or "score.js failed")
    return json.loads(proc.stdout)


def compute(day_dir, allow_partial=False):
    manifest = json.loads((day_dir / "manifest.json").read_text())
    if not manifest.get("ok") and not allow_partial:
        raise ComputeError("fetch had a required failure (manifest ok=false); fix it or use --allow-partial")
    config = json.loads(CONFIG.read_text())
    market_date = dt.date.fromisoformat(manifest["market_date"])
    previous = load_previous(market_date)
    previous_tickers = previous["tickers"] if previous else {}

    engine_input = {"config": config, "tickers": {}, "previous": previous_tickers}
    meta, failed = {}, {}
    for ticker in manifest["tickers"]:
        files = {}
        for function in FUNCTIONS:
            path = day_dir / ticker / f"{function}.json"
            files[function] = json.loads(path.read_text()) if path.exists() else None
        missing = [f for f in REQUIRED if files[f] is None]
        if missing:
            errors = [f"{r['function']}: {r['error']}" for r in manifest["results"]
                      if r["ticker"] == ticker and r.get("error")]
            failed[ticker] = "; ".join(errors) or f"missing {', '.join(missing)}"
            continue

        prices = price_features(files["TIME_SERIES_DAILY"]["data"])
        overview = overview_features(files["OVERVIEW"]["data"])
        earnings = files["EARNINGS"]
        news = files["NEWS_SENTIMENT"]
        engine_input["tickers"][ticker] = {
            "inputs": {
                "trend": prices["trend"],
                "momentum": prices["momentum"],
                "risk": prices["risk"],
                "valuation": overview["valuation"],
                "growth_quality": {
                    **overview["growth_quality"],
                    "avg_surprise_pct_4q": avg_surprise(earnings["data"], market_date) if earnings else None,
                },
                "sentiment": news_features(news["data"], ticker, market_date) if news
                else {"weighted_sentiment_7d": None, "article_count_7d": None},
            },
            "zone_inputs": prices["zone_inputs"],
        }
        meta[ticker] = {
            "name": overview["name"],
            "price": prices["price"],
            "change_1d_pct": prices["change_1d_pct"],
            "data_as_of": {f: rec["fetched_at"] for f, rec in files.items() if rec},
        }

    scored = run_engine(engine_input)
    tickers = {}
    for ticker in manifest["tickers"]:
        if ticker in failed:
            tickers[ticker] = {"status": "not_scored", "reason": failed[ticker], "missing_layers": list(config["layers"])}
            continue
        s = scored["tickers"][ticker]
        before = previous_tickers.get(ticker, {}).get("composite")
        entry = {"status": s["status"]}
        if "reason" in s:
            entry["reason"] = s["reason"]
        entry.update({
            "name": meta[ticker]["name"],
            "price": meta[ticker]["price"],
            "change_1d_pct": meta[ticker]["change_1d_pct"],
            "composite": s["composite"],
            "composite_change_1d": round(s["composite"] - before, 1)
            if s["composite"] is not None and before is not None else None,
            "rating": s["rating"],
            "layers": s["layers"],
            "zone": s["zone"],
            "missing_layers": s["missing_layers"],
            "flags": s["flags"],
            "data_as_of": meta[ticker]["data_as_of"],
        })
        tickers[ticker] = entry

    return {
        "schema_version": SCHEMA_VERSION,
        "market_date": market_date.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine_version": scored["engine_version"],
        "scoring_config_version": config["version"],
        "watchlist": manifest["tickers"],
        "tickers": tickers,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="market date YYYY-MM-DD (default: newest fetched day)")
    ap.add_argument("--allow-partial", action="store_true", help="continue when fetch had a required failure")
    args = ap.parse_args(argv)

    try:
        latest = compute(pick_day(args.date), args.allow_partial)
    except ComputeError as exc:
        print(f"compute.py: {exc}", file=sys.stderr)
        return 1

    write_json(DATA_DIR / "latest.json", latest)
    write_json(DATA_DIR / "history" / f"{latest['market_date']}.json", latest)

    for ticker, t in latest["tickers"].items():
        if t["status"] == "scored":
            zone = t["zone"]["current"] if t["zone"] else "-"
            flags = " ".join(t["flags"])
            print(f"{ticker:6} {t['composite']:5.1f}  {t['rating']:8} {zone:10} {flags}")
        else:
            print(f"{ticker:6} not scored: {t['reason']}")
    print(f"\nWrote data/latest.json and data/history/{latest['market_date']}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
