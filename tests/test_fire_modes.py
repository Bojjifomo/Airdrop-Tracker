"""Instant firing, salvos of sequential-nonce transactions, and rebroadcasting."""

from datetime import datetime, timedelta, timezone

import pytest
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

from mintbot.config import ConfigError, parse_config
from mintbot.runner import MintRunner, Reporter
from tests.conftest import FakeClient, base_config_dict


def make_runner(config, client, wallets) -> MintRunner:
    return MintRunner(config, wallets, client, Reporter(None))


def nonces_of(raw_transactions: list[bytes]) -> list[int]:
    return [TypedTransaction.from_bytes(HexBytes(raw)).as_dict()["nonce"] for raw in raw_transactions]


def just_now() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


# --------------------------------------------------------------------------- #
# instant mode
# --------------------------------------------------------------------------- #
def test_instant_mode_fires_without_ever_probing_the_contract(abi, wallets):
    config = parse_config(base_config_dict(fire={"mode": "instant", "at": just_now()}))
    client = FakeClient(config.chain, abi, live=False)   # the mint call still reverts
    runner = make_runner(config, client, wallets[:1])

    results = runner.run()

    assert results["a"].status == "minted"
    assert client.call_count == 0          # no eth_call was made at all
    assert len(client.sent) == 1


def test_instant_mode_waits_for_its_moment(abi, wallets):
    fire_at = (datetime.now(timezone.utc) + timedelta(milliseconds=400)).isoformat()
    config = parse_config(base_config_dict(fire={"mode": "instant", "at": fire_at}))
    client = FakeClient(config.chain, abi, live=False)
    runner = make_runner(config, client, wallets[:1])

    started = datetime.now(timezone.utc)
    runner.run()
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    assert elapsed >= 0.35
    assert len(client.sent) == 1


def test_instant_mode_without_a_time_is_rejected():
    with pytest.raises(ConfigError, match="needs"):
        parse_config(base_config_dict(fire={"mode": "instant"}))


def test_instant_mode_accepts_the_watch_start_time_instead():
    config = parse_config(
        base_config_dict(fire={"mode": "instant"}, watch={"start_at": just_now()})
    )
    assert config.fire.mode == "instant"


def test_probe_mode_stays_the_default(config):
    assert config.fire.mode == "probe"
    assert config.fire.transactions == 1
    assert config.fire.rebroadcast is False


# --------------------------------------------------------------------------- #
# salvos
# --------------------------------------------------------------------------- #
def test_a_salvo_sends_sequential_nonces_from_one_wallet(abi, wallets):
    config = parse_config(base_config_dict(fire={"transactions": 3, "interval_ms": 0}))
    client = FakeClient(config.chain, abi, live=True)
    runner = make_runner(config, client, wallets[:1])

    results = runner.run()

    assert len(client.sent) == 3
    assert nonces_of(client.sent) == [7, 8, 9]          # FakeClient's pending nonce is 7
    assert results["a"].status == "minted"


def test_every_transaction_in_a_salvo_comes_from_the_same_wallet(abi, wallets):
    config = parse_config(base_config_dict(fire={"transactions": 3, "interval_ms": 0}))
    client = FakeClient(config.chain, abi, live=True)
    runner = make_runner(config, client, wallets[:1])
    runner.run()

    senders = {Account.recover_transaction(raw) for raw in client.sent}
    assert senders == {wallets[0].address}


def test_a_salvo_carries_the_same_calldata_and_value(abi, wallets):
    config = parse_config(
        base_config_dict(mint={"price": "0.01 eth"}, fire={"transactions": 2, "interval_ms": 0})
    )
    client = FakeClient(config.chain, abi, live=True)
    runner = make_runner(config, client, wallets[:1])

    salvo = runner.build_salvo(wallets[0])
    assert len({p.calldata for p in salvo}) == 1
    assert {p.value for p in salvo} == {10**16}


