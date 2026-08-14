"""
Offline tests for the LP trigger.

Every API is stubbed, so these run anywhere with no network and no key. They pin
down the arithmetic the trigger stands on — fee derivation, the market-cap
fallback, the ETH price precedence chain, the honeypot veto and the pass/fail
boundaries — across all three sources.

    pytest test_lp_scanner.py
"""

import json

import pytest

import lp_scanner as lp
import lp_sources as src

ETH_USD = 3000.0


# ----------------------------- fixtures -------------------------------------
def make_pool(pool_id, symbol, *, volume_h24, mcap=None, fdv=None, reserve=100_000.0,
              price_usd=0.001, dex="pools-trade", fee_pct=None, eth_usd=ETH_USD):
    """A pool payload shaped like GeckoTerminal's /pools response."""
    attrs = {
        "address": f"0x{pool_id}",
        "name": f"{symbol} / WETH",
        "base_token_price_usd": str(price_usd),
        "base_token_price_native_currency": str(price_usd / eth_usd),
        "market_cap_usd": None if mcap is None else str(mcap),
        "fdv_usd": None if fdv is None else str(fdv),
        "reserve_in_usd": str(reserve),
        "volume_usd": {"m5": "0", "h1": "0", "h6": "0", "h24": str(volume_h24)},
        "price_change_percentage": {"h24": "12.5"},
        "pool_created_at": "2026-08-01T00:00:00Z",
    }
    if fee_pct is not None:
        attrs["pool_fee_percentage"] = str(fee_pct)
    return {
        "id": f"robinhood_0x{pool_id}",
        "type": "pool",
        "attributes": attrs,
        "relationships": {
            "dex": {"data": {"id": dex, "type": "dex"}},
            "base_token": {"data": {"id": f"robinhood_t{pool_id}", "type": "token"}},
        },
    }


def token_included(pool_id, symbol):
    return {
        "id": f"robinhood_t{pool_id}",
        "type": "token",
        "attributes": {"symbol": symbol, "name": f"{symbol} Token",
                       "address": f"0xt{pool_id}"},
    }


def gmgn_token(symbol, *, market_cap, volume, liquidity=100_000.0,
               is_honeypot=False, price=0.001, address=None):
    """A token entry shaped like GMGN's rank/swaps response."""
    return {
        "address": address or f"0x{symbol.lower()}",
        "chain": "robinhood",
        "symbol": symbol,
        "name": f"{symbol} Token",
        "price": price,
        "market_cap": market_cap,
        "volume": volume,
        "liquidity": liquidity,
        "holder_count": 500,
        "is_honeypot": is_honeypot,
        "buy_tax": 0,
        "sell_tax": 0,
        "price_change_percent": 42.0,
    }


def gmgn_envelope(tokens, code=0, msg="success"):
    return {"code": code, "msg": msg, "data": {"rank": list(tokens)}}


def vol_for_fees(fees_eth, fee_rate=0.0025, eth_usd=ETH_USD):
    """Volume that produces exactly `fees_eth` of fees at a given fee tier."""
    return fees_eth * eth_usd / fee_rate


