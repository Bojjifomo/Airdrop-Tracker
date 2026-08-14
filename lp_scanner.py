"""
Robinhood Chain LP trigger — finds coins worth providing liquidity to.

A coin fires the trigger when its pool clears both gates:

    market cap  >  $500,000
    24h fees    >  0.5 ETH

Fees are pool-wide fees earned over the last 24 hours (24h volume x pool fee
tier), converted to ETH. That is the size of the pie an LP position would be
taking a share of — not your share of it.

Run once (this is what cron calls):
    python lp_scanner.py

Run continuously without cron:
    python lp_scanner.py --loop 180

Cron, every 3 minutes:
    */3 * * * * cd /path/to/Airdrop-Tracker && /usr/bin/python3 lp_scanner.py >> lp_scanner.log 2>&1

Results are written to lp_hits.json, which the Streamlit app reads and renders.

Environment overrides (all optional):
    GT_API          GeckoTerminal API base URL
    RH_NETWORK      GeckoTerminal network id, skips auto-discovery
    ETH_USD         pin the ETH price instead of deriving it from pool data
    LP_FEE_TIERS    JSON map of dex-id substring -> fee fraction, e.g. {"froth": 0.01}
"""

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

import requests

# ----------------------------- config ---------------------------------------
API = os.environ.get("GT_API", "https://api.geckoterminal.com/api/v2").rstrip("/")
STATE_FILE = "lp_hits.json"
NETWORK_HINT = "robinhood"

MIN_MCAP_USD = 500_000.0
MIN_FEES_ETH = 0.5
FEE_WINDOW = "h24"

PAGES = 10                # 20 pools per page, sorted by 24h volume desc
HISTORY_LIMIT = 60        # runs of scan history kept in the state file
REQUEST_PAUSE = 0.4       # stay under GeckoTerminal's free-tier 30 calls/min

# Pool fee tiers used only when the API does not report one for a pool.
# Keys are matched as substrings against the pool's dex id.
DEX_FEE_DEFAULTS = {
    "pools": 0.0025,        # pools.trade (Uniswap v4 launchpad) — 0.25%
    "uniswap-v4": 0.0025,
    "froth": 0.01,          # froth.meme / FrothSwap — 1%
    "robinpad": 0.01,       # launchpad graduates seed the 1% v3 tier
    "uniswap-v2": 0.003,
    "uniswap-v3": 0.003,
}
FALLBACK_FEE = 0.003


class ScanError(RuntimeError):
    """Raised when the scan cannot produce a trustworthy result."""


# ----------------------------- helpers --------------------------------------
def _f(value):
    """Coerce an API value to float, returning None for null/blank/garbage."""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def fee_tiers():
    """Default fee tiers, with LP_FEE_TIERS merged over them if set."""
    tiers = dict(DEX_FEE_DEFAULTS)
    raw = os.environ.get("LP_FEE_TIERS")
    if raw:
        try:
            override = json.loads(raw)
        except json.JSONDecodeError as ex:
            raise ScanError(f"LP_FEE_TIERS is not valid JSON: {ex}") from ex
        for key, val in override.items():
            rate = _f(val)
            if rate is None or not 0 <= rate < 1:
                raise ScanError(f"LP_FEE_TIERS['{key}'] must be a fraction like 0.01")
            tiers[str(key).lower()] = rate
    return tiers


# ----------------------------- API ------------------------------------------
def new_session():
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json;version=20230302",
        "User-Agent": "airdrop-desk-lp-scanner/1.0",
    })
    return s


def _request(session, path, params=None, attempts=4):
    """GET with backoff on rate limits and server errors."""
    url = f"{API}{path}"
    delay, last = 1.0, None
    for i in range(attempts):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
            elif r.status_code == 404:
                return None
            else:
                r.raise_for_status()
                return r.json()
        except requests.RequestException as ex:
            last = str(ex)
        if i < attempts - 1:
            time.sleep(delay)
            delay *= 2
    raise ScanError(f"GET {path} failed after {attempts} attempts: {last}")


def resolve_network(session, hint=NETWORK_HINT):
    """Find the GeckoTerminal network id for Robinhood Chain by name."""
    hint = hint.lower()
    partial = []
    for page in range(1, 11):
        payload = _request(session, "/networks", {"page": page})
        rows = (payload or {}).get("data") or []
        if not rows:
            break
        for n in rows:
            nid = n.get("id", "")
            name = (n.get("attributes") or {}).get("name", "")
            if hint in (nid.lower(), name.lower()):
                return nid
            if hint in nid.lower() or hint in name.lower():
                partial.append(nid)
        time.sleep(REQUEST_PAUSE)
    if partial:
        return partial[0]
    raise ScanError(
        f"no GeckoTerminal network matching '{hint}'. Set RH_NETWORK to the "
        f"correct network id (see {API}/networks)."
    )


