"""
Data sources for the LP trigger.

Each source turns one venue's API into the same normalized row, so the trigger's
arithmetic, thresholds and state handling live in one place (lp_scanner.py) and
never need to know where a number came from.

A normalized row carries:

    symbol, token_name, token_address, pool_address, dex, url
    price_usd, mcap_usd, mcap_source
    volume_usd      already narrowed to the requested window
    reserve_usd     pool liquidity / TVL
    fee_rate        fraction, e.g. 0.0025
    fee_source      where fee_rate came from: "api", "default:<dex>", "assumed"
    is_honeypot, buy_tax, sell_tax, holder_count   safety fields, may be None

Sources differ in what they can actually tell you, and that difference matters:

    geckoterminal  reports a per-pool fee tier, so fees are measured, not guessed.
                   Derives the ETH price itself.
    gmgn           the widest coverage of Robinhood Chain launchpads, plus
                   honeypot and tax flags — but it does NOT report a pool fee
                   tier, so fees are volume x an ASSUMED rate. Needs an ETH
                   price from elsewhere. Cloudflare-protected.
    custom         a configurable JSON adapter. Point it at any endpoint that
                   returns a list of tokens and map the field names. This is the
                   path for FOMO, which publishes no API documentation.
"""

import json
import os
import statistics
import time

import requests

REQUEST_PAUSE = 0.4          # stay under free-tier rate limits
DEFAULT_ASSUMED_FEE = 0.0025  # pools.trade / Uniswap v4, the dominant RH-chain venue
FALLBACK_FEE = 0.003

# Pool fee tiers used when a source does not report one. Keys are matched as
# substrings against the pool's dex id.
DEX_FEE_DEFAULTS = {
    "pools": 0.0025,        # pools.trade (Uniswap v4 launchpad) — 0.25%
    "uniswap-v4": 0.0025,
    "froth": 0.01,          # froth.meme / FrothSwap — 1%
    "robinpad": 0.01,       # launchpad graduates seed the 1% v3 tier
    "flap": 0.01,
    "uniswap-v2": 0.003,
    "uniswap-v3": 0.003,
}

GT_API = os.environ.get("GT_API", "https://api.geckoterminal.com/api/v2").rstrip("/")
GMGN_API = os.environ.get("GMGN_API", "https://gmgn.ai/defi/quotation/v1").rstrip("/")

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class ScanError(RuntimeError):
    """Raised when a scan cannot produce a trustworthy result."""