class FakeResponse:
    def __init__(self, payload, status_code=200, content_type="application/json"):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise src.requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Routes by URL shape: GT networks, GT pools, GMGN rank, or a custom feed."""

    def __init__(self, pools=(), included=(), networks=None, gmgn=None, custom=None):
        self.pools, self.included = list(pools), list(included)
        self.networks, self.gmgn, self.custom = networks, gmgn, custom
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        if url.endswith("/networks"):
            page = int(params.get("page", 1))
            return FakeResponse({"data": (self.networks or []) if page == 1 else []})
        if "/rank/" in url:
            return FakeResponse(self.gmgn)
        if self.custom is not None:
            return FakeResponse(self.custom)
        if int(params.get("page", 1)) > 1:
            return FakeResponse({"data": [], "included": []})
        return FakeResponse({"data": self.pools, "included": self.included})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(src.time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ETH_USD", "RH_NETWORK", "LP_FEE_TIERS", "GT_API", "GMGN_API",
                "LP_SOURCE", "LP_SOURCE_URL", "LP_SOURCE_MAP", "LP_SOURCE_ITEMS",
                "LP_SOURCE_PARAMS", "ETH_PRICE_URL", "ETH_PRICE_PATH"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "hits.json")


# ----------------------------- coercion -------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1.5", 1.5), (2, 2.0), (None, None), ("", None), ("abc", None),
    ("nan", None), ("inf", None), (True, None), (False, None),
])
def test_f_coerces_api_values(raw, expected):
    assert src._f(raw) == expected


@pytest.mark.parametrize("path,expected", [
    ("a.b", 1), ("a", {"b": 1}), ("a.missing", None), ("nope.deep", None),
])
def test_dig_walks_dotted_paths(path, expected):
    assert src._dig({"a": {"b": 1}}, path) == expected


# ----------------------------- fee tiers ------------------------------------
def test_api_fee_percentage_wins_over_defaults():
    rate, source = src.pool_fee_rate({"pool_fee_percentage": "1"}, "pools-trade", src.fee_tiers())
    assert (rate, source) == (0.01, "api")


def test_dex_default_used_when_api_omits_the_fee():
    rate, source = src.pool_fee_rate({}, "froth-meme", src.fee_tiers())
    assert (rate, source) == (0.01, "default:froth")


def test_unknown_dex_falls_back():
    rate, source = src.pool_fee_rate({}, "some-new-dex", src.fee_tiers())
    assert (rate, source) == (src.FALLBACK_FEE, "fallback")


def test_fee_tiers_env_override(monkeypatch):
    monkeypatch.setenv("LP_FEE_TIERS", json.dumps({"some-new-dex": 0.005}))
    rate, source = src.pool_fee_rate({}, "some-new-dex", src.fee_tiers())
    assert (rate, source) == (0.005, "default:some-new-dex")


@pytest.mark.parametrize("bad", [5, -0.1, "abc"])
def test_bad_fee_tier_override_is_rejected(monkeypatch, bad):
    monkeypatch.setenv("LP_FEE_TIERS", json.dumps({"x": bad}))
    with pytest.raises(src.ScanError, match="fraction"):
        src.fee_tiers()


def test_malformed_fee_tier_json_is_rejected(monkeypatch):
    monkeypatch.setenv("LP_FEE_TIERS", "{not json")
    with pytest.raises(src.ScanError, match="not valid JSON"):
        src.fee_tiers()


# ----------------------------- geckoterminal --------------------------------
def test_eth_price_derived_as_median_of_pool_quotes():
    pools = [make_pool("a", "AAA", volume_h24=1),
             make_pool("b", "BBB", volume_h24=1, price_usd=0.05),
             make_pool("c", "CCC", volume_h24=1, price_usd=12.0)]
    assert src.derive_eth_usd(pools) == pytest.approx(ETH_USD)


def test_eth_price_median_ignores_one_mispriced_pool():
    pools = [make_pool("a", "AAA", volume_h24=1),
             make_pool("b", "BBB", volume_h24=1),
             make_pool("c", "CCC", volume_h24=1, eth_usd=99_000.0)]
    assert src.derive_eth_usd(pools) == pytest.approx(ETH_USD)


def test_eth_price_none_when_no_usable_quotes():
    broken = make_pool("a", "AAA", volume_h24=1)
    broken["attributes"]["base_token_price_native_currency"] = "0"
    assert src.derive_eth_usd([broken]) is None


def test_market_cap_falls_back_to_fdv_when_absent():
    row = src.pool_metrics(make_pool("a", "AAA", volume_h24=1, mcap=None, fdv=1_000_000),
                           {}, src.fee_tiers())
    assert (row["mcap_usd"], row["mcap_source"]) == (1_000_000, "fdv")


def test_real_market_cap_preferred_over_fdv():
    row = src.pool_metrics(make_pool("a", "AAA", volume_h24=1, mcap=800_000, fdv=9_000_000),
                           {}, src.fee_tiers())
    assert (row["mcap_usd"], row["mcap_source"]) == (800_000, "market_cap")


def test_token_symbol_pulled_from_included_payload():
    row = src.pool_metrics(make_pool("a", "AAA", volume_h24=1, mcap=1),
                           {"robinhood_ta": token_included("a", "AAA")}, src.fee_tiers())
    assert (row["symbol"], row["token_name"]) == ("AAA", "AAA Token")


def test_missing_volume_is_zero_not_a_crash():
    pool = make_pool("a", "AAA", volume_h24=1, mcap=1)
    pool["attributes"]["volume_usd"] = {}
    assert src.pool_metrics(pool, {}, src.fee_tiers())["volume_usd"] == 0.0


def test_resolve_network_matches_on_name():
    session = FakeSession(networks=[
        {"id": "eth", "attributes": {"name": "Ethereum"}},
        {"id": "rhc", "attributes": {"name": "Robinhood Chain"}},
    ])
    assert src.resolve_network(session) == "rhc"


def test_resolve_network_errors_when_absent():
    session = FakeSession(networks=[{"id": "eth", "attributes": {"name": "Ethereum"}}])
    with pytest.raises(src.ScanError, match="RH_NETWORK"):
        src.resolve_network(session)


# ----------------------------- scoring --------------------------------------
def test_fees_come_from_volume_times_fee_tier():
    row = lp.score_row({"volume_usd": 1_200_000, "fee_rate": 0.0025, "reserve_usd": None},
                       ETH_USD)
    assert row["fees_usd"] == pytest.approx(3_000.0)
    assert row["fees_eth"] == pytest.approx(1.0)


def test_fee_apr_is_annualised_against_reserves():
    row = lp.score_row({"volume_usd": 1_200_000, "fee_rate": 0.0025, "reserve_usd": 365_000},
                       ETH_USD)
    # 3000/day on 365k of reserves -> 1,095,000/yr -> 300%
    assert row["fee_apr"] == pytest.approx(300.0)


def test_fee_apr_is_none_without_reserves():
    assert lp.score_row({"volume_usd": 1, "fee_rate": 0.01, "reserve_usd": 0}, ETH_USD)["fee_apr"] is None


# ----------------------------- thresholds -----------------------------------
@pytest.mark.parametrize("mcap,fees_eth,expected", [
    (500_001, 0.51, True),      # clears both
    (500_000, 1.00, False),     # mcap exactly at the line -> strictly greater
    (500_001, 0.50, False),     # fees exactly at the line -> strictly greater
    (499_999, 1.00, False),     # mcap short
    (900_000, 0.49, False),     # fees short
    (None, 1.00, False),        # no mcap at all
    (900_000, None, False),     # no fee figure
])
def test_threshold_boundaries(mcap, fees_eth, expected):
    assert lp.passes({"mcap_usd": mcap, "fees_eth": fees_eth}) is expected


def test_thresholds_are_tunable():
    row = {"mcap_usd": 600_000, "fees_eth": 0.2}
    assert lp.passes(row) is False
    assert lp.passes(row, min_mcap=100_000, min_fees_eth=0.1) is True


def test_honeypot_is_vetoed_even_when_it_clears_both_gates():
    row = {"mcap_usd": 5_000_000, "fees_eth": 9.0, "is_honeypot": True}
    assert lp.passes(row) is False
    assert lp.passes(row, allow_honeypot=True) is True


# ----------------------------- eth price precedence -------------------------
def test_eth_price_flag_beats_everything(monkeypatch):
    monkeypatch.setenv("ETH_USD", "1111")
    assert lp.resolve_eth_usd(None, {"eth_usd": 2222}, pinned=4200.0) == (4200.0, "flag")


def test_eth_price_env_beats_derived(monkeypatch):
    monkeypatch.setenv("ETH_USD", "1111")
    assert lp.resolve_eth_usd(None, {"eth_usd": 2222}) == (1111.0, "env")


def test_eth_price_falls_back_to_source_derived():
    price, source = lp.resolve_eth_usd(None, {"eth_usd": 2222, "eth_price_source": "derived"})
    assert (price, source) == (2222.0, "derived")


def test_eth_price_from_configured_url(monkeypatch):
    monkeypatch.setenv("ETH_PRICE_URL", "https://prices.example/eth")
    monkeypatch.setenv("ETH_PRICE_PATH", "data.usd")
    session = FakeSession(custom={"data": {"usd": 3456.0}})
    assert lp.resolve_eth_usd(session, {"eth_usd": None}) == (3456.0, "url")


def test_eth_price_url_with_unusable_path_errors(monkeypatch):
    monkeypatch.setenv("ETH_PRICE_URL", "https://prices.example/eth")
    monkeypatch.setenv("ETH_PRICE_PATH", "nope")
    with pytest.raises(lp.ScanError, match="ETH_PRICE_PATH"):
        lp.resolve_eth_usd(FakeSession(custom={"data": {"usd": 1}}), {"eth_usd": None})


def test_missing_eth_price_is_a_clear_error():
    with pytest.raises(lp.ScanError, match="--eth-usd"):
        lp.resolve_eth_usd(None, {"eth_usd": None})


# ----------------------------- geckoterminal end to end ---------------------
def build_gt_board():
    """Four pools: two should fire the trigger, two should not."""
    pools = [
        make_pool("a", "AAA", volume_h24=vol_for_fees(1.0), mcap=2_000_000),
        make_pool("b", "BBB", volume_h24=vol_for_fees(1.0), mcap=400_000),
        make_pool("c", "CCC", volume_h24=100_000, mcap=5_000_000),
        make_pool("d", "DDD", volume_h24=vol_for_fees(0.6), mcap=None, fdv=1_000_000),
    ]
    included = [token_included(p, s) for p, s in
                (("a", "AAA"), ("b", "BBB"), ("c", "CCC"), ("d", "DDD"))]
    return pools, included


def gt_scan(session, state_path, **kw):
    return lp.scan(source="geckoterminal", network="robinhood", pages=1,
                   session=session, state_path=state_path, **kw)


def test_gt_scan_returns_only_qualifying_pools_sorted_by_fees(state_path):
    pools, included = build_gt_board()
    result = gt_scan(FakeSession(pools, included), state_path)

    assert [h["symbol"] for h in result["hits"]] == ["AAA", "DDD"]
    assert result["hits"][0]["fees_eth"] == pytest.approx(1.0)
    assert result["hits"][1]["fees_eth"] == pytest.approx(0.6)
    assert result["scanned"] == 4
    assert result["eth_usd"] == pytest.approx(ETH_USD)
    assert result["eth_price_source"] == "derived"
    assert result["source"] == "geckoterminal"


def test_gt_reports_a_measured_fee_tier_not_an_assumed_one(state_path):
    pools, included = build_gt_board()
    pools[0]["attributes"]["pool_fee_percentage"] = "1"
    hit = gt_scan(FakeSession(pools, included), state_path)["hits"][0]
    assert hit["fee_source"] == "api"
    assert hit["fee_rate"] == 0.01


def test_window_switch_changes_the_verdict(state_path):
    """A pool with h24 volume but none in h6 passes on h24 and fails on h6."""
    pool = make_pool("a", "AAA", volume_h24=vol_for_fees(1.0), mcap=2_000_000)
    pool["attributes"]["volume_usd"]["h6"] = "0"
    session = FakeSession([pool], [token_included("a", "AAA")])

    assert len(gt_scan(session, state_path)["hits"]) == 1
    assert len(gt_scan(session, state_path, window="h6")["hits"]) == 0


def test_empty_network_is_reported_not_silently_empty(state_path):
    with pytest.raises(lp.ScanError, match="no pools"):
        gt_scan(FakeSession([]), state_path)


# ----------------------------- gmgn -----------------------------------------
def build_gmgn_board():
    """Mirrors the GeckoTerminal board so both sources are judged alike."""
    return gmgn_envelope([
        gmgn_token("AAA", market_cap=2_000_000, volume=vol_for_fees(1.0)),
        gmgn_token("BBB", market_cap=400_000, volume=vol_for_fees(1.0)),
        gmgn_token("CCC", market_cap=5_000_000, volume=100_000),
        gmgn_token("DDD", market_cap=1_000_000, volume=vol_for_fees(0.6)),
    ])


def gmgn_scan(session, state_path, **kw):
    kw.setdefault("eth_usd", ETH_USD)
    return lp.scan(source="gmgn", session=session, state_path=state_path, **kw)


def test_gmgn_scan_applies_the_same_gates(state_path):
    result = gmgn_scan(FakeSession(gmgn=build_gmgn_board()), state_path)
    assert [h["symbol"] for h in result["hits"]] == ["AAA", "DDD"]
    assert result["hits"][0]["fees_eth"] == pytest.approx(1.0)
    assert result["source"] == "gmgn"
    assert result["network"] == "robinhood"


def test_gmgn_fee_rate_is_labelled_as_assumed(state_path):
    hit = gmgn_scan(FakeSession(gmgn=build_gmgn_board()), state_path)["hits"][0]
    assert hit["fee_rate"] == src.DEFAULT_ASSUMED_FEE
    assert hit["fee_source"] == "assumed:0.25%"


def test_gmgn_fee_rate_override_changes_the_fee_maths(state_path):
    """At 1% instead of 0.25%, the same volume yields four times the fees.

    This is the assumption that matters most on GMGN: guess the tier wrong and
    every fee figure is off by that multiple. A coin sitting just under the bar
    at 0.25% clears it at 1%.
    """
    board = gmgn_envelope([
        gmgn_token("AAA", market_cap=2_000_000, volume=vol_for_fees(1.0)),
        # 0.4 ETH of fees at 0.25%, so it misses; at 1% it makes 1.6 and passes
        gmgn_token("EDGE", market_cap=2_000_000, volume=vol_for_fees(0.4)),
    ])

    quarter = gmgn_scan(FakeSession(gmgn=board), state_path)
    assert [h["symbol"] for h in quarter["hits"]] == ["AAA"]

    full = gmgn_scan(FakeSession(gmgn=board), state_path, fee_rate=0.01)
    by_symbol = {h["symbol"]: h for h in full["hits"]}
    assert by_symbol["AAA"]["fees_eth"] == pytest.approx(4.0)
    assert by_symbol["AAA"]["fee_source"] == "assumed:1.00%"
    assert by_symbol["EDGE"]["fees_eth"] == pytest.approx(1.6)


def test_gmgn_honeypot_is_dropped_by_default(state_path):
    board = gmgn_envelope([
        gmgn_token("TRAP", market_cap=9_000_000, volume=vol_for_fees(5.0), is_honeypot=True),
        gmgn_token("AAA", market_cap=2_000_000, volume=vol_for_fees(1.0)),
    ])
    assert [h["symbol"] for h in gmgn_scan(FakeSession(gmgn=board), state_path)["hits"]] == ["AAA"]
    allowed = gmgn_scan(FakeSession(gmgn=board), state_path, allow_honeypot=True)
    assert [h["symbol"] for h in allowed["hits"]] == ["TRAP", "AAA"]


def test_gmgn_dedupes_across_both_orderings(state_path):
    """Both the volume and marketcap passes return the same coin once."""
    session = FakeSession(gmgn=build_gmgn_board())
    result = gmgn_scan(session, state_path)
    assert result["scanned"] == 4
    assert len([c for c in session.calls if "/rank/" in c[0]]) == 2


def test_gmgn_error_envelope_is_surfaced(state_path):
    board = gmgn_envelope([], code=40001, msg="rate limited")
    with pytest.raises(lp.ScanError, match="rate limited"):
        gmgn_scan(FakeSession(gmgn=board), state_path)


def test_gmgn_empty_chain_is_reported(state_path):
    with pytest.raises(lp.ScanError, match="no tokens"):
        gmgn_scan(FakeSession(gmgn=gmgn_envelope([])), state_path)


def test_gmgn_without_an_eth_price_refuses_to_guess(state_path):
    with pytest.raises(lp.ScanError, match="does not report an ETH price"):
        lp.scan(source="gmgn", session=FakeSession(gmgn=build_gmgn_board()),
                state_path=state_path)


def test_gmgn_unsupported_window(state_path):
    with pytest.raises(lp.ScanError, match="no ranking window"):
        gmgn_scan(FakeSession(gmgn=build_gmgn_board()), state_path, window="h2")


def test_cloudflare_challenge_gives_actionable_advice():
    class Challenged(FakeSession):
        def get(self, url, params=None, timeout=None):
            return FakeResponse("<html>attention required</html>", 403, "text/html")

    with pytest.raises(src.ScanError, match="curl_cffi"):
        src._request(Challenged(), "https://gmgn.ai/defi/quotation/v1/rank/robinhood/swaps/24h")


# ----------------------------- custom / FOMO --------------------------------
def test_custom_source_maps_arbitrary_field_names(monkeypatch, state_path):
    monkeypatch.setenv("LP_SOURCE_URL", "https://fomo.example/api/tokens")
    monkeypatch.setenv("LP_SOURCE_ITEMS", "result.tokens")
    monkeypatch.setenv("LP_SOURCE_MAP", json.dumps({
        "symbol": "ticker", "mcap_usd": "stats.marketCap",
        "volume_usd": "stats.volume24h", "reserve_usd": "stats.liquidityUsd",
        "token_address": "contract",
    }))
    feed = {"result": {"tokens": [
        {"ticker": "AAA", "contract": "0xaaa",
         "stats": {"marketCap": 2_000_000, "volume24h": vol_for_fees(1.0),
                   "liquidityUsd": 250_000}},
        {"ticker": "BBB", "contract": "0xbbb",
         "stats": {"marketCap": 100_000, "volume24h": vol_for_fees(1.0),
                   "liquidityUsd": 250_000}},
    ]}}

    result = lp.scan(source="custom", session=FakeSession(custom=feed),
                     state_path=state_path, eth_usd=ETH_USD)
    assert [h["symbol"] for h in result["hits"]] == ["AAA"]
    assert result["hits"][0]["fees_eth"] == pytest.approx(1.0)
    assert result["hits"][0]["reserve_usd"] == 250_000


def test_custom_source_without_a_url_explains_itself(state_path):
    with pytest.raises(lp.ScanError, match="LP_SOURCE_URL"):
        lp.scan(source="custom", session=FakeSession(custom={}),
                state_path=state_path, eth_usd=ETH_USD)


def test_custom_source_bad_items_path_names_the_keys(monkeypatch, state_path):
    monkeypatch.setenv("LP_SOURCE_URL", "https://fomo.example/api/tokens")
    monkeypatch.setenv("LP_SOURCE_ITEMS", "wrong.path")
    with pytest.raises(lp.ScanError, match="did not resolve to a list"):
        lp.scan(source="custom", session=FakeSession(custom={"result": {"tokens": []}}),
                state_path=state_path, eth_usd=ETH_USD)


def test_unknown_source_is_rejected():
    with pytest.raises(src.ScanError, match="unknown source"):
        src.get_source("nope")


# ----------------------------- state ----------------------------------------
def test_first_seen_and_new_flag_survive_across_runs(state_path):
    pools, included = build_gt_board()

    first = gt_scan(FakeSession(pools, included), state_path)
    assert all(h["is_new"] and h["runs_passed"] == 1 for h in first["hits"])
    lp.save_state(first, state_path)

    second = gt_scan(FakeSession(pools, included), state_path)
    assert not any(h["is_new"] for h in second["hits"])
    assert all(h["runs_passed"] == 2 for h in second["hits"])
    assert second["hits"][0]["first_seen"] == first["hits"][0]["first_seen"]
    assert [run["hit_count"] for run in second["history"]] == [2, 2]


def test_switching_source_resets_streaks_instead_of_mixing_them(state_path):
    pools, included = build_gt_board()
    lp.save_state(gt_scan(FakeSession(pools, included), state_path), state_path)

    switched = gmgn_scan(FakeSession(gmgn=build_gmgn_board()), state_path)
    assert all(h["is_new"] and h["runs_passed"] == 1 for h in switched["hits"])
    assert len(switched["history"]) == 1


def test_scan_reuses_the_network_from_previous_state(state_path):
    pools, included = build_gt_board()
    lp.save_state({"source": "geckoterminal", "network": "rhc-cached", "hits": []}, state_path)

    session = FakeSession(pools, included)
    result = lp.scan(source="geckoterminal", pages=1, session=session, state_path=state_path)

    assert result["network"] == "rhc-cached"
    assert not any(url.endswith("/networks") for url, _ in session.calls)


def test_cached_network_is_not_reused_across_sources(state_path):
    lp.save_state({"source": "gmgn", "network": "robinhood", "hits": []}, state_path)
    session = FakeSession(*build_gt_board(), networks=[
        {"id": "rhc", "attributes": {"name": "Robinhood Chain"}}])

    result = lp.scan(source="geckoterminal", pages=1, session=session, state_path=state_path)

    assert result["network"] == "rhc"
    assert any(url.endswith("/networks") for url, _ in session.calls)


def test_history_is_capped(state_path):
    pools, included = build_gt_board()
    lp.save_state({"source": "geckoterminal", "network": "rhc",
                   "history": [{"at": str(i), "scanned": 0, "hit_count": 0, "hits": []}
                               for i in range(lp.HISTORY_LIMIT + 20)]}, state_path)
    result = lp.scan(source="geckoterminal", pages=1,
                     session=FakeSession(pools, included), state_path=state_path)
    assert len(result["history"]) == lp.HISTORY_LIMIT


def test_state_file_write_is_atomic_and_reloadable(tmp_path, state_path):
    pools, included = build_gt_board()
    lp.save_state(gt_scan(FakeSession(pools, included), state_path), state_path)
    assert lp.load_state(state_path)["hits"][0]["symbol"] == "AAA"
    assert not (tmp_path / "hits.json.tmp").exists()


def test_corrupt_state_file_does_not_break_the_scan(tmp_path):
    state = tmp_path / "hits.json"
    state.write_text("{not json")
    pools, included = build_gt_board()
    assert len(gt_scan(FakeSession(pools, included), str(state))["hits"]) == 2


# ----------------------------- retries --------------------------------------
def test_rate_limit_is_retried_then_succeeds():
    class Flaky(FakeSession):
        attempts = 0

        def get(self, url, params=None, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                return FakeResponse({}, status_code=429)
            return super().get(url, params, timeout)

    session = Flaky(gmgn=gmgn_envelope([gmgn_token("AAA", market_cap=1, volume=1)]))
    payload = src._request(session, "https://gmgn.ai/x/rank/robinhood/swaps/24h")
    assert payload["data"]["rank"][0]["symbol"] == "AAA"
    assert session.attempts == 2


def test_persistent_failure_raises_scan_error():
    class Dead(FakeSession):
        def get(self, url, params=None, timeout=None):
            return FakeResponse({}, status_code=503)

    with pytest.raises(src.ScanError, match="failed after"):
        src._request(Dead(), "https://example.test/x")


# ----------------------------- cli ------------------------------------------
def test_cli_writes_state_and_reports_hits(monkeypatch, capsys, state_path):
    pools, included = build_gt_board()
    monkeypatch.setattr(lp_sources_new_session_target(), lambda *a, **k: FakeSession(pools, included))

    rc = lp.main(["--source", "geckoterminal", "--network", "robinhood",
                  "--pages", "1", "--state", state_path])

    assert rc == 0
    assert "AAA" in capsys.readouterr().out
    assert len(lp.load_state(state_path)["hits"]) == 2


def test_cli_reports_failure_on_stderr(monkeypatch, capsys, state_path):
    monkeypatch.setattr(lp_sources_new_session_target(), lambda *a, **k: FakeSession([]))
    rc = lp.main(["--source", "geckoterminal", "--network", "robinhood",
                  "--pages", "1", "--state", state_path])
    assert rc == 1
    assert "scan failed" in capsys.readouterr().err


def lp_sources_new_session_target():
    """monkeypatch target for the session factory both modules share."""
    return "lp_sources.new_session"