def test_a_reverting_follow_up_does_not_spoil_a_successful_mint(abi, wallets):
    """The extras revert once the wallet's allowance is used — that is expected."""

    class FirstOnly(FakeClient):
        def wait_for_receipt(self, tx_hash, timeout=120.0):
            self.receipts = getattr(self, "receipts", 0) + 1
            return {"status": 1 if self.receipts == 1 else 0, "blockNumber": 4663, "gasUsed": 1}

    config = parse_config(base_config_dict(fire={"transactions": 3, "interval_ms": 0}))
    client = FirstOnly(config.chain, abi, live=True)
    runner = make_runner(config, client, wallets[:1])

    results = runner.run()

    assert results["a"].status == "minted"
    assert results["a"].attempts == 1          # no retry: the mint landed


def test_a_salvo_that_wholly_reverts_is_retried_then_reported_failed(abi, wallets):
    config = parse_config(
        base_config_dict(
            fire={"transactions": 2, "interval_ms": 0}, safety={"max_attempts_per_wallet": 2}
        )
    )
    client = FakeClient(config.chain, abi, live=True)
    client.receipt_status = 0
    runner = make_runner(config, client, wallets[:1])

    results = runner.run()

    assert results["a"].attempts == 2
    assert results["a"].status == "failed"
    assert len(client.sent) == 4               # two transactions per attempt


@pytest.mark.parametrize("count", [0, 11, 100])
def test_an_unreasonable_salvo_size_is_rejected(count):
    with pytest.raises(ConfigError, match="between 1 and 10"):
        parse_config(base_config_dict(fire={"transactions": count}))


def test_the_worst_case_cost_covers_the_whole_salvo(abi, wallets):
    config = parse_config(
        base_config_dict(mint={"price": "0.01 eth"}, fire={"transactions": 3, "interval_ms": 0})
    )
    client = FakeClient(config.chain, abi)
    runner = make_runner(config, client, wallets[:1])

    salvo = runner.build_salvo(wallets[0])
    for plan in salvo:
        runner.sign(plan)
    assert sum(p.max_cost for p in salvo) == 3 * salvo[0].max_cost


# --------------------------------------------------------------------------- #
# rebroadcasting
# --------------------------------------------------------------------------- #
def test_rebroadcast_pushes_the_same_bytes_to_every_endpoint(abi, wallets):
    config = parse_config(
        base_config_dict(
            chain={"rpc_url": "http://a", "fallback_rpc_urls": ["http://b", "http://c"]},
            fire={"rebroadcast": True},
        )
    )

    class Recording(FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.per_endpoint: list[tuple[str, bytes]] = []

        def broadcast_all(self, raw):
            self.per_endpoint = [(url, raw) for url in self._urls]
            self.sent.append(raw)
            return "0x" + "ab" * 32, []

    client = Recording(config.chain, abi, live=True)
    results = make_runner(config, client, wallets[:1]).run()

    assert [url for url, _ in client.per_endpoint] == ["http://a", "http://b", "http://c"]
    assert len({payload for _, payload in client.per_endpoint}) == 1   # identical bytes
    assert results["a"].status == "minted"


def test_a_refused_endpoint_is_reported_without_failing_the_mint(abi, wallets, tmp_path):
    import json

    config = parse_config(base_config_dict(fire={"rebroadcast": True}))

    class PartlyRefused(FakeClient):
        def broadcast_all(self, raw):
            self.sent.append(raw)
            return "0x" + "ab" * 32, ["http://b: 429 Too Many Requests"]

    log_file = tmp_path / "events.jsonl"
    client = PartlyRefused(config.chain, abi, live=True)
    runner = MintRunner(config, wallets[:1], client, Reporter(log_file))

    results = runner.run()

    assert results["a"].status == "minted"
    kinds = [json.loads(line)["event"] for line in log_file.read_text().splitlines()]
    assert "endpoint_refused" in kinds


def test_broadcast_all_raises_only_when_no_endpoint_takes_it(abi):
    from mintbot.chain import ChainClient, ChainError
    from mintbot.config import ChainConfig

    chain = ChainConfig(rpc_url="http://127.0.0.1:1", chain_id=1,
                        fallback_rpc_urls=("http://127.0.0.1:2",))
    client = ChainClient(chain, abi)
    with pytest.raises(ChainError, match="no endpoint accepted"):
        client.broadcast_all(b"\x02\xff")