def fetch_pools(session, network, pages=PAGES):
    """Top pools by 24h volume, plus the newest pools, deduped by address."""
    pools, included, seen = [], {}, set()
    endpoints = [("/networks/%s/pools" % network, {"sort": "h24_volume_usd_desc"}),
                 ("/networks/%s/new_pools" % network, {})]
    for path, extra in endpoints:
        for page in range(1, pages + 1):
            params = {"include": "dex,base_token,quote_token", "page": page, **extra}
            payload = _request(session, path, params)
            if payload is None:
                break
            for item in payload.get("included") or []:
                included[item["id"]] = item
            rows = payload.get("data") or []
            if not rows:
                break
            for pool in rows:
                if pool["id"] not in seen:
                    seen.add(pool["id"])
                    pools.append(pool)
            time.sleep(REQUEST_PAUSE)
    if not pools:
        raise ScanError(f"network '{network}' returned no pools")
    return pools, included


# ----------------------------- metrics --------------------------------------
def derive_eth_usd(pools):
    """USD price of the chain's native currency, from price pairs in the feed.

    Every pool reports its base token in both USD and native currency, so the
    ratio of the two is the native price. Taking the median across pools keeps
    one mispriced token from moving the result.
    """
    pinned = _f(os.environ.get("ETH_USD"))
    if pinned and pinned > 0:
        return pinned, "env"
    quotes = []
    for pool in pools:
        a = pool.get("attributes") or {}
        usd = _f(a.get("base_token_price_usd"))
        native = _f(a.get("base_token_price_native_currency"))
        if usd and native and usd > 0 and native > 0:
            quotes.append(usd / native)
    if not quotes:
        raise ScanError("could not derive the ETH price from pool data; set ETH_USD")
    return statistics.median(quotes), "derived"


def _rel_id(pool, name):
    return (((pool.get("relationships") or {}).get(name) or {}).get("data") or {}).get("id")


def pool_fee_rate(attrs, dex_id, tiers):
    """Fee fraction for a pool, and where that number came from."""
    pct = _f(attrs.get("pool_fee_percentage"))
    if pct is not None and 0 < pct < 100:
        return pct / 100.0, "api"
    dex = (dex_id or "").lower()
    for key, rate in tiers.items():
        if key in dex:
            return rate, f"default:{key}"
    return FALLBACK_FEE, "fallback"


def pool_metrics(pool, included, eth_usd, tiers, window=FEE_WINDOW):
    """Flatten one pool into the numbers the trigger cares about."""
    a = pool.get("attributes") or {}
    dex_id = _rel_id(pool, "dex") or ""
    base = included.get(_rel_id(pool, "base_token") or "") or {}
    base_attrs = base.get("attributes") or {}

    volume = _f((a.get("volume_usd") or {}).get(window)) or 0.0
    reserve = _f(a.get("reserve_in_usd"))

    mcap, mcap_source = _f(a.get("market_cap_usd")), "market_cap"
    if not mcap:
        # New launchpad tokens rarely have a CoinGecko-backed market cap. FDV is
        # the same number when the whole supply is circulating, which is the
        # normal case for these launches.
        mcap, mcap_source = _f(a.get("fdv_usd")), "fdv"

    fee_rate, fee_source = pool_fee_rate(a, dex_id, tiers)
    fees_usd = volume * fee_rate
    fees_eth = fees_usd / eth_usd if eth_usd else None

    return {
        "pool_id": pool.get("id"),
        "pool_address": a.get("address"),
        "pool_name": a.get("name"),
        "dex": dex_id,
        "symbol": base_attrs.get("symbol"),
        "token_name": base_attrs.get("name"),
        "token_address": base_attrs.get("address"),
        "price_usd": _f(a.get("base_token_price_usd")),
        "mcap_usd": mcap,
        "mcap_source": mcap_source,
        "volume_usd": volume,
        "reserve_usd": reserve,
        "fee_rate": fee_rate,
        "fee_source": fee_source,
        "fees_usd": fees_usd,
        "fees_eth": fees_eth,
        # Annualised pool fee yield: informational, never a filter. It is what
        # the whole pool earns, so your share scales with your share of reserves.
        "fee_apr": (fees_usd * 365 / reserve * 100) if reserve else None,
        "price_change_h24": _f((a.get("price_change_percentage") or {}).get("h24")),
        "pool_created_at": a.get("pool_created_at"),
        "url": f"https://www.geckoterminal.com/{(pool.get('id') or '_').split('_')[0]}"
               f"/pools/{a.get('address')}" if a.get("address") else None,
    }


