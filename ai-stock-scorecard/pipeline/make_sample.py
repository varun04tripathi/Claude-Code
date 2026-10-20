#!/usr/bin/env python3
"""Make up 60 trading days of sample data, for working on the page without using the API.

Runs the real compute.py on invented prices, fundamentals and news, writing everything to
sample/data/ (ignored by git). Nothing produced here is real market data.

Usage:
    python pipeline/make_sample.py
    python pipeline/build.py --data sample/data --sample
"""
import argparse
import datetime as dt
import importlib.util
import json
import random
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample" / "data"
BARS = 100

spec = importlib.util.spec_from_file_location("compute", ROOT / "pipeline" / "compute.py")
compute = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compute)


def business_days(end, count):
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


def price_path(rng, days, drift, vol):
    bars, close = {}, rng.uniform(40, 400)
    for day in days:
        open_ = close
        close = max(1.0, close * (1 + drift + rng.gauss(0, vol)))
        high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 2)))
        bars[day.isoformat()] = {"1. open": f"{open_:.4f}", "2. high": f"{high:.4f}", "3. low": f"{low:.4f}",
                                 "4. close": f"{close:.4f}", "5. volume": str(rng.randint(10**6, 10**8))}
    return bars


def profile(rng, ticker):
    return {
        "Symbol": ticker, "Name": f"{ticker} (sample company)",
        "ForwardPE": f"{rng.uniform(14, 70):.1f}", "PEGRatio": f"{rng.uniform(0.6, 3.2):.2f}",
        "EVToEBITDA": f"{rng.uniform(8, 55):.1f}",
        "QuarterlyRevenueGrowthYOY": f"{rng.uniform(-0.08, 0.6):.3f}",
        "QuarterlyEarningsGrowthYOY": f"{rng.uniform(-0.3, 0.9):.3f}",
        "ProfitMargin": f"{rng.uniform(0.02, 0.5):.3f}", "ReturnOnEquityTTM": f"{rng.uniform(0.03, 0.8):.3f}",
    }


def earnings(rng, market_date):
    rows = []
    for i in range(6):
        reported = market_date - dt.timedelta(days=25 + 91 * i)
        rows.append({"fiscalDateEnding": reported.isoformat(), "reportedDate": reported.isoformat(),
                     "surprisePercentage": f"{rng.gauss(3, 8):.2f}"})
    return {"quarterlyEarnings": rows}


def news(rng, ticker, market_date, mood, articles):
    feed = []
    for _ in range(articles):
        day = market_date - dt.timedelta(days=rng.randint(0, 6))
        feed.append({"time_published": day.strftime("%Y%m%d") + "T120000", "ticker_sentiment": [{
            "ticker": ticker, "relevance_score": f"{rng.uniform(0.2, 1):.3f}",
            "ticker_sentiment_score": f"{max(-1, min(1, rng.gauss(mood, 0.15))):.3f}"}]})
    return {"feed": feed}


def write_record(path, ticker, function, data, fetched_at):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ticker": ticker, "function": function, "params": {}, "source": "sample",
                                "fetched_at": fetched_at, "data": data}))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    tickers = json.loads((ROOT / "watchlist.json").read_text())
    today = dt.date.today()
    end = today - dt.timedelta(days=1)
    all_days = business_days(end, BARS + args.days)
    run_days = all_days[-args.days:]

    if OUT.exists():
        shutil.rmtree(OUT)
    compute.DATA_DIR, compute.RAW_DIR = OUT, OUT / "raw"

    rngs = {t: random.Random(f"{args.seed}-{t}") for t in tickers}
    paths = {t: price_path(rngs[t], all_days, drift=rngs[t].uniform(-0.002, 0.003), vol=rngs[t].uniform(0.012, 0.035))
             for t in tickers}
    profiles = {t: profile(rngs[t], t) for t in tickers}
    moods = {t: rngs[t].uniform(-0.25, 0.3) for t in tickers}

    for market_date in run_days:
        day_dir = OUT / "raw" / market_date.isoformat()
        fetched_at = f"{market_date.isoformat()}T20:47:00+00:00"
        for n, t in enumerate(tickers):
            rng = rngs[t]
            window = {d: paths[t][d] for d in sorted(paths[t]) if d <= market_date.isoformat()}
            window = dict(list(window.items())[-BARS:])
            moods[t] = max(-0.5, min(0.5, moods[t] + rng.gauss(0, 0.04)))
            write_record(day_dir / t / "TIME_SERIES_DAILY.json", t, "TIME_SERIES_DAILY",
                         {"Meta Data": {}, "Time Series (Daily)": window}, fetched_at)
            write_record(day_dir / t / "OVERVIEW.json", t, "OVERVIEW", profiles[t], fetched_at)
            write_record(day_dir / t / "EARNINGS.json", t, "EARNINGS", earnings(rng, market_date), fetched_at)
            articles = rng.randint(0, 3) if n == len(tickers) - 1 else rng.randint(4, 25)  # last ticker: thin news
            write_record(day_dir / t / "NEWS_SENTIMENT.json", t, "NEWS_SENTIMENT",
                         news(rng, t, market_date, moods[t], articles), fetched_at)
        (day_dir / "manifest.json").write_text(json.dumps({
            "market_date": market_date.isoformat(), "tickers": tickers, "results": [], "ok": True}))
        latest = compute.compute(day_dir)
        compute.write_json(OUT / "latest.json", latest)
        compute.write_json(OUT / "history" / f"{market_date.isoformat()}.json", latest)

    print(f"Wrote {len(run_days)} sample days ({run_days[0]} to {run_days[-1]}) to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
