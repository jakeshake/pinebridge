"""Attribute older, untagged trades to a strategy, by symbol.

Trades from before your Pine scripts sent `strategy=<tag>` show as
"untagged". Tell the bridge which strategy ran on each pair:

    docker exec -u bridge pinebridge-bridge python -m app.backfill EURUSD=igt AUDUSD=sflow-v2
    docker exec -u bridge pinebridge-bridge python -m app.backfill USDJPY=sdx2 --before 2026-10-03
    docker exec -u bridge pinebridge-bridge python -m app.backfill --list

Each pair becomes a rule: untagged trades and signals on that symbol from
before --before (default: now) get the strategy. Nothing in the raw
signals or deals is changed, and a tag in the data itself always wins.
"""
import argparse
import sys
import time
from datetime import datetime

from . import config
from .store import Store
from .translate import UNTAGGED


def _when(text):
    if not text:
        return time.time()
    dt = datetime.fromisoformat(text)   # a bare date or naive time is the container's local time
    return dt.timestamp()


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.backfill", description=__doc__.split("\n\n")[0])
    p.add_argument("pairs", nargs="*", metavar="SYMBOL=strategy")
    p.add_argument("--before", help="only trades opened before this (YYYY-MM-DD or ISO time); default now")
    p.add_argument("--list", action="store_true", help="show the rules and untagged trades per symbol")
    p.add_argument("--db", default=config.DB_PATH)
    args = p.parse_args(argv)

    store = Store(args.db)
    before = _when(args.before)
    for pair in args.pairs:
        if "=" not in pair:
            p.error(f"expected SYMBOL=strategy, got {pair!r}")
        symbol, strategy = pair.split("=", 1)
        tag = store.add_backfill(symbol.strip(), strategy, before)
        print(f"{symbol.strip().upper()}: trades opened before {datetime.fromtimestamp(before):%Y-%m-%d %H:%M} -> {tag}")

    if args.list or not args.pairs:
        for r in store.backfill_rules():
            print(f"rule #{r['id']}: {r['symbol']} before {datetime.fromtimestamp(r['before_ts']):%Y-%m-%d %H:%M}"
                  f" -> {r['strategy']}")
    counts = {}
    for t in store.positions():
        if t["closed"]:
            key = (t["symbol"], t["strategy"])
            counts[key] = counts.get(key, 0) + 1
    print("closed trades by symbol and strategy:")
    for (symbol, strategy), n in sorted(counts.items()):
        flag = "  <- untagged" if strategy == UNTAGGED else ""
        print(f"  {symbol:10} {strategy:16} {n}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