def passes(row, min_mcap=MIN_MCAP_USD, min_fees_eth=MIN_FEES_ETH):
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
    """Carry first_seen / streaks across runs and mark newly-qualifying pools."""
    prior = {h["pool_id"]: h for h in previous.get("hits", []) if h.get("pool_id")}
    for hit in result["hits"]:
        was = prior.get(hit["pool_id"])
        hit["first_seen"] = (was or {}).get("first_seen") or result["generated_at"]
        hit["runs_passed"] = ((was or {}).get("runs_passed") or 0) + 1
        hit["is_new"] = was is None
    history = previous.get("history", [])
    history.append({
        "at": result["generated_at"],
        "scanned": result["scanned"],
        "hit_count": len(result["hits"]),
        "hits": [h.get("symbol") or h.get("pool_name") for h in result["hits"]],
    })
    result["history"] = history[-HISTORY_LIMIT:]
    return result


# ----------------------------- scan -----------------------------------------
def scan(network=None, min_mcap=MIN_MCAP_USD, min_fees_eth=MIN_FEES_ETH,
         window=FEE_WINDOW, pages=PAGES, session=None, state_path=STATE_FILE):
    """Run one full scan and return the result dict (does not write to disk)."""
    session = session or new_session()
    previous = load_state(state_path)
    network = network or os.environ.get("RH_NETWORK") or previous.get("network")
    if not network:
        network = resolve_network(session)

    pools, included = fetch_pools(session, network, pages)
    eth_usd, eth_source = derive_eth_usd(pools)
    tiers = fee_tiers()

    rows = [pool_metrics(p, included, eth_usd, tiers, window) for p in pools]
    hits = sorted((r for r in rows if passes(r, min_mcap, min_fees_eth)),
                  key=lambda r: r["fees_eth"], reverse=True)

    result = {
        "generated_at": _iso(_now()),
        "network": network,
        "eth_usd": round(eth_usd, 2),
        "eth_price_source": eth_source,
        "filters": {
            "min_mcap_usd": min_mcap,
            "min_fees_eth": min_fees_eth,
            "fee_window": window,
        },
        "scanned": len(rows),
        "hits": hits,
    }
    return merge_state(previous, result)


# ----------------------------- cli ------------------------------------------
def _summarise(result):
    hits = result["hits"]
    head = (f"{result['generated_at']}  {result['network']}  "
            f"scanned {result['scanned']}  hits {len(hits)}  "
            f"ETH ${result['eth_usd']:,.0f}")
    lines = [head]
    for h in hits:
        flag = "NEW " if h.get("is_new") else "    "
        lines.append(
            f"  {flag}{(h.get('symbol') or '?'):<12} "
            f"mcap ${(h.get('mcap_usd') or 0):>12,.0f}  "
            f"fees {h['fees_eth']:>6.2f} ETH  "
            f"apr {(h.get('fee_apr') or 0):>7.0f}%  {h.get('dex') or ''}"
        )
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="Robinhood Chain LP trigger scanner")
    p.add_argument("--min-mcap", type=float, default=MIN_MCAP_USD,
                   help="minimum market cap in USD (default: 500000)")
    p.add_argument("--min-fees-eth", type=float, default=MIN_FEES_ETH,
                   help="minimum 24h pool fees in ETH (default: 0.5)")
    p.add_argument("--window", default=FEE_WINDOW, choices=["m5", "h1", "h6", "h24"],
                   help="fee measurement window (default: h24)")
    p.add_argument("--pages", type=int, default=PAGES,
                   help="pages of pools to pull per endpoint (default: 10)")
    p.add_argument("--network", default=None,
                   help="GeckoTerminal network id, skips auto-discovery")
    p.add_argument("--state", default=STATE_FILE, help="output JSON path")
    p.add_argument("--loop", type=int, metavar="SECONDS", default=None,
                   help="keep scanning every N seconds instead of exiting")
    p.add_argument("--quiet", action="store_true", help="only print on hits or errors")
    args = p.parse_args(argv)

    session = new_session()

    def once():
        try:
            result = scan(network=args.network, min_mcap=args.min_mcap,
                          min_fees_eth=args.min_fees_eth, window=args.window,
                          pages=args.pages, session=session, state_path=args.state)
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