# ----------------------------- helpers --------------------------------------
def _f(value):
    """Coerce an API value to float, returning None for null/blank/garbage."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _dig(obj, path):
    """Walk a dotted path through nested dicts. Returns None if it dead-ends."""
    if not path:
        return obj
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _env_json(var):
    raw = os.environ.get(var)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as ex:
        raise ScanError(f"{var} is not valid JSON: {ex}") from ex


def fee_tiers():
    """Default per-DEX fee tiers, with LP_FEE_TIERS merged over them."""
    tiers = dict(DEX_FEE_DEFAULTS)
    for key, val in (_env_json("LP_FEE_TIERS") or {}).items():
        rate = _f(val)
        if rate is None or not 0 <= rate < 1:
            raise ScanError(f"LP_FEE_TIERS['{key}'] must be a fraction like 0.01")
        tiers[str(key).lower()] = rate
    return tiers


# ----------------------------- http -----------------------------------------
def new_session(impersonate=True):
    """A requests-compatible session.

    GMGN sits behind Cloudflare, which rejects plain-requests TLS fingerprints.
    curl_cffi impersonates a real Chrome handshake and is used when installed;
    otherwise we fall back to requests and surface a clear error if we are
    challenged, rather than silently returning nothing.
    """
    if impersonate:
        try:
            from curl_cffi import requests as cffi
            s = cffi.Session(impersonate="chrome")
            s.headers.update({"User-Agent": BROWSER_UA, "Accept": "application/json"})
            return s
        except ImportError:
            pass
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": BROWSER_UA,
    })
    return s


def _is_challenge(resp):
    ctype = (getattr(resp, "headers", None) or {}).get("Content-Type", "")
    return resp.status_code in (401, 403, 503) and "html" in ctype.lower()


def _request(session, url, params=None, attempts=4):
    """GET JSON with backoff on rate limits and server errors."""
    delay, last = 1.0, None
    for i in range(attempts):
        try:
            resp = session.get(url, params=params, timeout=20)
            if _is_challenge(resp):
                raise ScanError(
                    f"{url} answered with a bot challenge (HTTP {resp.status_code}). "
                    "Install curl_cffi (`pip install curl_cffi`) so the scanner can "
                    "present a real browser TLS fingerprint, or point GMGN_API at a "
                    "scraping proxy you control."
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
            elif resp.status_code == 404:
                return None
            else:
                resp.raise_for_status()
                return resp.json()
        except ScanError:
            raise
        except (requests.RequestException, ValueError) as ex:
            last = str(ex)
        if i < attempts - 1:
            time.sleep(delay)
            delay *= 2
    raise ScanError(f"GET {url} failed after {attempts} attempts: {last}")


# ----------------------------- GeckoTerminal ---------------------------------
def resolve_network(session, hint="robinhood"):
    """Find the GeckoTerminal network id for Robinhood Chain by name."""
    hint = hint.lower()
    partial = []
    for page in range(1, 11):
        payload = _request(session, f"{GT_API}/networks", {"page": page})
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
        f"correct network id (see {GT_API}/networks)."
    )


def fetch_pools(session, network, pages=10):
    """Top pools by 24h volume, plus the newest pools, deduped."""
    pools, included, seen = [], {}, set()
    endpoints = [(f"{GT_API}/networks/{network}/pools", {"sort": "h24_volume_usd_desc"}),
                 (f"{GT_API}/networks/{network}/new_pools", {})]
    for url, extra in endpoints:
        for page in range(1, pages + 1):
            payload = _request(session, url, {"include": "dex,base_token,quote_token",
                                              "page": page, **extra})
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


def derive_eth_usd(pools):
    """Median of price_usd / price_native across the feed.

    Every pool prices its base token in both USD and the chain's native
    currency, so the ratio is the native price. The median keeps one mispriced
    token from moving the result.
    """
    quotes = []
    for pool in pools:
        a = pool.get("attributes") or {}
        usd = _f(a.get("base_token_price_usd"))
        native = _f(a.get("base_token_price_native_currency"))
        if usd and native and usd > 0 and native > 0:
            quotes.append(usd / native)
    return statistics.median(quotes) if quotes else None


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


def pool_metrics(pool, included, tiers, window="h24"):
    """Flatten one GeckoTerminal pool into a normalized row."""
    a = pool.get("attributes") or {}
    dex_id = _rel_id(pool, "dex") or ""
    base_attrs = (included.get(_rel_id(pool, "base_token") or "") or {}).get("attributes") or {}

    mcap, mcap_source = _f(a.get("market_cap_usd")), "market_cap"
    if not mcap:
        # New launchpad tokens rarely have a CoinGecko-backed cap. FDV is the
        # same number when the whole supply circulates, the normal case here.
        mcap, mcap_source = _f(a.get("fdv_usd")), "fdv"

    fee_rate, fee_source = pool_fee_rate(a, dex_id, tiers)
    chain_prefix = (pool.get("id") or "_").split("_")[0]
    address = a.get("address")
    return {
        "source": "geckoterminal",
        "row_id": pool.get("id"),
        "pool_address": address,
        "pool_name": a.get("name"),
        "dex": dex_id,
        "symbol": base_attrs.get("symbol"),
        "token_name": base_attrs.get("name"),
        "token_address": base_attrs.get("address"),
        "price_usd": _f(a.get("base_token_price_usd")),
        "mcap_usd": mcap,
        "mcap_source": mcap_source,
        "volume_usd": _f((a.get("volume_usd") or {}).get(window)) or 0.0,
        "reserve_usd": _f(a.get("reserve_in_usd")),
        "fee_rate": fee_rate,
        "fee_source": fee_source,
        "price_change_h24": _f((a.get("price_change_percentage") or {}).get("h24")),
        "pool_created_at": a.get("pool_created_at"),
        "is_honeypot": None,
        "buy_tax": None,
        "sell_tax": None,
        "holder_count": None,
        "url": f"https://www.geckoterminal.com/{chain_prefix}/pools/{address}" if address else None,
    }


class GeckoTerminalSource:
    """Per-pool fee tiers and a self-derived ETH price. The measured option."""

    name = "geckoterminal"
    needs_eth_price = False

    def rows(self, session, window, cfg):
        network = cfg.get("network") or os.environ.get("RH_NETWORK") or cfg.get("cached_network")
        if not network:
            network = resolve_network(session)
        pools, included = fetch_pools(session, network, cfg.get("pages", 10))
        tiers = fee_tiers()
        rows = [pool_metrics(p, included, tiers, window) for p in pools]
        return rows, {"network": network, "eth_usd": derive_eth_usd(pools),
                      "eth_price_source": "derived"}


# ----------------------------- GMGN -----------------------------------------
GMGN_PERIODS = {"m5": "5m", "h1": "1h", "h6": "6h", "h24": "24h"}


def _gmgn_tokens(payload):
    """Unwrap GMGN's {code, msg, data:{rank:[...]}} envelope."""
    if not isinstance(payload, dict):
        raise ScanError("GMGN returned an unexpected payload")
    code = payload.get("code")
    if code not in (None, 0):
        raise ScanError(f"GMGN error {code}: {payload.get('msg') or 'unknown'}")
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("rank", "list", "tokens"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


class GmgnSource:
    """Widest Robinhood Chain coverage, plus honeypot and tax flags.

    GMGN does not report a pool fee tier, so fees here are volume x an assumed
    rate (--fee-rate, default 0.25%). Every row is labelled "assumed" to keep
    that visible: if the coin actually sits in a 1% pool, its real fees are 4x
    what this reports.
    """

    name = "gmgn"
    needs_eth_price = True

    def rows(self, session, window, cfg):
        chain = cfg.get("network") or os.environ.get("RH_NETWORK") or "robinhood"
        period = GMGN_PERIODS.get(window)
        if not period:
            raise ScanError(f"GMGN has no ranking window for '{window}'")
        fee_rate = cfg.get("fee_rate") or DEFAULT_ASSUMED_FEE
        url = f"{GMGN_API}/rank/{chain}/swaps/{period}"

        rows, seen = [], set()
        # Two orderings: the highest-volume coins are where the fees are, and
        # the highest-cap coins catch anything liquid that is quiet right now.
        for orderby in ("volume", "marketcap"):
            payload = _request(session, url, {
                "orderby": orderby, "direction": "desc",
                "filters[]": ["not_honeypot"],
            })
            for t in _gmgn_tokens(payload):
                address = t.get("address")
                if not address or address in seen:
                    continue
                seen.add(address)
                rows.append(self._row(t, chain, fee_rate))
            time.sleep(REQUEST_PAUSE)
        if not rows:
            raise ScanError(f"GMGN returned no tokens for chain '{chain}'")
        return rows, {"network": chain, "eth_usd": None}

    @staticmethod
    def _row(t, chain, fee_rate):
        address = t.get("address")
        return {
            "source": "gmgn",
            "row_id": f"gmgn_{address}",
            "pool_address": None,
            "pool_name": t.get("symbol"),
            "dex": t.get("launchpad") or t.get("dex") or None,
            "symbol": t.get("symbol"),
            "token_name": t.get("name") or t.get("symbol"),
            "token_address": address,
            "price_usd": _f(t.get("price")),
            "mcap_usd": _f(t.get("market_cap")),
            "mcap_source": "gmgn",
            "volume_usd": _f(t.get("volume")) or 0.0,
            "reserve_usd": _f(t.get("liquidity")),
            "fee_rate": fee_rate,
            "fee_source": f"assumed:{fee_rate * 100:.2f}%",
            "price_change_h24": _f(t.get("price_change_percent")),
            "pool_created_at": t.get("open_timestamp"),
            "is_honeypot": bool(t.get("is_honeypot")) if t.get("is_honeypot") is not None else None,
            "buy_tax": _f(t.get("buy_tax")),
            "sell_tax": _f(t.get("sell_tax")),
            "holder_count": _f(t.get("holder_count")),
            "url": f"https://gmgn.ai/{chain}/token/{address}" if address else None,
        }


# ----------------------------- custom / FOMO ---------------------------------
CUSTOM_DEFAULT_MAP = {
    "symbol": "symbol",
    "token_name": "name",
    "token_address": "address",
    "price_usd": "price",
    "mcap_usd": "market_cap",
    "volume_usd": "volume",
    "reserve_usd": "liquidity",
}


class CustomSource:
    """Any JSON endpoint that returns a list of tokens.

    This is the FOMO path: FOMO publishes no API documentation, so rather than
    guess at endpoints, point this at the request the site actually makes (copy
    it out of the browser network tab) and map the field names.

        LP_SOURCE_URL    the endpoint to GET
        LP_SOURCE_ITEMS  dotted path to the list, e.g. "data.tokens" ("" = root)
        LP_SOURCE_MAP    JSON: normalized field -> that API's field name
        LP_SOURCE_PARAMS JSON: query params to send
    """

    name = "custom"
    needs_eth_price = True

    def rows(self, session, window, cfg):
        url = cfg.get("source_url") or os.environ.get("LP_SOURCE_URL")
        if not url:
            raise ScanError(
                "the 'custom' source needs LP_SOURCE_URL (and usually LP_SOURCE_MAP). "
                "Open the site, copy the token-list request from the browser network "
                "tab, and point LP_SOURCE_URL at it."
            )
        mapping = {**CUSTOM_DEFAULT_MAP, **(_env_json("LP_SOURCE_MAP") or {})}
        params = _env_json("LP_SOURCE_PARAMS") or {}
        items_path = os.environ.get("LP_SOURCE_ITEMS", "data")

        payload = _request(session, url, params)
        items = _dig(payload, items_path)
        if not isinstance(items, list):
            raise ScanError(
                f"LP_SOURCE_ITEMS='{items_path}' did not resolve to a list. "
                f"Top-level keys were: {sorted(payload)[:10] if isinstance(payload, dict) else type(payload).__name__}"
            )

        fee_rate = cfg.get("fee_rate") or DEFAULT_ASSUMED_FEE
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue

            def get(field, _item=item):
                path = mapping.get(field)
                return _dig(_item, path) if path else None

            address = get("token_address")
            rows.append({
                "source": "custom",
                "row_id": f"custom_{address or len(rows)}",
                "pool_address": None,
                "pool_name": get("symbol"),
                "dex": None,
                "symbol": get("symbol"),
                "token_name": get("token_name") or get("symbol"),
                "token_address": address,
                "price_usd": _f(get("price_usd")),
                "mcap_usd": _f(get("mcap_usd")),
                "mcap_source": "custom",
                "volume_usd": _f(get("volume_usd")) or 0.0,
                "reserve_usd": _f(get("reserve_usd")),
                "fee_rate": fee_rate,
                "fee_source": f"assumed:{fee_rate * 100:.2f}%",
                "price_change_h24": None,
                "pool_created_at": None,
                "is_honeypot": None,
                "buy_tax": None,
                "sell_tax": None,
                "holder_count": None,
                "url": None,
            })
        if not rows:
            raise ScanError(f"{url} returned no usable rows")
        return rows, {"network": cfg.get("network") or "custom", "eth_usd": None}


SOURCES = {s.name: s for s in (GmgnSource, GeckoTerminalSource, CustomSource)}


def get_source(name):
    try:
        return SOURCES[name]()
    except KeyError:
        raise ScanError(
            f"unknown source '{name}'. Available: {', '.join(sorted(SOURCES))}"
        ) from None
