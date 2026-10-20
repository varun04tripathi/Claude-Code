// Tests for engine/score.js. Run from the project root: npm test
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const engine = require("../engine/score.js");
const ROOT = path.join(__dirname, "..");
const config = JSON.parse(fs.readFileSync(path.join(ROOT, "config", "scoring.json"), "utf8"));

const allLayers = (score) => Object.fromEntries(Object.keys(config.layers).map((k) => [k, score]));

test("the real config is valid", () => {
  engine.validateConfig(config);
});

test("config with weights not summing to 1 is rejected", () => {
  const bad = structuredClone(config);
  bad.layers.trend.weight = 0.5;
  assert.throws(() => engine.validateConfig(bad), /weights sum/);
});

test("scoreInput maps bad->0, good->100, linear between, clamped outside", () => {
  const rule = { bad: -10, good: 10 };
  assert.equal(engine.scoreInput(-10, rule), 0);
  assert.equal(engine.scoreInput(0, rule), 50);
  assert.equal(engine.scoreInput(10, rule), 100);
  assert.equal(engine.scoreInput(-50, rule), 0);
  assert.equal(engine.scoreInput(50, rule), 100);
  assert.equal(engine.scoreInput(null, rule), null);
  assert.equal(engine.scoreInput(NaN, rule), null);
});

test("scoreInput works when lower is better (bad > good)", () => {
  const rule = { bad: 60, good: 12 };
  assert.equal(engine.scoreInput(12, rule), 100);
  assert.equal(engine.scoreInput(36, rule), 50);
  assert.equal(engine.scoreInput(100, rule), 0);
});

test("RSI peaks at 60 and falls to 0 at 85 (overbought)", () => {
  const rule = config.layers.momentum.inputs.rsi14;
  assert.equal(engine.scoreInput(30, rule), 0);
  assert.equal(engine.scoreInput(60, rule), 100);
  assert.equal(engine.scoreInput(72.5, rule), 50);
  assert.equal(engine.scoreInput(85, rule), 0);
  assert.equal(engine.scoreInput(95, rule), 0);
});

test("non-positive values: P/E counts as worst, PEG is ignored", () => {
  const v = config.layers.valuation.inputs;
  assert.equal(engine.scoreInput(-5, v.ForwardPE), 0);
  assert.equal(engine.scoreInput(-1, v.PEGRatio), null);
});

test("layer score averages available inputs and needs at least half of them", () => {
  const trend = config.layers.trend;
  assert.equal(engine.scoreLayer(trend, { price_vs_sma50_pct: 0, sma50_slope_20d_pct: 0, price_vs_sma100_pct: 0 }), 50);
  assert.equal(engine.scoreLayer(trend, { price_vs_sma50_pct: 10, sma50_slope_20d_pct: 0 }), 75);
  assert.equal(engine.scoreLayer(trend, { price_vs_sma50_pct: 10 }), null);
});

test("growth_quality needs 3 of its 5 inputs", () => {
  const layer = config.layers.growth_quality;
  assert.equal(engine.scoreLayer(layer, { ProfitMargin: 0.35, ReturnOnEquityTTM: 0.35 }), null);
  assert.equal(engine.scoreLayer(layer, { ProfitMargin: 0.35, ReturnOnEquityTTM: 0.35, avg_surprise_pct_4q: 10 }), 100);
});

test("sentiment layer is skipped with fewer than 3 articles", () => {
  const layer = config.layers.sentiment;
  assert.equal(engine.scoreLayer(layer, { weighted_sentiment_7d: 0.35, article_count_7d: 2 }), null);
  assert.equal(engine.scoreLayer(layer, { weighted_sentiment_7d: 0.35, article_count_7d: 3 }), 100);
  assert.equal(engine.scoreLayer(layer, { weighted_sentiment_7d: 0.35 }), null);
});

test("composite is the weighted average of layer scores", () => {
  assert.equal(engine.composite(allLayers(50), config), 50);
  const scores = { trend: 100, momentum: 0, risk: 0, valuation: 0, growth_quality: 0, sentiment: 0 };
  assert.equal(engine.composite(scores, config), 20); // trend weight 0.20
});

test("one missing layer: its weight is spread over the rest", () => {
  const scores = { ...allLayers(80), sentiment: null };
  assert.equal(engine.composite(scores, config), 80);
});

test("two missing layers: no composite", () => {
  const scores = { ...allLayers(80), sentiment: null, valuation: null };
  assert.equal(engine.composite(scores, config), null);
});

