"""Offline tests for pipeline/fetch.py — no network, no API quota used.

Run from the project root:  python -m unittest discover -s tests -v
"""
import datetime as dt
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SECRET = "TEST-KEY-must-never-be-saved"

spec = importlib.util.spec_from_file_location("fetch", ROOT / "pipeline" / "fetch.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)

MARKET_DATE = dt.date(2026, 10, 19)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def fake_response(payload):
    """Something urlopen() could return, usable as a context manager."""
    return io.BytesIO(json.dumps(payload).encode())


def daily_series(bars=100, newest=MARKET_DATE):
    series = {}
    day = newest
    while len(series) < bars:
        if day.weekday() < 5:
            series[day.isoformat()] = {"1. open": "10", "2. high": "11", "3. low": "9", "4. close": "10.5", "5. volume": "1000"}
        day -= dt.timedelta(days=1)
    return {"Meta Data": {}, "Time Series (Daily)": series}


def good_payload(params):
    function = params["function"]
    if function == "TIME_SERIES_DAILY":
        return daily_series()
    if function == "OVERVIEW":
        return {"Symbol": params["symbol"], "ForwardPE": "30"}
    if function == "EARNINGS":
        return {"quarterlyEarnings": []}
    return {"feed": []}


@mock.patch.object(fetch.time, "sleep", lambda s: None)
class GetJsonTests(unittest.TestCase):
    def call_with(self, *responses):
        side_effects = [r if isinstance(r, Exception) else fake_response(r) for r in responses]
        with mock.patch.object(fetch.urllib.request, "urlopen", side_effect=side_effects) as urlopen:
            try:
                return fetch.get_json({"function": "OVERVIEW", "symbol": "NVDA"}, SECRET), urlopen
            except fetch.FetchError as exc:
                return exc, urlopen

    def test_success_returns_payload(self):
        result, urlopen = self.call_with({"Symbol": "NVDA"})
        self.assertEqual(result, {"Symbol": "NVDA"})
        self.assertEqual(urlopen.call_count, 1)

    def test_bad_symbol_fails_without_retry(self):
        result, urlopen = self.call_with(fixture("bad_symbol.json"))
        self.assertIsInstance(result, fetch.FetchError)
        self.assertIn("API error", str(result))
        self.assertEqual(urlopen.call_count, 1)

    def test_daily_quota_fails_without_retry(self):
        result, urlopen = self.call_with(fixture("daily_quota.json"))
        self.assertIn("daily API quota", str(result))
        self.assertEqual(urlopen.call_count, 1)

    def test_per_minute_limit_retries_then_succeeds(self):
        result, urlopen = self.call_with(fixture("rate_limited.json"), {"Symbol": "NVDA"})
        self.assertEqual(result, {"Symbol": "NVDA"})
        self.assertEqual(urlopen.call_count, 2)

    def test_network_errors_give_up_without_leaking_key(self):
        err = urllib.error.URLError(f"failed for apikey={SECRET}")
        result, urlopen = self.call_with(err, err, err, err)
        self.assertIn("gave up after 4 attempts", str(result))
        self.assertNotIn(SECRET, str(result))
        self.assertEqual(urlopen.call_count, 4)


class CheckTests(unittest.TestCase):
    def test_good_daily_series(self):
        self.assertIsNone(fetch.check("TIME_SERIES_DAILY", daily_series(), MARKET_DATE))

    def test_stale_daily_series(self):
        stale = daily_series(newest=MARKET_DATE - dt.timedelta(days=10))
        self.assertIn("days old", fetch.check("TIME_SERIES_DAILY", stale, MARKET_DATE))

    def test_too_few_bars(self):
        self.assertIn("only 50 bars", fetch.check("TIME_SERIES_DAILY", daily_series(bars=50), MARKET_DATE))

    def test_empty_overview(self):
        self.assertIn("empty overview", fetch.check("OVERVIEW", fixture("empty_overview.json"), MARKET_DATE))

    def test_missing_earnings_and_news(self):
        self.assertIsNotNone(fetch.check("EARNINGS", {}, MARKET_DATE))
        self.assertIsNotNone(fetch.check("NEWS_SENTIMENT", {}, MARKET_DATE))


@mock.patch.object(fetch.time, "sleep", lambda s: None)
@mock.patch.dict(os.environ, {"ALPHAVANTAGE_API_KEY": SECRET})
class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(fetch, "RAW_DIR", Path(self.tmp.name) / "data" / "raw")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def run_main(self, date, payload_fn=good_payload, tickers=("NVDA", "MU")):
        def fake_get_json(params, api_key):
            self.calls.append(params)
            return payload_fn(params)
        with mock.patch.object(fetch, "get_json", fake_get_json), \
             mock.patch.object(fetch, "ROOT", Path(self.tmp.name)), \
             mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            code = fetch.main(["--date", date, "--tickers", *tickers])
        manifest = json.loads((fetch.RAW_DIR / date / "manifest.json").read_text())
        return code, manifest

    def test_full_run_saves_every_file_and_manifest(self):
        code, manifest = self.run_main("2026-10-19")
        self.assertEqual(code, 0)
        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["api_calls"], 8)                      # 2 tickers x 4 endpoints
        saved = json.loads((fetch.RAW_DIR / "2026-10-19" / "NVDA" / "OVERVIEW.json").read_text())
        self.assertEqual(saved["data"]["Symbol"], "NVDA")
        self.assertIn("fetched_at", saved)

    def test_api_key_never_written_to_disk(self):
        self.run_main("2026-10-19")
        for path in fetch.RAW_DIR.rglob("*.json"):
            self.assertNotIn(SECRET, path.read_text(), path)

    def test_rerun_same_day_uses_no_api_calls(self):
        self.run_main("2026-10-19")
        self.calls.clear()
        code, manifest = self.run_main("2026-10-19")
        self.assertEqual(code, 0)
        self.assertEqual(manifest["api_calls"], 0)
        self.assertTrue(all(r["status"] == "already saved today" for r in manifest["results"]))

    def test_next_day_reuses_weekly_endpoints(self):
        self.run_main("2026-10-19")
        self.calls.clear()
        code, manifest = self.run_main("2026-10-20")
        self.assertEqual(code, 0)
        self.assertEqual(sorted({c["function"] for c in self.calls}), ["NEWS_SENTIMENT", "TIME_SERIES_DAILY"])
        self.assertEqual(manifest["api_calls"], 4)                      # 2 tickers x 2 daily endpoints
        reused = json.loads((fetch.RAW_DIR / "2026-10-20" / "MU" / "OVERVIEW.json").read_text())
        self.assertEqual(reused["reused_on"], "2026-10-20")

    def test_required_failure_exits_1(self):
        def payload_fn(params):
            if params["function"] == "OVERVIEW" and params["symbol"] == "MU":
                return {}
            return good_payload(params)
        code, manifest = self.run_main("2026-10-19", payload_fn)
        self.assertEqual(code, 1)
        self.assertFalse(manifest["ok"])
        failed = [r for r in manifest["results"] if r["status"] == "FAILED"]
        self.assertEqual([(r["ticker"], r["function"]) for r in failed], [("MU", "OVERVIEW")])

    def test_daily_quota_stops_api_calls_but_still_reuses_cache(self):
        self.run_main("2026-10-19")
        self.calls.clear()
        def payload_fn(params):
            raise fetch.QuotaExhausted("daily API quota used up; try again tomorrow")
        code, manifest = self.run_main("2026-10-20", payload_fn)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls), 1)                            # no calls after the quota message
        statuses = {(r["ticker"], r["function"]): r for r in manifest["results"]}
        self.assertTrue(statuses[("MU", "OVERVIEW")]["status"].startswith("reused copy"))
        self.assertIn("skipped", statuses[("MU", "TIME_SERIES_DAILY")]["error"])

    def test_optional_failure_still_exits_0(self):
        def payload_fn(params):
            return {} if params["function"] == "NEWS_SENTIMENT" else good_payload(params)
        code, manifest = self.run_main("2026-10-19", payload_fn)
        self.assertEqual(code, 0)
        self.assertTrue(manifest["ok"])


