"""
Robinhood Chain LP trigger — finds coins worth providing liquidity to.

A coin fires the trigger when it clears both gates:

    market cap  >  $500,000
    fees, 24h   >  0.5 ETH

Fees are pool-wide fees over the window (volume x fee tier), converted to ETH.
That is the size of the pie an LP position takes a share of, not your share of
it — your cut scales with your share of the pool's liquidity.

Sources (see lp_sources.py) differ in what they can prove:

    gmgn           widest Robinhood Chain coverage + honeypot/tax flags, but the
                   fee tier is ASSUMED (--fee-rate) and the ETH price must be
                   supplied (--eth-usd / ETH_USD).
    geckoterminal  reports a real per-pool fee tier and derives the ETH price
                   itself, at the cost of narrower launchpad coverage.
    custom         configurable JSON adapter — the path for FOMO.

Run once (this is what cron calls):
    python lp_scanner.py --source gmgn --eth-usd 3200

Run continuously without cron:
    python lp_scanner.py --loop 180

Cron, every 3 minutes:
    */3 * * * * cd /path/to/Airdrop-Tracker && /usr/bin/python3 lp_scanner.py --quiet >> lp_scanner.log 2>&1

Results are written to lp_hits.json, which the Streamlit app reads.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import lp_sources
from lp_sources import ScanError, _dig, _f, _request  # noqa: F401  (re-exported)

STATE_FILE = "lp_hits.json"
DEFAULT_SOURCE = "gmgn"

MIN_MCAP_USD = 500_000.0
MIN_FEES_ETH = 0.5
FEE_WINDOW = "h24"

PAGES = 10
HISTORY_LIMIT = 60


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


# ----------------------------- eth price ------------------------------------
def resolve_eth_usd(session, meta, pinned=None):
    """Settle on an ETH/USD price, most explicit wins.

    Sources that price tokens in the native currency can derive this; the ones
    that only speak USD cannot, so the price has to be supplied. Rather than
    invent a number, an unresolvable price is an error.
    """
    if pinned and pinned > 0:
        return pinned, "flag"
    env = _f(os.environ.get("ETH_USD"))
    if env and env > 0:
        return env, "env"
    derived = _f(meta.get("eth_usd"))
    if derived and derived > 0:
        return derived, meta.get("eth_price_source", "derived")

    url = os.environ.get("ETH_PRICE_URL")
    if url:
        payload = _request(session, url)
        price = _f(_dig(payload, os.environ.get("ETH_PRICE_PATH", "")))
        if price and price > 0:
            return price, "url"
        raise ScanError(
            f"ETH_PRICE_URL returned no usable number at ETH_PRICE_PATH="
            f"'{os.environ.get('ETH_PRICE_PATH', '')}'"
        )
    raise ScanError(
        "this source does not report an ETH price. Pass --eth-usd, set ETH_USD, "
        "or set ETH_PRICE_URL (+ ETH_PRICE_PATH) to a feed you can reach."
    )


# ----------------------------- scoring --------------------------------------
def score_row(row, eth_usd):
    """Attach the derived fee figures to a normalized row."""
    fees_usd = (row.get("volume_usd") or 0.0) * (row.get("fee_rate") or 0.0)
    reserve = row.get("reserve_usd")
    row["fees_usd"] = fees_usd
    row["fees_eth"] = fees_usd / eth_usd if eth_usd else None
    # Annualised pool fee yield: informational, never a filter. It is what the
    # whole pool earns, so your share scales with your share of reserves.
    row["fee_apr"] = (fees_usd * 365 / reserve * 100) if reserve else None
    return row


def passes(row, min_mcap=MIN_MCAP_USD, min_fees_eth=MIN_FEES_ETH, allow_honeypot=False):
    """Both gates, plus a safety veto.

    A honeypot can post a convincing market cap and volume while being
    unsellable, which is a trap worth failing closed on when the whole point is
    to park liquidity in it.
    """
    if not allow_honeypot and row.get("is_honeypot"):
        return False
    return (row.get("mcap_usd") or 0) > min_mcap and (row.get("fees_eth") or 0) > min_fees_eth


# ----------------------------- state ----------------------------------------
def load_state(path=STATE_FILE):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_state(state, path=STATE_FILE):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)   # atomic, so the dashboard never reads a half-written file


def merge_state(previous, result):
    """Carry first_seen / streaks across runs and mark newly-qualifying coins."""
    same_source = previous.get("source") == result["source"]
    prior = {h["row_id"]: h for h in previous.get("hits", [])
             if h.get("row_id") and same_source}
    for hit in result["hits"]:
        was = prior.get(hit["row_id"])
        hit["first_seen"] = (was or {}).get("first_seen") or result["generated_at"]
        hit["runs_passed"] = ((was or {}).get("runs_passed") or 0) + 1
        hit["is_new"] = was is None
    history = previous.get("history", []) if same_source else []
    history.append({
        "at": result["generated_at"],
        "scanned": result["scanned"],
        "hit_count": len(result["hits"]),
        "hits": [h.get("symbol") or h.get("pool_name") for h in result["hits"]],
    })
    result["history"] = history[-HISTORY_LIMIT:]
    return result


# ----------------------------- scan -----------------------------------------
def scan(source=DEFAULT_SOURCE, network=None, min_mcap=MIN_MCAP_USD,
         min_fees_eth=MIN_FEES_ETH, window=FEE_WINDOW, pages=PAGES, session=None,
         state_path=STATE_FILE, fee_rate=None, eth_usd=None, allow_honeypot=False,
         source_url=None):
    """Run one full scan and return the result dict (does not write to disk)."""
    previous = load_state(state_path)
    src = lp_sources.get_source(source)
    session = session or lp_sources.new_session()

    cfg = {
        "network": network,
        # Only reuse a remembered network id when it came from this same source;
        # each source names Robinhood Chain differently.
        "cached_network": previous.get("network") if previous.get("source") == source else None,
        "pages": pages,
        "fee_rate": fee_rate,
        "source_url": source_url,
    }
    rows, meta = src.rows(session, window, cfg)
    eth, eth_src = resolve_eth_usd(session, meta, eth_usd)

    rows = [score_row(r, eth) for r in rows]
    hits = sorted((r for r in rows if passes(r, min_mcap, min_fees_eth, allow_honeypot)),
                  key=lambda r: r["fees_eth"], reverse=True)

    result = {
        "generated_at": _iso(_now()),
        "source": source,
        "network": meta.get("network"),
        "eth_usd": round(eth, 2),
        "eth_price_source": eth_src,
        "filters": {
            "min_mcap_usd": min_mcap,
            "min_fees_eth": min_fees_eth,
            "fee_window": window,
            "allow_honeypot": allow_honeypot,
        },
        "scanned": len(rows),
        "hits": hits,
    }
    return merge_state(previous, result)


# ----------------------------- cli ------------------------------------------
def _summarise(result):
    lines = [
        f"{result['generated_at']}  {result['source']}/{result['network']}  "
        f"scanned {result['scanned']}  hits {len(result['hits'])}  "
        f"ETH ${result['eth_usd']:,.0f} ({result['eth_price_source']})"
    ]
    for h in result["hits"]:
        flag = "NEW " if h.get("is_new") else "    "
        lines.append(
            f"  {flag}{(h.get('symbol') or '?'):<12} "
            f"mcap ${(h.get('mcap_usd') or 0):>12,.0f}  "
            f"fees {h['fees_eth']:>6.2f} ETH  "
            f"apr {(h.get('fee_apr') or 0):>7.0f}%  [{h.get('fee_source') or '?'}]"
        )
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="Robinhood Chain LP trigger scanner")
    p.add_argument("--source", default=os.environ.get("LP_SOURCE", DEFAULT_SOURCE),
                   choices=sorted(lp_sources.SOURCES),
                   help=f"where to pull coins from (default: {DEFAULT_SOURCE})")
    p.add_argument("--min-mcap", type=float, default=MIN_MCAP_USD,
                   help="minimum market cap in USD (default: 500000)")
    p.add_argument("--min-fees-eth", type=float, default=MIN_FEES_ETH,
                   help="minimum fees in ETH over the window (default: 0.5)")
    p.add_argument("--window", default=FEE_WINDOW, choices=["m5", "h1", "h6", "h24"],
                   help="fee measurement window (default: h24)")
    p.add_argument("--fee-rate", type=float, default=None,
                   help="pool fee fraction to assume when the source omits it "
                        "(default: 0.0025 = 0.25%%)")
    p.add_argument("--eth-usd", type=float, default=None,
                   help="pin the ETH price instead of deriving or fetching it")
    p.add_argument("--allow-honeypot", action="store_true",
                   help="do not veto coins flagged as honeypots")
    p.add_argument("--pages", type=int, default=PAGES,
                   help="pages of pools to pull per endpoint (geckoterminal)")
    p.add_argument("--network", default=None, help="chain id/slug for the source")
    p.add_argument("--source-url", default=None, help="endpoint for --source custom")
    p.add_argument("--state", default=STATE_FILE, help="output JSON path")
    p.add_argument("--loop", type=int, metavar="SECONDS", default=None,
                   help="keep scanning every N seconds instead of exiting")
    p.add_argument("--quiet", action="store_true", help="only print on hits or errors")
    args = p.parse_args(argv)

    session = lp_sources.new_session()

    def once():
        try:
            result = scan(source=args.source, network=args.network,
                          min_mcap=args.min_mcap, min_fees_eth=args.min_fees_eth,
                          window=args.window, pages=args.pages, session=session,
                          state_path=args.state, fee_rate=args.fee_rate,
                          eth_usd=args.eth_usd, allow_honeypot=args.allow_honeypot,
                          source_url=args.source_url)
        except ScanError as ex:
            print(f"scan failed: {ex}", file=sys.stderr)
            return 1
        save_state(result, args.state)
        if result["hits"] or not args.quiet:
            print(_summarise(result), flush=True)
        return 0

    if args.loop:
        while True:
            once()
            time.sleep(args.loop)
    return once()


if __name__ == "__main__":
    sys.exit(main())
