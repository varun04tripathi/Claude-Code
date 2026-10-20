"""Offline tests for pipeline/compute.py, using made-up market data (no API calls).

Run from the project root:  python -m unittest discover -s tests -v
"""
import datetime as dt
import importlib.util
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("compute", ROOT / "pipeline" / "compute.py")
compute = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compute)

DAY1 = dt.date(2026, 10, 16)
DAY2 = dt.date(2026, 10, 19)


# --- Made-up raw data in the exact shape fetch.py saves ----------------------

def business_days(end, count):
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


def random_walk(seed, end, bars=100, start=100.0, drift=0.001, vol=0.02):
    rng = random.Random(seed)
    series, close = {}, start
    for day in business_days(end, bars):
        open_ = close
        close = max(1.0, close * (1 + drift + rng.gauss(0, vol)))
        high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 2)))
        series[day.isoformat()] = {"1. open": f"{open_:.4f}", "2. high": f"{high:.4f}", "3. low": f"{low:.4f}",
                                   "4. close": f"{close:.4f}", "5. volume": "1000000"}
    return {"Meta Data": {}, "Time Series (Daily)": series}


def overview(ticker):
    return {"Symbol": ticker, "Name": f"{ticker} Example Corp", "ForwardPE": "28.5", "PEGRatio": "1.4",
            "EVToEBITDA": "22.1", "QuarterlyRevenueGrowthYOY": "0.21", "QuarterlyEarningsGrowthYOY": "0.30",
            "ProfitMargin": "0.24", "ReturnOnEquityTTM": "0.31"}


def earnings(market_date):
    rows = [{"fiscalDateEnding": "2026-12-31", "reportedDate": "2027-01-28", "surprisePercentage": "None"}]
    for i, surprise in enumerate(["4.0", "6.0", "-2.0", "8.0", "99.0"]):
        reported = market_date - dt.timedelta(days=30 + 91 * i)
        rows.append({"fiscalDateEnding": reported.isoformat(), "reportedDate": reported.isoformat(),
                     "surprisePercentage": surprise})
    return {"quarterlyEarnings": rows}


def news(ticker, market_date, articles=6):
    feed = []
    for i in range(articles):
        day = market_date - dt.timedelta(days=i)
        feed.append({"time_published": day.strftime("%Y%m%d") + "T120000",
                     "ticker_sentiment": [{"ticker": ticker, "relevance_score": "0.8", "ticker_sentiment_score": "0.2"},
                                          {"ticker": "OTHER", "relevance_score": "0.9", "ticker_sentiment_score": "-0.9"}]})
    return {"feed": feed}


def write_raw_day(raw_dir, market_date, tickers, skip=(), ok=True):
    """Write one fetched day. `skip` = {(ticker, function)} to leave out, like a failed fetch."""
    day_dir = raw_dir / market_date.isoformat()
    results = []
    for n, ticker in enumerate(tickers):
        payloads = {
            "TIME_SERIES_DAILY": random_walk(seed=n, end=market_date),
            "OVERVIEW": overview(ticker),
            "EARNINGS": earnings(market_date),
            "NEWS_SENTIMENT": news(ticker, market_date),
        }
        for function, data in payloads.items():
            if (ticker, function) in skip:
                results.append({"ticker": ticker, "function": function, "status": "FAILED", "error": "test failure"})
                continue
            path = day_dir / ticker / f"{function}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"ticker": ticker, "function": function, "params": {},
                                        "source": "test", "fetched_at": f"{market_date}T20:47:00+00:00",
                                        "data": data}))
            results.append({"ticker": ticker, "function": function, "status": "fetched"})
    (day_dir / "manifest.json").write_text(json.dumps(
        {"market_date": market_date.isoformat(), "tickers": list(tickers), "results": results, "ok": ok}))
    return day_dir


# --- Indicator math ------------------------------------------------------------

