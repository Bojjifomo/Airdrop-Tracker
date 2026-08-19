import pytest

from mintbot.chain import (
    ChainError,
    FeeParams,
    GasTooHigh,
    compute_fees,
    load_abi,
    resolve_overload,
    signature_of,
)
from mintbot.config import GasConfig

GWEI = 10**9


def gas(**overrides) -> GasConfig:
    defaults = {"max_fee_gwei": 5.0, "priority_fee_gwei": 0.01}
    defaults.update(overrides)
    return GasConfig(**defaults)


def test_bundled_abi_loads_by_default():
    abi = load_abi(None)
    assert any(e["name"] == "mint" for e in abi)


def test_missing_abi_file_is_reported():
    with pytest.raises(ChainError, match="not found"):
        load_abi("/nonexistent/abi.json")


def test_overload_is_resolved_by_argument_count(abi):
    assert signature_of(resolve_overload(abi, "mint", [1])) == "mint(uint256)"
    assert signature_of(resolve_overload(abi, "mint", [])) == "mint()"
    assert signature_of(resolve_overload(abi, "mint", ["0xabc", 1])) == "mint(address,uint256)"


def test_unknown_function_lists_what_is_available(abi):
    with pytest.raises(ChainError, match="Available:"):
        resolve_overload(abi, "summonCat", [])


def test_wrong_arity_names_the_accepted_arities(abi):
    with pytest.raises(ChainError, match="takes 0 or 1 or 2 argument"):
        resolve_overload(abi, "mint", [1, 2, 3])


def test_fees_target_twice_the_base_fee_plus_tip():
    fees = compute_fees(base_fee_wei=GWEI, gas=gas())
    assert fees.max_fee_wei == 2 * GWEI + int(0.01 * GWEI)
    assert fees.priority_fee_wei == int(0.01 * GWEI)
    assert fees.legacy is False


def test_fees_are_clamped_to_the_configured_ceiling():
    fees = compute_fees(base_fee_wei=4 * GWEI, gas=gas(max_fee_gwei=5.0))
    assert fees.max_fee_wei == 5 * GWEI


def test_a_base_fee_above_the_ceiling_refuses_to_produce_fees():
    with pytest.raises(GasTooHigh, match="above your"):
        compute_fees(base_fee_wei=9 * GWEI, gas=gas(max_fee_gwei=5.0))


def test_legacy_mode_emits_a_single_gas_price():
    fees = compute_fees(base_fee_wei=GWEI, gas=gas(legacy=True))
    assert fees.legacy is True
    assert fees.as_tx_fields() == {"gasPrice": fees.max_fee_wei}


def test_eip1559_mode_emits_both_fee_fields():
    fields = compute_fees(base_fee_wei=GWEI, gas=gas()).as_tx_fields()
    assert set(fields) == {"maxFeePerGas", "maxPriorityFeePerGas"}


def test_bumping_raises_fees_but_respects_the_ceiling():
    fees = FeeParams(max_fee_wei=2 * GWEI, priority_fee_wei=GWEI // 10, base_fee_wei=GWEI)
    bumped = fees.bumped(percent=50, ceiling_wei=5 * GWEI)
    assert bumped.max_fee_wei == 3 * GWEI
    assert bumped.bumped(percent=100, ceiling_wei=5 * GWEI).max_fee_wei == 5 * GWEI


def test_encode_call_produces_the_expected_selector(client):
    from tests.conftest import CONTRACT

    calldata = client.encode_call(CONTRACT, "mint", [3])
    assert calldata.startswith("0xa0712d68")          # keccak("mint(uint256)")[:4]
    assert calldata[10:] == f"{3:064x}"


def test_failover_moves_to_the_next_endpoint(config, abi):
    from mintbot.chain import ChainClient
    from mintbot.config import ChainConfig

    chain = ChainConfig(rpc_url="http://a", chain_id=1, fallback_rpc_urls=("http://b",))
    client = ChainClient(chain, abi)
    seen: list[str] = []

    def flaky(w3):
        url = w3.provider.endpoint_uri
        seen.append(url)
        if url == "http://a":
            raise ConnectionError("boom")
        return "second endpoint answered"

    assert client.run(flaky) == "second endpoint answered"
    assert seen == ["http://a", "http://b"]


def test_all_endpoints_failing_raises_with_every_reason(abi):
    from mintbot.chain import ChainClient
    from mintbot.config import ChainConfig

    client = ChainClient(ChainConfig(rpc_url="http://a", chain_id=1), abi)
    with pytest.raises(ChainError, match="all RPC endpoints failed"):
        client.run(lambda w3: (_ for _ in ()).throw(ConnectionError("down")))


def test_a_revert_is_not_retried_across_endpoints(abi):
    from web3.exceptions import ContractLogicError

    from mintbot.chain import ChainClient
    from mintbot.config import ChainConfig

    chain = ChainConfig(rpc_url="http://a", chain_id=1, fallback_rpc_urls=("http://b",))
    client = ChainClient(chain, abi)
    calls: list[int] = []

    def reverting(w3):
        calls.append(1)
        raise ContractLogicError("execution reverted")

    with pytest.raises(ContractLogicError):
        client.run(reverting)
    assert len(calls) == 1
