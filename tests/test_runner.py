import logging
import threading

import pytest
from eth_account import Account

from mintbot.config import parse_config
from mintbot.runner import DEFAULT_GAS_LIMIT, MintRunner, Reporter
from tests.conftest import CONTRACT, FakeClient, base_config_dict


def make_runner(config, client, wallets) -> MintRunner:
    return MintRunner(config, wallets, client, Reporter(None))


# --------------------------------------------------------------------------- #
# planning and signing
# --------------------------------------------------------------------------- #
def test_plan_encodes_the_quantity_and_scales_the_value(client, wallets):
    config = parse_config(base_config_dict(mint={"price": "0.01 eth", "max_per_wallet": 2}))
    runner = make_runner(config, client, wallets)

    plan_a, plan_b = (runner.build_plan(w) for w in wallets)
    assert plan_a.calldata.startswith("0xa0712d68")
    assert plan_a.calldata[10:] == f"{1:064x}"
    assert plan_b.calldata[10:] == f"{2:064x}"
    assert plan_a.value == 10**16
    assert plan_b.value == 2 * 10**16


def test_signed_transaction_recovers_to_the_wallet_that_signed_it(config, client, wallets):
    runner = make_runner(config, client, wallets)
    plan = runner.sign(runner.build_plan(wallets[0]))
    assert Account.recover_transaction(plan.raw) == wallets[0].address


def test_address_and_proof_placeholders_are_filled_per_wallet(client, wallets):
    config = parse_config(
        base_config_dict(mint={"function": "allowlistMint", "args": ["{quantity}", "{proof}"]})
    )
    wallets[0].proof = ("0x" + "ab" * 32,)
    plan = make_runner(config, client, wallets).build_plan(wallets[0])
    assert plan.calldata.startswith("0x7bc9200e")
    assert "ab" * 32 in plan.calldata


def test_worst_case_cost_covers_value_plus_gas(config, client, wallets):
    runner = make_runner(config, client, wallets)
    plan = runner.sign(runner.build_plan(wallets[0]))
    assert plan.max_cost == plan.value + plan.gas_limit * plan.fees.max_fee_wei


def test_gas_estimation_falls_back_while_the_mint_still_reverts(client, wallets, caplog):
    config = parse_config(base_config_dict(gas={"gas_limit": "estimate"}))
    with caplog.at_level(logging.WARNING):
        plan = make_runner(config, client, wallets).build_plan(wallets[0])
    assert plan.gas_limit == DEFAULT_GAS_LIMIT
    assert "cannot estimate gas yet" in caplog.text


def test_gas_estimation_is_used_once_the_call_succeeds(config, abi, wallets):
    config = parse_config(base_config_dict(gas={"gas_limit": "estimate"}))
    client = FakeClient(config.chain, abi, live=True)
    plan = make_runner(config, client, wallets).build_plan(wallets[0])
    assert plan.gas_limit == int(120_000 * config.gas.gas_limit_buffer)


# --------------------------------------------------------------------------- #
# liveness
# --------------------------------------------------------------------------- #
def test_simulation_reports_closed_then_open(config, client, wallets):
    runner = make_runner(config, client, wallets)
    plan = runner.build_plan(wallets[0])

    live, reason = runner.is_live(plan)
    assert live is False and "mint not started" in reason

    client.live = True
    live, reason = runner.is_live(plan)
    assert live is True and reason == "simulation succeeded"


def test_getter_mode_compares_against_the_expected_value(abi, wallets):
    config = parse_config(
        base_config_dict(watch={"mode": "getter", "getter": {"function": "saleState", "expect": 1}})
    )
    client = FakeClient(config.chain, abi)
    runner = make_runner(config, client, wallets)
    plan = runner.build_plan(wallets[0])

    client.getter_value = 0
    assert runner.is_live(plan)[0] is False

    client.getter_value = 1
    assert runner.is_live(plan)[0] is True


def test_both_mode_will_not_fire_on_the_flag_alone(abi, wallets):
    config = parse_config(
        base_config_dict(
            watch={"mode": "both", "getter": {"function": "mintActive", "expect": True}}
        )
    )
    client = FakeClient(config.chain, abi)
    runner = make_runner(config, client, wallets)
    plan = runner.build_plan(wallets[0])

    client.getter_value = 1          # flag flipped, but the call still reverts
    assert runner.is_live(plan)[0] is False

    client.live = True
    assert runner.is_live(plan)[0] is True


# --------------------------------------------------------------------------- #
# firing
# --------------------------------------------------------------------------- #
def test_dry_run_signs_but_broadcasts_nothing(abi, wallets):
    config = parse_config(base_config_dict(safety={"dry_run": True}))
    client = FakeClient(config.chain, abi, live=True)
    runner = make_runner(config, client, wallets)

    results = runner.run([runner.build_plan(w) for w in wallets])
    assert client.sent == []
    assert {r.status for r in results.values()} == {"skipped"}