class ConfigFileTests(unittest.TestCase):
    def test_all_json_files_parse(self):
        for path in [ROOT / "watchlist.json", ROOT / "package.json", ROOT / "config" / "scoring.json",
                     ROOT / ".claude" / "settings.json", *FIXTURES.glob("*.json")]:
            with self.subTest(path=path.name):
                json.loads(path.read_text())

    def test_watchlist_is_valid(self):
        tickers = json.loads((ROOT / "watchlist.json").read_text())
        self.assertIsInstance(tickers, list)
        self.assertLessEqual(len(tickers), 6)
        self.assertEqual(len(tickers), len(set(tickers)))
        fetch.load_tickers(tickers)                                      # raises on a bad symbol

    def test_scoring_weights_sum_to_one(self):
        config = json.loads((ROOT / "config" / "scoring.json").read_text())
        self.assertAlmostEqual(sum(l["weight"] for l in config["layers"].values()), 1.0)
        self.assertEqual(len(config["layers"]), 6)

    def test_ratings_are_descending_and_cover_zero(self):
        mins = [r["min"] for r in json.loads((ROOT / "config" / "scoring.json").read_text())["ratings"]]
        self.assertEqual(mins, sorted(mins, reverse=True))
        self.assertEqual(mins[-1], 0)


if __name__ == "__main__":
    unittest.main()
