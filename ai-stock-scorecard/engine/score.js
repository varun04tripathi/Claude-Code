/* Six-layer scoring engine. Pure functions, no dependencies.
 *
 * pipeline/compute.py measures (indicators, fundamentals, sentiment); this file judges
 * (layer scores, composite, rating, zone, flags). All thresholds come from config/scoring.json.
 *
 * The same file runs in three places:
 *   - Node, as a command:  node engine/score.js < input.json > output.json
 *   - Node, as a module:   const engine = require("./engine/score.js")
 *   - The dashboard page:  window.ScoreEngine (inlined by build.py)
 */
(function (root, factory) {
  const engine = factory();
  if (typeof module === "object" && module.exports) module.exports = engine;
  else root.ScoreEngine = engine;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const ENGINE_VERSION = "1.0.0";

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const round = (v, digits = 0) => {
    const f = 10 ** digits;
    return Math.round(v * f) / f;
  };

  function validateConfig(config) {
    const layers = Object.entries((config && config.layers) || {});
    if (!layers.length) throw new Error("config has no layers");
    const total = layers.reduce((sum, [, layer]) => sum + layer.weight, 0);
    if (Math.abs(total - 1) > 1e-6) throw new Error(`layer weights sum to ${total}, expected 1`);
    if (config.missing_layer_policy !== "renormalize") {
      throw new Error(`unsupported missing_layer_policy: ${config.missing_layer_policy}`);
    }
    for (const [name, layer] of layers) {
      if (!layer.inputs || !Object.keys(layer.inputs).length) throw new Error(`layer ${name} has no inputs`);
      for (const [input, rule] of Object.entries(layer.inputs)) {
        if (!isNum(rule.bad) || !isNum(rule.good) || rule.bad === rule.good) {
          throw new Error(`invalid rule for ${name}.${input}`);
        }
      }
    }
  }

  /** Map one value onto 0-100: `bad` -> 0, `good` -> 100, linear in between, clamped.
   *  `bad_above` makes a peak: past `good` the score falls again, reaching 0 at `bad_above`.
   *  `nonpositive`: "worst" scores values <= 0 as 0 (e.g. negative P/E); "ignore" treats them as missing.
   *  Returns null when there is no usable value. */
  function scoreInput(value, rule) {
    if (!isNum(value)) return null;
    if (value <= 0 && rule.nonpositive) return rule.nonpositive === "worst" ? 0 : null;
    const pastGood = (value - rule.good) * (rule.good - rule.bad) > 0;
    if (isNum(rule.bad_above) && pastGood) {
      return clamp((value - rule.bad_above) / (rule.good - rule.bad_above), 0, 1) * 100;
    }
    return clamp((value - rule.bad) / (rule.good - rule.bad), 0, 1) * 100;
  }

  /** Average of the layer's input scores, rounded to a whole number.
   *  null when the gate fails or fewer than `min_inputs` (default: half) have values. */
  function scoreLayer(layer, inputs) {
    inputs = inputs || {};
    const gate = layer.gate;
    if (gate && !(isNum(inputs[gate.input]) && inputs[gate.input] >= gate.min)) return null;
    const scores = Object.entries(layer.inputs)
      .map(([name, rule]) => scoreInput(inputs[name], rule))
      .filter(isNum);
    const needed = isNum(layer.min_inputs) ? layer.min_inputs : Math.ceil(Object.keys(layer.inputs).length / 2);
    if (!scores.length || scores.length < needed) return null;
    return round(scores.reduce((a, b) => a + b, 0) / scores.length);
  }

  /** Weighted average of layer scores, one decimal. A missing layer's weight is spread over the
   *  others; more than `max_missing_layers` missing gives null. Pass `weights` to try other weights
   *  (the dashboard does this); by default they come from the config. */
  function composite(layerScores, config, weights) {
    weights = weights || Object.fromEntries(Object.entries(config.layers).map(([k, l]) => [k, l.weight]));
    const names = Object.keys(weights);
    const present = names.filter((k) => isNum(layerScores[k]));
    const maxMissing = isNum(config.max_missing_layers) ? config.max_missing_layers : 0;
    if (!present.length || names.length - present.length > maxMissing) return null;
    const total = present.reduce((sum, k) => sum + weights[k], 0);
    if (total <= 0) return null;
    return round(present.reduce((sum, k) => sum + layerScores[k] * weights[k], 0) / total, 1);
  }

  function rating(score, config) {
    if (!isNum(score)) return null;
    const band = config.ratings.find((r) => score >= r.min);
    return band ? band.label : null;
  }

  /** Price zone around the 50-day average, measured in ATRs. Checked in this order:
   *  breakdown (below sma100), extended (above sma50 + N ATR), hold (at or above sma50),
   *  accumulate (within N ATR below sma50), pullback (further below, still above sma100). */
  function zone(z, config) {
    const { price, sma50, sma100, atr14 } = z || {};
    if (![price, sma50, sma100, atr14].every(isNum)) return null;
    const accumulateLow = sma50 - config.zones.accumulate_atr_below * atr14;
    const extendedAbove = sma50 + config.zones.extended_atr_above * atr14;
    let current;
    if (price < sma100) current = "breakdown";
    else if (price > extendedAbove) current = "extended";
    else if (price >= sma50) current = "hold";
    else if (price >= accumulateLow) current = "accumulate";
    else current = "pullback";
    return {
      current,
      sma50: round(sma50, 2),
      sma100: round(sma100, 2),
      atr14: round(atr14, 2),
      accumulate_low: round(accumulateLow, 2),
      accumulate_high: round(sma50, 2),
      extended_above: round(extendedAbove, 2),
      breakdown_below: round(sma100, 2),
    };
  }

  /** Alerts for large day-over-day moves, compared with the previous day's entry. */
  function flags(current, previous, config) {
    if (!previous) return [];
    const out = [];
    if (isNum(current.composite) && isNum(previous.composite) &&
        Math.abs(current.composite - previous.composite) >= config.alerts.composite_change_pts) {
      out.push("composite_jump");
    }
    for (const [name, layer] of Object.entries(current.layers)) {
      const before = previous.layers && previous.layers[name] ? previous.layers[name].score : null;
      if (isNum(layer.score) && isNum(before) && Math.abs(layer.score - before) >= config.alerts.layer_change_pts) {
        out.push(`layer_jump:${name}`);
      }
    }
    return out;
  }

  /** Score one ticker. `t` is { inputs: { <layer>: { <input>: number|null } }, zone_inputs }. */
  function scoreTicker(t, config, previous) {
    const layers = {};
    const layerScores = {};
    for (const [name, layer] of Object.entries(config.layers)) {
      const inputs = (t.inputs && t.inputs[name]) || {};
      layerScores[name] = scoreLayer(layer, inputs);
      layers[name] = { score: layerScores[name], inputs };
    }
    const missing = Object.keys(layers).filter((k) => layerScores[k] === null);
    const score = composite(layerScores, config);
    const result = {
      status: score === null ? "not_scored" : "scored",
      composite: score,
      rating: rating(score, config),
      layers,
      zone: zone(t.zone_inputs, config),
      missing_layers: missing,
    };
    if (score === null) result.reason = `too many missing layers: ${missing.join(", ")}`;
    result.flags = flags(result, previous, config);
    return result;
  }

  /** Input: { config, tickers: { SYM: {inputs, zone_inputs} }, previous: { SYM: <yesterday's entry> } } */
  function run(input) {
    validateConfig(input.config);
    const previous = input.previous || {};
    const tickers = {};
    for (const [symbol, t] of Object.entries(input.tickers || {})) {
      tickers[symbol] = scoreTicker(t, input.config, previous[symbol]);
    }
    return { engine_version: ENGINE_VERSION, tickers };
  }

  return { ENGINE_VERSION, validateConfig, scoreInput, scoreLayer, composite, rating, zone, flags, scoreTicker, run };
});

if (typeof require !== "undefined" && typeof module !== "undefined" && require.main === module) {
  let text = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => (text += chunk));
  process.stdin.on("end", () => {
    try {
      process.stdout.write(JSON.stringify(module.exports.run(JSON.parse(text))));
    } catch (e) {
      process.stderr.write(`score.js: ${e.message}\n`);
      process.exit(1);
    }
  });
}
