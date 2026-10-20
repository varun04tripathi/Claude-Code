#!/usr/bin/env python3
"""Fetch raw Alpha Vantage data for every ticker in watchlist.json.

Each response is saved unchanged, wrapped with a timestamp, at
data/raw/<YYYY-MM-DD>/<TICKER>/<FUNCTION>.json, and data/raw/<date>/manifest.json
records what succeeded. No calculations happen here; compute.py reads these files.

Usage:
    python pipeline/fetch.py                  # every ticker, today's market date
    python pipeline/fetch.py --tickers NVDA   # one ticker
    python pipeline/fetch.py --force          # ignore today's files and the cache
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
WATCHLIST = ROOT / "watchlist.json"
API_URL = "https://www.alphavantage.co/query"
MARKET_TZ = ZoneInfo("America/New_York")
PAUSE_SECONDS = float(os.environ.get("AV_PAUSE_SECONDS", "12"))  # stay under the per-minute limit

# max_age_days: 0 = fetch fresh every run; N = reuse a saved copy up to N days old.
# required: a failure here stops the pipeline; optional endpoints only warn.
ENDPOINTS = [
    {"function": "TIME_SERIES_DAILY", "params": {"outputsize": "compact"}, "max_age_days": 0, "required": True},
    {"function": "OVERVIEW",          "params": {},                        "max_age_days": 7, "required": True},
    {"function": "EARNINGS",          "params": {},                        "max_age_days": 7, "required": False},
    {"function": "NEWS_SENTIMENT",    "params": {"limit": "50"},           "max_age_days": 0, "required": False},
]
MIN_BARS = 100          # "compact" returns the latest 100 daily bars
MAX_BAR_AGE_DAYS = 5    # covers weekends and a holiday


class FetchError(Exception):
    pass


class QuotaExhausted(FetchError):
    """The daily quota is spent: every further call today will fail too."""


def now_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_json(path, obj):
    """Write atomically so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def load_tickers(cli_tickers):
    tickers = cli_tickers or json.loads(WATCHLIST.read_text())
    tickers = [t.strip().upper() for t in tickers]
    bad = [t for t in tickers if not re.fullmatch(r"[A-Z0-9.\-]{1,10}", t)]
    if bad:
        raise SystemExit(f"Invalid ticker(s): {bad}")
    return tickers


def get_json(params, api_key, retries=4):
    """Call the API. Alpha Vantage returns HTTP 200 even for errors, so inspect the body."""
    url = f"{API_URL}?{urllib.parse.urlencode({**params, 'apikey': api_key})}"
    delay = 2
    problem = "unknown"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                payload = json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            problem = f"network/parse error ({exc.__class__.__name__})"  # never echo the URL: it has the key
        else:
            if "Error Message" in payload:
                raise FetchError(f"API error: {payload['Error Message'][:150]}")
            note = payload.get("Note") or payload.get("Information")
            if not note:
                return payload
            if "day" in note.lower():
                raise QuotaExhausted("daily API quota used up; try again tomorrow")
            problem = "rate limited (per-minute)"
            delay = max(delay, 60)
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise FetchError(f"gave up after {retries} attempts: {problem}")


def check(function, payload, market_date):
    """Return a problem description, or None if the payload looks usable."""
    if function == "TIME_SERIES_DAILY":
        series = payload.get("Time Series (Daily)")
        if not series:
            return "missing 'Time Series (Daily)'"
        newest = max(series)
        age = (market_date - dt.date.fromisoformat(newest)).days
        if age > MAX_BAR_AGE_DAYS:
            return f"newest bar {newest} is {age} days old"
        if len(series) < MIN_BARS:
            return f"only {len(series)} bars (need {MIN_BARS})"
    elif function == "OVERVIEW" and not payload.get("Symbol"):
        return "empty overview (unknown symbol?)"
    elif function == "EARNINGS" and "quarterlyEarnings" not in payload:
        return "missing 'quarterlyEarnings'"
    elif function == "NEWS_SENTIMENT" and "feed" not in payload:
        return "missing 'feed'"
    return None


def find_cached(ticker, function, market_date, max_age_days):
    """Most recent earlier saved copy that is still fresh enough, or None."""
    if max_age_days <= 0 or not RAW_DIR.exists():
        return None
    for day_dir in sorted(RAW_DIR.iterdir(), reverse=True):
        try:
            day = dt.date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if day >= market_date or (market_date - day).days > max_age_days:
            continue
        saved = day_dir / ticker / f"{function}.json"
        if saved.exists():
            return json.loads(saved.read_text())
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+", help="override watchlist.json")
    ap.add_argument("--date", help="market date YYYY-MM-DD (default: today in New York)")
    ap.add_argument("--force", action="store_true", help="refetch even if saved or cached")
    args = ap.parse_args(argv)

    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        raise SystemExit("ALPHAVANTAGE_API_KEY is not set")

    market_date = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(MARKET_TZ).date()
    day_dir = RAW_DIR / market_date.isoformat()
    tickers = load_tickers(args.tickers)

    manifest = {"market_date": market_date.isoformat(), "started_at": now_utc(), "tickers": tickers, "results": []}
    api_calls = 0
    required_failed = False
    quota_spent = False

    for ticker in tickers:
        for ep in ENDPOINTS:
            function = ep["function"]
            out = day_dir / ticker / f"{function}.json"
            entry = {"ticker": ticker, "function": function, "required": ep["required"]}
            try:
                cached = None if args.force else find_cached(ticker, function, market_date, ep["max_age_days"])
                if out.exists() and not args.force:
                    entry["status"] = "already saved today"
                elif cached:
                    write_json(out, {**cached, "reused_on": market_date.isoformat()})
                    entry["status"] = f"reused copy fetched {cached['fetched_at']}"
                elif quota_spent:
                    raise FetchError("skipped: daily API quota already used up")
                else:
                    if api_calls:
                        time.sleep(PAUSE_SECONDS)
                    symbol_key = "tickers" if function == "NEWS_SENTIMENT" else "symbol"
                    params = {"function": function, symbol_key: ticker, **ep["params"]}
                    api_calls += 1
                    payload = get_json(params, api_key)
                    problem = check(function, payload, market_date)
                    if problem:
                        raise FetchError(problem)
                    write_json(out, {
                        "ticker": ticker,
                        "function": function,
                        "params": params,            # no API key stored
                        "source": API_URL,
                        "fetched_at": now_utc(),
                        "data": payload,             # the response, unchanged
                    })
                    entry["status"] = "fetched"
            except FetchError as exc:
                quota_spent |= isinstance(exc, QuotaExhausted)
                entry["status"] = "FAILED"
                entry["error"] = str(exc)
                required_failed |= ep["required"]
            manifest["results"].append(entry)
            print(f"{ticker:6} {function:18} {entry['status']}" + (f" - {entry['error']}" if "error" in entry else ""), flush=True)

    manifest.update(finished_at=now_utc(), api_calls=api_calls, ok=not required_failed)
    write_json(day_dir / "manifest.json", manifest)
    print(f"\n{api_calls} API calls. Manifest: {(day_dir / 'manifest.json').relative_to(ROOT)}")
    if required_failed:
        print("A required endpoint failed - stop the pipeline, do not publish.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
