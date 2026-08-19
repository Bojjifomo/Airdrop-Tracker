import pytest

from mintbot.config import ConfigError, parse_config, resolve_args
from tests.conftest import base_config_dict


def test_parses_a_minimal_config(config):
    assert config.chain.chain_id == 4663
    assert config.mint.function == "mint"
    assert config.gas.max_fee_wei == 5 * 10**9
    assert config.safety.dry_run is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0 eth", 0),
        ("0.01 eth", 10**16),
        ("5 gwei", 5 * 10**9),
        (1234, 1234),
        ("0x10", 16),
    ],
)
def test_price_accepts_units(value, expected):
    config = parse_config(base_config_dict(mint={"price": value}))
    assert config.mint.price_wei == expected


def test_rejects_unknown_price_unit():
    with pytest.raises(ConfigError, match="unknown unit"):
        parse_config(base_config_dict(mint={"price": "1 satoshi"}))


def test_rejects_quantity_above_the_per_wallet_cap():
    with pytest.raises(ConfigError, match="max_per_wallet"):
        parse_config(base_config_dict(mint={"quantity": 5, "max_per_wallet": 2}))


def test_rejects_a_malformed_contract_address():
    with pytest.raises(ConfigError, match="not a 20-byte hex address"):
        parse_config(base_config_dict(contract={"address": "0xdeadbeef"}))


def test_rejects_an_unknown_watch_mode():
    with pytest.raises(ConfigError, match="watch..mode"):
        parse_config(base_config_dict(watch={"mode": "vibes"}))


def test_getter_mode_requires_a_getter_function():
    with pytest.raises(ConfigError, match="requires"):
        parse_config(base_config_dict(watch={"mode": "getter"}))


def test_priority_fee_cannot_exceed_max_fee():
    with pytest.raises(ConfigError, match="cannot exceed"):
        parse_config(base_config_dict(gas={"max_fee_gwei": 1.0, "priority_fee_gwei": 2.0}))


def test_rejects_a_punishing_poll_interval():
    with pytest.raises(ConfigError, match="rate-limited"):
        parse_config(base_config_dict(watch={"poll_interval_ms": 10}))


def test_deadline_must_follow_start():
    with pytest.raises(ConfigError, match="after start_at"):
        parse_config(
            base_config_dict(
                watch={"start_at": "2026-08-20T15:00:00Z", "deadline_at": "2026-08-20T14:00:00Z"}
            )
        )


def test_start_at_without_a_zone_is_read_as_utc():
    config = parse_config(base_config_dict(watch={"start_at": "2026-08-20T15:00:00"}))
    assert config.watch.start_at.tzinfo is not None
    assert config.watch.start_at.hour == 15


def test_gas_limit_estimate_keyword_means_no_fixed_limit():
    assert parse_config(base_config_dict(gas={"gas_limit": "estimate"})).gas.gas_limit is None


def test_rpc_urls_deduplicate_and_keep_the_primary_first():
    config = parse_config(
        base_config_dict(
            chain={
                "rpc_url": "http://a",
                "fallback_rpc_urls": ["http://b", "http://a", "http://c"],
            }
        )
    )
    assert config.chain.rpc_urls == ("http://a", "http://b", "http://c")


def test_resolve_args_substitutes_placeholders():
    context = {"quantity": 3, "address": "0xabc", "proof": [b"\x01"]}
    assert resolve_args(("{quantity}", "{proof}", 9), context) == [3, [b"\x01"], 9]


def test_resolve_args_rejects_an_unknown_placeholder():
    with pytest.raises(ConfigError, match="no value"):
        resolve_args(("{nonsense}",), {"quantity": 1})