def test_a_successful_mint_is_recorded_with_its_hash(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    runner = make_runner(config, client, wallets)

    results = runner.run([runner.build_plan(w) for w in wallets])
    assert len(client.sent) == 2
    for result in results.values():
        assert result.status == "minted"
        assert result.tx_hash == "0x" + "ab" * 32
        assert result.attempts == 1


def test_a_reverted_transaction_is_retried_up_to_the_cap(abi, wallets):
    config = parse_config(base_config_dict(safety={"max_attempts_per_wallet": 3}))
    client = FakeClient(config.chain, abi, live=True)
    client.receipt_status = 0
    runner = make_runner(config, client, wallets[:1])

    results = runner.run([runner.build_plan(wallets[0])])
    assert results["a"].attempts == 3
    assert results["a"].status == "failed"
    assert "reverted" in results["a"].detail


def test_insufficient_funds_stops_that_wallet_immediately(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    client.send_error = ValueError("insufficient funds for gas * price + value")
    runner = make_runner(config, client, wallets[:1])

    results = runner.run([runner.build_plan(wallets[0])])
    assert results["a"].status == "failed"
    assert results["a"].attempts == 1
    assert "insufficient funds" in results["a"].detail


def test_an_underpriced_rejection_is_re_signed_and_retried(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    client.send_error = ValueError("replacement transaction underpriced")
    runner = make_runner(config, client, wallets[:1])

    results = runner.run([runner.build_plan(wallets[0])])
    assert results["a"].status == "minted"
    assert results["a"].attempts == 2
    assert len(client.sent) == 1


def test_stop_on_first_success_halts_the_other_wallets(abi, wallets):
    config = parse_config(base_config_dict(safety={"stop_on_first_success": True}))
    client = FakeClient(config.chain, abi, live=True)
    runner = make_runner(config, client, wallets)

    runner.run([runner.build_plan(w) for w in wallets])
    assert runner.stop_event.is_set()


# --------------------------------------------------------------------------- #
# the whole loop
# --------------------------------------------------------------------------- #
def test_the_bot_waits_while_closed_then_fires_when_the_phase_opens(config, abi, wallets):
    class OpensLater(FakeClient):
        """Reverts the first few probes, then behaves like an open mint."""

        def call(self, tx):
            if tx.get("from") is not None and self.call_count >= 3:
                self.live = True
            return super().call(tx)

    client = OpensLater(config.chain, abi)
    runner = make_runner(config, client, wallets[:1])

    results = runner.run([runner.build_plan(wallets[0])])
    assert client.call_count >= 3          # it really did poll before firing
    assert results["a"].status == "minted"
    assert len(client.sent) == 1


def test_a_deadline_gives_up_instead_of_polling_forever(abi, wallets):
    from datetime import datetime, timedelta, timezone

    deadline = (datetime.now(timezone.utc) + timedelta(milliseconds=250)).isoformat()
    config = parse_config(base_config_dict(watch={"deadline_at": deadline}))
    client = FakeClient(config.chain, abi, live=False)
    runner = make_runner(config, client, wallets[:1])

    results = runner.run([runner.build_plan(wallets[0])])
    assert results["a"].status == "failed"
    assert "deadline" in results["a"].detail
    assert client.sent == []


def test_an_rpc_outage_does_not_kill_the_watcher(config, abi, wallets):
    from mintbot.chain import ChainError

    class Flaky(FakeClient):
        def call(self, tx):
            self.call_count += 1
            if self.call_count < 3:
                raise ChainError("all RPC endpoints failed — timeout")
            self.live = True
            return b""

    client = Flaky(config.chain, abi)
    runner = make_runner(config, client, wallets[:1])

    results = runner.run([runner.build_plan(wallets[0])])
    assert results["a"].status == "minted"
    assert client.call_count >= 3          # it survived two outages before firing


def test_every_action_is_written_to_the_jsonl_audit_trail(config, abi, wallets, tmp_path):
    import json

    log_file = tmp_path / "mintbot.jsonl"
    client = FakeClient(config.chain, abi, live=True)
    runner = MintRunner(config, wallets[:1], client, Reporter(log_file))
    runner.run([runner.build_plan(wallets[0])])

    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert "armed" in kinds and "live" in kinds and "sent" in kinds and "minted" in kinds
    assert all("ts" in e for e in events)


def test_the_audit_trail_never_contains_a_private_key(config, abi, wallets, tmp_path):
    from tests.conftest import KEY_A

    log_file = tmp_path / "mintbot.jsonl"
    client = FakeClient(config.chain, abi, live=True)
    runner = MintRunner(config, wallets[:1], client, Reporter(log_file))
    runner.run([runner.build_plan(wallets[0])])

    assert KEY_A not in log_file.read_text()
    assert KEY_A[2:] not in log_file.read_text()


def test_a_broadcast_without_a_receipt_is_not_reported_as_a_failure(config, abi, wallets):
    class NoReceipt(FakeClient):
        def wait_for_receipt(self, tx_hash, timeout=120.0):
            raise TimeoutError("not mined within 120s")

    client = NoReceipt(config.chain, abi, live=True)
    runner = make_runner(config, client, wallets[:1])

    results = runner.run([runner.build_plan(wallets[0])])
    assert results["a"].status == "broadcast"
    assert results["a"].tx_hash == "0x" + "ab" * 32
    assert "no receipt yet" in results["a"].detail