test("custom weights (used by the dashboard) override the config", () => {
  const weights = { trend: 1, momentum: 0, risk: 0, valuation: 0, growth_quality: 0, sentiment: 0 };
  assert.equal(engine.composite({ ...allLayers(10), trend: 90 }, config, weights), 90);
});

test("ratings follow the bands in the config", () => {
  assert.equal(engine.rating(75, config), "Strong");
  assert.equal(engine.rating(74.9, config), "Positive");
  assert.equal(engine.rating(40, config), "Neutral");
  assert.equal(engine.rating(25, config), "Weak");
  assert.equal(engine.rating(0, config), "Avoid");
  assert.equal(engine.rating(null, config), null);
});

test("zones are measured in ATRs around the 50-day average", () => {
  const z = (price) => engine.zone({ price, sma50: 100, sma100: 90, atr14: 4 }, config).current;
  assert.equal(z(89), "breakdown");
  assert.equal(z(93), "pullback");   // below 100 - 1x4 = 96
  assert.equal(z(97), "accumulate"); // 96..100
  assert.equal(z(100), "hold");      // 100..108
  assert.equal(z(108), "hold");
  assert.equal(z(109), "extended");  // above 100 + 2x4 = 108
  const full = engine.zone({ price: 100, sma50: 100, sma100: 90, atr14: 4 }, config);
  assert.deepEqual(full, {
    current: "hold", sma50: 100, sma100: 90, atr14: 4,
    accumulate_low: 96, accumulate_high: 100, extended_above: 108, breakdown_below: 90,
  });
  assert.equal(engine.zone({ price: 100, sma50: null, sma100: 90, atr14: 4 }, config), null);
});

test("flags large moves versus the previous day", () => {
  const current = { composite: 70, layers: { trend: { score: 90 }, risk: { score: 50 } } };
  const previous = { composite: 59, layers: { trend: { score: 50 }, risk: { score: 45 } } };
  assert.deepEqual(engine.flags(current, previous, config), ["composite_jump", "layer_jump:trend"]);
  assert.deepEqual(engine.flags(current, undefined, config), []);
});

test("scoreTicker marks a ticker not_scored when too many layers are missing", () => {
  const result = engine.scoreTicker({ inputs: { trend: { price_vs_sma50_pct: 0, sma50_slope_20d_pct: 0 } } }, config);
  assert.equal(result.status, "not_scored");
  assert.equal(result.composite, null);
  assert.match(result.reason, /too many missing layers/);
});

test("command line: reads JSON on stdin, writes scores on stdout", () => {
  const input = {
    config,
    tickers: {
      TEST: {
        inputs: {
          trend: { price_vs_sma50_pct: 0, sma50_slope_20d_pct: 0, price_vs_sma100_pct: 0 },
          momentum: { rsi14: 45, return_20d_pct: 0, macd_hist_pct: 0 },
          risk: { atr14_pct: 3.75, volatility_20d_pct: 50, drawdown_from_high_pct: -17.5 },
          valuation: { ForwardPE: 36, PEGRatio: 1.9, EVToEBITDA: 29 },
          growth_quality: { QuarterlyRevenueGrowthYOY: 0.15, QuarterlyEarningsGrowthYOY: 0.15, ProfitMargin: 0.175, ReturnOnEquityTTM: 0.175, avg_surprise_pct_4q: 0 },
          sentiment: { weighted_sentiment_7d: 0, article_count_7d: 10 },
        },
        zone_inputs: { price: 100, sma50: 100, sma100: 90, atr14: 4 },
      },
    },
  };
  const out = JSON.parse(execFileSync("node", [path.join(ROOT, "engine", "score.js")], { input: JSON.stringify(input) }));
  assert.equal(out.engine_version, engine.ENGINE_VERSION);
  const t = out.tickers.TEST;
  assert.equal(t.status, "scored");
  for (const layer of Object.values(t.layers)) assert.equal(layer.score, 50);
  assert.equal(t.composite, 50);
  assert.equal(t.rating, "Neutral");
  assert.equal(t.zone.current, "hold");
});

test("command line: invalid config exits with an error", () => {
  const bad = structuredClone(config);
  bad.layers.trend.weight = 0.9;
  assert.throws(
    () => execFileSync("node", [path.join(ROOT, "engine", "score.js")], { input: JSON.stringify({ config: bad, tickers: {} }), stdio: "pipe" }),
    /weights sum/,
  );
});
