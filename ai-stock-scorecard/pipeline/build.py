#!/usr/bin/env python3
"""Build the dashboard page: dashboard/template.html + engine/score.js + data -> dist/index.html.

The output is one self-contained HTML file (no external data files), ready to publish.

Usage:
    python pipeline/build.py                                  # data/ -> dist/index.html
    python pipeline/build.py --data sample/data --sample      # made-up data, with a banner saying so
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "dashboard" / "template.html"
ENGINE = ROOT / "engine" / "score.js"
CONFIG = ROOT / "config" / "scoring.json"
HISTORY_DAYS = 90
SCHEMA_VERSION = 1
ENGINE_SLOT = "/*__ENGINE__*/"
DATA_SLOT = "/*__DATA__*/"
MAX_BYTES = 16 * 1024 * 1024  # artifact page size limit


class BuildError(Exception):
    pass


def script_safe(text):
    """Stop embedded text from closing the <script> tag it sits in."""
    return re.sub(r"</(script)", r"<\\/\1", text, flags=re.IGNORECASE)


def load_history(data_dir):
    """The last HISTORY_DAYS days, reduced to what the trend chart needs."""
    files = sorted((data_dir / "history").glob("*.json"))[-HISTORY_DAYS:]
    history = []
    for path in files:
        day = json.loads(path.read_text())
        history.append({
            "date": day["market_date"],
            "tickers": {t: {"composite": v.get("composite"), "price": v.get("price")} for t, v in day["tickers"].items()},
        })
    return history


def build(data_dir, sample=False):
    latest_path = data_dir / "latest.json"
    if not latest_path.exists():
        raise BuildError(f"no {latest_path}; run pipeline/compute.py first")
    latest = json.loads(latest_path.read_text())
    if latest.get("schema_version") != SCHEMA_VERSION:
        raise BuildError(f"latest.json has schema_version {latest.get('schema_version')}, page expects {SCHEMA_VERSION}")
    config = json.loads(CONFIG.read_text())
    if config["version"] != latest["scoring_config_version"]:
        print(f"warning: scores were computed with scoring config {latest['scoring_config_version']}, "
              f"page shows config {config['version']}", file=sys.stderr)

    template = TEMPLATE.read_text()
    for slot in (ENGINE_SLOT, DATA_SLOT):
        if template.count(slot) != 1:
            raise BuildError(f"template must contain {slot} exactly once")
    payload = {"latest": latest, "history": load_history(data_dir), "config": config, "sample": sample}
    data_js = "window.SCORECARD = " + script_safe(json.dumps(payload, separators=(",", ":"))) + ";"
    html = template.replace(ENGINE_SLOT, script_safe(ENGINE.read_text())).replace(DATA_SLOT, data_js)
    if len(html.encode()) > MAX_BYTES:
        raise BuildError(f"page is {len(html.encode()) // 1024} KB, over the 16 MB limit")
    return html


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=ROOT / "data", help="folder with latest.json and history/")
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "index.html")
    ap.add_argument("--sample", action="store_true", help="show the made-up-data banner")
    args = ap.parse_args(argv)
    try:
        html = build(args.data, args.sample)
    except BuildError as exc:
        print(f"build.py: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(f"Wrote {args.out} ({len(html.encode()) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
