import pytest

from mintbot.cli import _preflight, build_parser, main
from mintbot.config import parse_config
from tests.conftest import FakeClient, base_config_dict


def preflight(config, client, wallets):
    return _preflight(config, client, wallets)


def test_clean_preflight_reports_no_problems(config, client, wallets):
    problems, notes = preflight(config, client, wallets)
    assert problems == []
    assert any("chain id 4663 confirmed" in n for n in notes)
    assert any("mint entrypoint resolves to mint(uint256)" in n for n in notes)


def test_preflight_flags_an_address_with_no_contract(config, client, wallets):
    client.has_code = False
    problems, _ = preflight(config, client, wallets)
    assert any("no contract code" in p for p in problems)


def test_preflight_flags_an_underfunded_wallet(abi, wallets):
    config = parse_config(base_config_dict(mint={"price": "0.5 eth", "max_per_wallet": 2}))
    client = FakeClient(config.chain, abi)
    client.wallet_balance = 10**15          # 0.001 ETH, nowhere near the price
    problems, _ = preflight(config, client, wallets)
    assert sum("needs" in p for p in problems) == len(wallets)


def test_preflight_flags_a_paid_mint_against_a_non_payable_function(abi, wallets):
    config = parse_config(
        base_config_dict(mint={"function": "totalSupply", "args": [], "price": "0.01 eth"})
    )
    client = FakeClient(config.chain, abi)
    problems, _ = preflight(config, client, wallets)
    assert any("not payable" in p for p in problems)


def test_preflight_flags_a_mint_function_missing_from_the_abi(abi, wallets):
    config = parse_config(base_config_dict(mint={"function": "summonCat", "args": []}))
    client = FakeClient(config.chain, abi)
    problems, _ = preflight(config, client, wallets)
    assert any("is not in the ABI" in p for p in problems)


def test_preflight_enforces_the_total_spend_cap(abi, wallets):
    config = parse_config(
        base_config_dict(
            mint={"price": "0.2 eth", "max_per_wallet": 2},
            safety={"max_total_spend_eth": 0.1},
        )
    )
    client = FakeClient(config.chain, abi)
    problems, _ = preflight(config, client, wallets)
    assert any("exceeds" in p and "max_total_spend_eth" in p for p in problems)


def test_preflight_says_so_when_the_mint_is_already_open(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    problems, notes = preflight(config, client, wallets)
    assert problems == []
    assert any("MINT IS OPEN RIGHT NOW" in n for n in notes)


def test_preflight_holds_rather_than_overpay_when_gas_is_above_the_ceiling(abi, wallets):
    config = parse_config(base_config_dict(gas={"max_fee_gwei": 1.0}))
    client = FakeClient(config.chain, abi, base_fee=50 * 10**9)
    problems, notes = preflight(config, client, wallets)
    assert any("above your ceiling" in n for n in notes)
    assert not any("cannot read gas" in p for p in problems)


def test_the_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_run_defaults_to_the_safe_side():
    args = build_parser().parse_args(["run"])
    assert args.live is False and args.yes is False


def test_a_missing_config_file_exits_with_a_usable_message(tmp_path, caplog):
    code = main(["--config", str(tmp_path / "absent.toml"), "preflight"])
    assert code == 2
    assert "not found" in caplog.text