class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(compute.sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(compute.sma([1, 2], 3))

    def test_pct(self):
        self.assertAlmostEqual(compute.pct(110, 100), 10)
        self.assertIsNone(compute.pct(None, 100))
        self.assertIsNone(compute.pct(100, 0))

    def test_rsi_extremes_and_balance(self):
        self.assertEqual(compute.rsi(list(range(1, 31))), 100.0)
        self.assertEqual(compute.rsi(list(range(30, 0, -1))), 0.0)
        self.assertEqual(compute.rsi([10.0] * 30), 50.0)
        self.assertAlmostEqual(compute.rsi([10, 11] * 20), 50, delta=5)
        self.assertIsNone(compute.rsi([1, 2, 3]))

    def test_atr_of_constant_range(self):
        closes = [100.0] * 30
        self.assertAlmostEqual(compute.atr([101.0] * 30, [99.0] * 30, closes), 2.0)

    def test_atr_includes_gaps(self):
        # Every day gaps up 5 from the previous close, with a 1-point range: true range = 5.5
        closes = [100.0 + 5 * i for i in range(30)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        self.assertAlmostEqual(compute.atr(highs, lows, closes), 5.5)

    def test_macd_flat_and_rising(self):
        self.assertAlmostEqual(compute.macd_hist([50.0] * 60), 0)
        accelerating = [100 * 1.01 ** i for i in range(60)]
        self.assertGreater(compute.macd_hist(accelerating), 0)
        self.assertIsNone(compute.macd_hist([1.0] * 10))

    def test_volatility(self):
        self.assertEqual(compute.volatility([100.0] * 30), 0)
        alternating = [100.0, 101.0] * 15
        self.assertGreater(compute.volatility(alternating), 10)

    def test_price_features_on_a_steady_uptrend(self):
        payload = {"Time Series (Daily)": {
            d.isoformat(): {"2. high": str(100 + i + 0.5), "3. low": str(100 + i - 0.5), "4. close": str(100 + i)}
            for i, d in enumerate(business_days(DAY2, 100))}}
        f = compute.price_features(payload)
        self.assertEqual(f["price"], 199)
        self.assertGreater(f["trend"]["price_vs_sma50_pct"], 0)
        self.assertGreater(f["trend"]["sma50_slope_20d_pct"], 0)
        self.assertEqual(f["momentum"]["rsi14"], 100)
        self.assertEqual(f["risk"]["drawdown_from_high_pct"], 0)
        self.assertAlmostEqual(f["momentum"]["return_20d_pct"], (199 / 179 - 1) * 100, places=2)


class FundamentalsTests(unittest.TestCase):
    def test_num_handles_alpha_vantage_placeholders(self):
        self.assertEqual(compute.num("12.5"), 12.5)
        for missing in ("None", "-", "", None, "nan", "inf"):
            self.assertIsNone(compute.num(missing), missing)

    def test_overview_features(self):
        f = compute.overview_features({**overview("NVDA"), "PEGRatio": "None"})
        self.assertEqual(f["name"], "NVDA Example Corp")
        self.assertIsNone(f["valuation"]["PEGRatio"])
        self.assertEqual(f["growth_quality"]["ProfitMargin"], 0.24)

    def test_avg_surprise_uses_last_four_reported_and_clips(self):
        # Reported quarters newest first: 4, 6, -2, 8 (the 99 is the 5th, the "None" row is in the future)
        self.assertEqual(compute.avg_surprise(earnings(DAY2), DAY2), 4.0)
        clipped = {"quarterlyEarnings": [{"reportedDate": "2026-08-01", "surprisePercentage": "900"}]}
        self.assertEqual(compute.avg_surprise(clipped, DAY2), compute.SURPRISE_CLIP_PCT)
        self.assertIsNone(compute.avg_surprise({"quarterlyEarnings": []}, DAY2))

    def test_news_counts_only_this_ticker_inside_the_window(self):
        payload = news("NVDA", DAY2, articles=10)  # 10 daily articles; only 7 fall in the window
        f = compute.news_features(payload, "NVDA", DAY2)
        self.assertEqual(f["article_count_7d"], 7)
        self.assertEqual(f["weighted_sentiment_7d"], 0.2)

    def test_news_weights_by_relevance(self):
        payload = {"feed": [
            {"time_published": "20261019T090000", "ticker_sentiment": [{"ticker": "MU", "relevance_score": "0.9", "ticker_sentiment_score": "0.5"}]},
            {"time_published": "20261019T100000", "ticker_sentiment": [{"ticker": "MU", "relevance_score": "0.1", "ticker_sentiment_score": "-0.5"}]},
        ]}
        self.assertEqual(compute.news_features(payload, "MU", DAY2)["weighted_sentiment_7d"], 0.4)


# --- Whole run, end to end -------------------------------------------------------

class EndToEndTests(unittest.TestCase):
    TICKERS = ["NVDA", "STM", "MU", "AMZN", "TSLA"]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "data"
        for name, value in [("DATA_DIR", self.data), ("RAW_DIR", self.data / "raw"), ("ROOT", Path(tmp.name))]:
            patcher = mock.patch.object(compute, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_compute(self, *args):
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()) as err:
            code = compute.main(list(args))
        self.stderr = err.getvalue()
        return code

    def latest(self):
        return json.loads((self.data / "latest.json").read_text())

    def test_full_run_matches_the_data_format(self):
        write_raw_day(self.data / "raw", DAY2, self.TICKERS)
        self.assertEqual(self.run_compute(), 0)
        latest = self.latest()
        self.assertEqual(set(latest), {"schema_version", "market_date", "generated_at", "engine_version",
                                       "scoring_config_version", "watchlist", "tickers"})
        self.assertEqual(latest["market_date"], "2026-10-19")
        self.assertEqual(latest["watchlist"], self.TICKERS)
        config = json.loads(compute.CONFIG.read_text())
        for ticker, t in latest["tickers"].items():
            with self.subTest(ticker=ticker):
                self.assertEqual(t["status"], "scored")
                self.assertEqual(set(t), {"status", "name", "price", "change_1d_pct", "composite",
                                          "composite_change_1d", "rating", "layers", "zone",
                                          "missing_layers", "flags", "data_as_of"})
                self.assertTrue(0 <= t["composite"] <= 100)
                self.assertIn(t["rating"], [r["label"] for r in config["ratings"]])
                self.assertEqual(set(t["layers"]), set(config["layers"]))
                for name, layer in t["layers"].items():
                    self.assertTrue(0 <= layer["score"] <= 100, name)
                    self.assertTrue(set(config["layers"][name]["inputs"]) <= set(layer["inputs"]), name)
                self.assertIn(t["zone"]["current"], ["breakdown", "pullback", "accumulate", "hold", "extended"])
                self.assertEqual(t["missing_layers"], [])
                self.assertIsNone(t["composite_change_1d"])         # no earlier day yet
                self.assertEqual(set(t["data_as_of"]), set(compute.FUNCTIONS))

    def test_history_file_is_an_exact_copy(self):
        write_raw_day(self.data / "raw", DAY2, self.TICKERS)
        self.run_compute()
        self.assertEqual((self.data / "history" / "2026-10-19.json").read_text(),
                         (self.data / "latest.json").read_text())

    def test_second_day_reports_change_from_previous_day(self):
        write_raw_day(self.data / "raw", DAY1, self.TICKERS)
        self.run_compute("--date", "2026-10-16")
        day1 = self.latest()
        write_raw_day(self.data / "raw", DAY2, self.TICKERS)
        self.run_compute()                                           # picks the newest day
        day2 = self.latest()
        self.assertEqual(day2["market_date"], "2026-10-19")
        for ticker in self.TICKERS:
            expected = round(day2["tickers"][ticker]["composite"] - day1["tickers"][ticker]["composite"], 1)
            self.assertEqual(day2["tickers"][ticker]["composite_change_1d"], expected)

    def test_scores_are_repeatable(self):
        write_raw_day(self.data / "raw", DAY2, self.TICKERS)
        self.run_compute()
        first = self.latest()
        self.run_compute()
        second = self.latest()
        first.pop("generated_at"), second.pop("generated_at")
        self.assertEqual(first, second)

    def test_missing_news_drops_only_the_sentiment_layer(self):
        write_raw_day(self.data / "raw", DAY2, ["NVDA"], skip={("NVDA", "NEWS_SENTIMENT")})
        self.assertEqual(self.run_compute(), 0)
        t = self.latest()["tickers"]["NVDA"]
        self.assertEqual(t["status"], "scored")
        self.assertEqual(t["missing_layers"], ["sentiment"])
        self.assertNotIn("NEWS_SENTIMENT", t["data_as_of"])

    def test_failed_fetch_stops_compute(self):
        write_raw_day(self.data / "raw", DAY2, ["NVDA", "MU"], skip={("MU", "OVERVIEW")}, ok=False)
        self.assertEqual(self.run_compute(), 1)
        self.assertIn("manifest ok=false", self.stderr)
        self.assertFalse((self.data / "latest.json").exists())

    def test_allow_partial_scores_the_rest(self):
        write_raw_day(self.data / "raw", DAY2, ["NVDA", "MU"], skip={("MU", "OVERVIEW")}, ok=False)
        self.assertEqual(self.run_compute("--allow-partial"), 0)
        tickers = self.latest()["tickers"]
        self.assertEqual(tickers["NVDA"]["status"], "scored")
        self.assertEqual(tickers["MU"]["status"], "not_scored")
        self.assertIn("OVERVIEW: test failure", tickers["MU"]["reason"])

    def test_no_fetched_data_is_a_clear_error(self):
        self.assertEqual(self.run_compute(), 1)
        self.assertIn("run pipeline/fetch.py first", self.stderr)


if __name__ == "__main__":
    unittest.main()
