"""
Offline tests for the LP trigger scanner.

The GeckoTerminal API is stubbed, so these run anywhere with no network and no
key. They pin down the arithmetic the trigger stands on: fee derivation, the
market-cap fallback, the ETH price derivation, and the pass/fail boundaries.

    pytest test_lp_scanner.py
"""

import json

import pytest

import lp_scanner as lp

ETH_USD = 3000.0


# ----------------------------- fixtures -------------------------------------
def make_pool(pool_id, symbol, *, volume_h24, mcap=None, fdv=None, reserve=100_000.0,
              price_usd=0.001, dex="pools-trade", fee_pct=None, eth_usd=ETH_USD):
    """Build a pool payload shaped like GeckoTerminal's /pools response."""
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


def vol_for_fees(fees_eth, fee_rate=0.0025, eth_usd=ETH_USD):
    """Volume that produces exactly `fees_eth` of fees at a given fee tier."""
    return fees_eth * eth_usd / fee_rate


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise lp.requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Serves one page of pools, then empty pages, for every endpoint."""

    def __init__(self, pools, included=(), networks=None):
        self.pools = pools
        self.included = list(included)
        self.networks = networks
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        page = int(params.get("page", 1))
        if url.endswith("/networks"):
            rows = self.networks if page == 1 else []
            return FakeResponse({"data": rows or []})
        if page > 1:
            return FakeResponse({"data": [], "included": []})
        return FakeResponse({"data": self.pools, "included": self.included})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(lp.time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ETH_USD", "RH_NETWORK", "LP_FEE_TIERS", "GT_API"):
        monkeypatch.delenv(var, raising=False)


# ----------------------------- unit: coercion -------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1.5", 1.5), (2, 2.0), (None, None), ("", None), ("abc", None),
    ("nan", None), ("inf", None),
])
def test_f_coerces_api_values(raw, expected):
    assert lp._f(raw) == expected


# ----------------------------- unit: eth price ------------------------------
def test_eth_price_derived_as_median_of_pool_quotes():
    pools = [make_pool("a", "AAA", volume_h24=1),
             make_pool("b", "BBB", volume_h24=1, price_usd=0.05),
             make_pool("c", "CCC", volume_h24=1, price_usd=12.0)]
    price, source = lp.derive_eth_usd(pools)
    assert price == pytest.approx(ETH_USD)
    assert source == "derived"


def test_eth_price_median_ignores_one_mispriced_pool():
    pools = [make_pool("a", "AAA", volume_h24=1),
             make_pool("b", "BBB", volume_h24=1),
             make_pool("c", "CCC", volume_h24=1, eth_usd=99_000.0)]
    price, _ = lp.derive_eth_usd(pools)
    assert price == pytest.approx(ETH_USD)


def test_eth_price_can_be_pinned_by_env(monkeypatch):
    monkeypatch.setenv("ETH_USD", "4200")
    price, source = lp.derive_eth_usd([make_pool("a", "AAA", volume_h24=1)])
    assert (price, source) == (4200.0, "env")


def test_eth_price_without_usable_quotes_is_an_error():
    broken = make_pool("a", "AAA", volume_h24=1)
    broken["attributes"]["base_token_price_native_currency"] = "0"
    with pytest.raises(lp.ScanError, match="ETH price"):
        lp.derive_eth_usd([broken])


# ----------------------------- unit: fee tiers ------------------------------
def test_api_fee_percentage_wins_over_defaults():
    attrs = {"pool_fee_percentage": "1"}
    rate, source = lp.pool_fee_rate(attrs, "pools-trade", lp.fee_tiers())
    assert (rate, source) == (0.01, "api")


def test_dex_default_used_when_api_omits_the_fee():
    rate, source = lp.pool_fee_rate({}, "froth-meme", lp.fee_tiers())
    assert rate == 0.01
    assert source == "default:froth"


def test_unknown_dex_falls_back():
    rate, source = lp.pool_fee_rate({}, "some-new-dex", lp.fee_tiers())
    assert (rate, source) == (lp.FALLBACK_FEE, "fallback")


def test_fee_tiers_env_override(monkeypatch):
    monkeypatch.setenv("LP_FEE_TIERS", json.dumps({"some-new-dex": 0.005}))
    rate, source = lp.pool_fee_rate({}, "some-new-dex", lp.fee_tiers())
    assert (rate, source) == (0.005, "default:some-new-dex")


def test_bad_fee_tier_override_is_rejected(monkeypatch):
    monkeypatch.setenv("LP_FEE_TIERS", json.dumps({"x": 5}))
    with pytest.raises(lp.ScanError, match="fraction"):
        lp.fee_tiers()


# ----------------------------- unit: metrics --------------------------------
def test_fees_come_from_volume_times_fee_tier():
    pool = make_pool("a", "AAA", volume_h24=1_200_000, mcap=2_000_000)
    row = lp.pool_metrics(pool, {}, ETH_USD, lp.fee_tiers())
    assert row["fee_rate"] == 0.0025
    assert row["fees_usd"] == pytest.approx(3_000.0)
    assert row["fees_eth"] == pytest.approx(1.0)


def test_market_cap_falls_back_to_fdv_when_absent():
    pool = make_pool("a", "AAA", volume_h24=1, mcap=None, fdv=1_000_000)
    row = lp.pool_metrics(pool, {}, ETH_USD, lp.fee_tiers())
    assert row["mcap_usd"] == 1_000_000
    assert row["mcap_source"] == "fdv"


def test_real_market_cap_preferred_over_fdv():
    pool = make_pool("a", "AAA", volume_h24=1, mcap=800_000, fdv=9_000_000)
    row = lp.pool_metrics(pool, {}, ETH_USD, lp.fee_tiers())
    assert row["mcap_usd"] == 800_000
    assert row["mcap_source"] == "market_cap"


def test_fee_apr_is_annualised_against_reserves():
    pool = make_pool("a", "AAA", volume_h24=1_200_000, mcap=2_000_000, reserve=365_000)
    row = lp.pool_metrics(pool, {}, ETH_USD, lp.fee_tiers())
    # 3000/day on 365k of reserves -> 1,095,000/yr -> 300%
    assert row["fee_apr"] == pytest.approx(300.0)


def test_token_symbol_pulled_from_included_payload():
    pool = make_pool("a", "AAA", volume_h24=1, mcap=1)
    row = lp.pool_metrics(pool, {"robinhood_ta": token_included("a", "AAA")},
                          ETH_USD, lp.fee_tiers())
    assert row["symbol"] == "AAA"
    assert row["token_name"] == "AAA Token"


def test_missing_volume_is_zero_not_a_crash():
    pool = make_pool("a", "AAA", volume_h24=1, mcap=1)
    pool["attributes"]["volume_usd"] = {}
    row = lp.pool_metrics(pool, {}, ETH_USD, lp.fee_tiers())
    assert row["fees_eth"] == 0.0


# ----------------------------- unit: thresholds -----------------------------
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


# ----------------------------- unit: network lookup -------------------------
def test_resolve_network_matches_on_name():
    session = FakeSession([], networks=[
        {"id": "eth", "attributes": {"name": "Ethereum"}},
        {"id": "rhc", "attributes": {"name": "Robinhood Chain"}},
    ])
    assert lp.resolve_network(session) == "rhc"


def test_resolve_network_errors_when_absent():
    session = FakeSession([], networks=[{"id": "eth", "attributes": {"name": "Ethereum"}}])
    with pytest.raises(lp.ScanError, match="RH_NETWORK"):
        lp.resolve_network(session)


# ----------------------------- end to end -----------------------------------
def build_board():
    """Four pools: two should fire the trigger, two should not."""
    pools = [
        # clears both gates: 1.0 ETH of fees, $2M cap
        make_pool("a", "AAA", volume_h24=vol_for_fees(1.0), mcap=2_000_000),
        # plenty of fees, but the cap is under $500k
        make_pool("b", "BBB", volume_h24=vol_for_fees(1.0), mcap=400_000),
        # big cap, but the pool barely trades
        make_pool("c", "CCC", volume_h24=100_000, mcap=5_000_000),
        # no market cap reported; FDV carries it, 0.6 ETH of fees
        make_pool("d", "DDD", volume_h24=vol_for_fees(0.6), mcap=None, fdv=1_000_000),
    ]
    included = [token_included(p, s) for p, s in
                (("a", "AAA"), ("b", "BBB"), ("c", "CCC"), ("d", "DDD"))]
    return pools, included


def test_scan_returns_only_qualifying_pools_sorted_by_fees(tmp_path):
    pools, included = build_board()
    session = FakeSession(pools, included)
    result = lp.scan(network="robinhood", pages=1, session=session,
                     state_path=str(tmp_path / "hits.json"))

    assert [h["symbol"] for h in result["hits"]] == ["AAA", "DDD"]
    assert result["hits"][0]["fees_eth"] == pytest.approx(1.0)
    assert result["hits"][1]["fees_eth"] == pytest.approx(0.6)
    assert result["scanned"] == 4
    assert result["eth_usd"] == pytest.approx(ETH_USD)
    assert result["filters"] == {"min_mcap_usd": 500_000.0,
                                 "min_fees_eth": 0.5, "fee_window": "h24"}


def test_scan_window_switch_changes_the_verdict(tmp_path):
    """A pool with h24 volume but no h6 volume passes on h24 and fails on h6."""
    pool = make_pool("a", "AAA", volume_h24=vol_for_fees(1.0), mcap=2_000_000)
    pool["attributes"]["volume_usd"]["h6"] = "0"
    session = FakeSession([pool], [token_included("a", "AAA")])
    state = str(tmp_path / "hits.json")

    assert len(lp.scan(network="robinhood", pages=1, session=session,
                       state_path=state)["hits"]) == 1
    assert len(lp.scan(network="robinhood", pages=1, window="h6", session=session,
                       state_path=state)["hits"]) == 0


def test_first_seen_and_new_flag_survive_across_runs(tmp_path):
    pools, included = build_board()
    state = str(tmp_path / "hits.json")

    first = lp.scan(network="robinhood", pages=1, session=FakeSession(pools, included),
                    state_path=state)
    assert all(h["is_new"] for h in first["hits"])
    assert all(h["runs_passed"] == 1 for h in first["hits"])
    lp.save_state(first, state)

    second = lp.scan(network="robinhood", pages=1, session=FakeSession(pools, included),
                     state_path=state)
    assert not any(h["is_new"] for h in second["hits"])
    assert all(h["runs_passed"] == 2 for h in second["hits"])
    assert second["hits"][0]["first_seen"] == first["hits"][0]["first_seen"]
    assert [run["hit_count"] for run in second["history"]] == [2, 2]


def test_scan_reuses_the_network_from_previous_state(tmp_path):
    pools, included = build_board()
    state = str(tmp_path / "hits.json")
    lp.save_state({"network": "rhc-cached", "hits": []}, state)

    session = FakeSession(pools, included)
    result = lp.scan(pages=1, session=session, state_path=state)

    assert result["network"] == "rhc-cached"
    assert not any(url.endswith("/networks") for url, _ in session.calls)


def test_history_is_capped(tmp_path):
    pools, included = build_board()
    state = str(tmp_path / "hits.json")
    lp.save_state({"network": "rhc",
                   "history": [{"at": str(i), "scanned": 0, "hit_count": 0, "hits": []}
                               for i in range(lp.HISTORY_LIMIT + 20)]}, state)
    result = lp.scan(pages=1, session=FakeSession(pools, included), state_path=state)
    assert len(result["history"]) == lp.HISTORY_LIMIT


def test_state_file_write_is_atomic_and_reloadable(tmp_path):
    pools, included = build_board()
    state = str(tmp_path / "hits.json")
    result = lp.scan(network="robinhood", pages=1, session=FakeSession(pools, included),
                     state_path=state)
    lp.save_state(result, state)
    assert lp.load_state(state)["hits"][0]["symbol"] == "AAA"
    assert not (tmp_path / "hits.json.tmp").exists()


def test_corrupt_state_file_does_not_break_the_scan(tmp_path):
    state = tmp_path / "hits.json"
    state.write_text("{not json")
    pools, included = build_board()
    result = lp.scan(network="robinhood", pages=1, session=FakeSession(pools, included),
                     state_path=str(state))
    assert len(result["hits"]) == 2


def test_empty_network_is_reported_not_silently_empty(tmp_path):
    with pytest.raises(lp.ScanError, match="no pools"):
        lp.scan(network="robinhood", pages=1, session=FakeSession([]),
                state_path=str(tmp_path / "hits.json"))


# ----------------------------- retries --------------------------------------
def test_rate_limit_is_retried_then_succeeds():
    class Flaky(FakeSession):
        def __init__(self):
            super().__init__([make_pool("a", "AAA", volume_h24=1)])
            self.attempts = 0

        def get(self, url, params=None, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                return FakeResponse({}, status_code=429)
            return super().get(url, params, timeout)

    session = Flaky()
    payload = lp._request(session, "/networks/rhc/pools")
    assert payload["data"][0]["id"] == "robinhood_0xa"
    assert session.attempts == 2


def test_persistent_failure_raises_scan_error():
    class Dead(FakeSession):
        def get(self, url, params=None, timeout=None):
            return FakeResponse({}, status_code=503)

    with pytest.raises(lp.ScanError, match="failed after"):
        lp._request(Dead([]), "/networks/rhc/pools")
