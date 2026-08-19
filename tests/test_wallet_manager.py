"""Funding, sweeping and token movement across a set of wallets."""

import pytest
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

from mintbot.keystore import generate_keystores
from mintbot.runner import Reporter
from mintbot.wallet_manager import (
    NATIVE_TRANSFER_GAS,
    TOKEN_TRANSFER_GAS,
    TransferError,
    WalletManager,
)
from mintbot.wallets import Wallet, WalletError
from tests.conftest import FakeClient, base_config_dict
from mintbot.config import parse_config

TARGETS = ["0x" + f"{n:040x}" for n in range(1, 4)]
ONE_ETH = 10**18


def decoded(raw: bytes) -> dict:
    """Decode a signed transaction, with `to` normalised to a checksum address."""
    from web3 import Web3

    fields = dict(TypedTransaction.from_bytes(HexBytes(raw)).as_dict())
    fields["to"] = Web3.to_checksum_address(fields["to"])
    return fields


@pytest.fixture
def manager(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    return WalletManager(client, config.gas, Reporter(None), dry_run=False), client


# --------------------------------------------------------------------------- #
# generating wallets
# --------------------------------------------------------------------------- #
def test_generated_wallets_are_distinct_and_encrypted(tmp_path):
    made = generate_keystores(5, "password123", keys_dir=tmp_path)
    assert len({k.address for k in made}) == 5
    assert [k.label for k in made] == [f"wallet{n}" for n in range(1, 6)]
    for key in made:
        assert '"crypto"' in key.path.read_text() or '"Crypto"' in key.path.read_text()


def test_generating_again_does_not_overwrite_earlier_wallets(tmp_path):
    first = generate_keystores(2, "password123", keys_dir=tmp_path)
    second = generate_keystores(2, "password123", keys_dir=tmp_path)
    assert [k.label for k in second] == ["wallet3", "wallet4"]
    assert all(k.path.exists() for k in first + second)


def test_generating_zero_wallets_is_refused(tmp_path):
    with pytest.raises(WalletError, match="at least 1"):
        generate_keystores(0, "password123", keys_dir=tmp_path)


# --------------------------------------------------------------------------- #
# dispersing
# --------------------------------------------------------------------------- #
def test_disperse_sends_one_transaction_per_recipient(manager, wallets):
    wm, client = manager
    batch = wm.disperse(wallets[0], TARGETS, 10**15)

    assert len(client.sent) == 3
    assert [t.status for t in batch.transfers] == ["confirmed"] * 3
    assert batch.total_wei == 3 * 10**15


def test_disperse_uses_sequential_nonces_from_the_funder(manager, wallets):
    wm, client = manager
    wm.disperse(wallets[0], TARGETS, 10**15)

    assert [decoded(raw)["nonce"] for raw in client.sent] == [7, 8, 9]
    assert {Account.recover_transaction(raw) for raw in client.sent} == {wallets[0].address}


def test_disperse_puts_the_right_amount_at_the_right_address(manager, wallets):
    wm, client = manager
    wm.disperse(wallets[0], TARGETS, 12345)

    sent = [(decoded(raw)["to"].lower(), decoded(raw)["value"]) for raw in client.sent]
    assert sent == [(t.lower(), 12345) for t in TARGETS]


def test_disperse_refuses_when_the_funder_cannot_cover_it(manager, wallets):
    wm, client = manager
    client.wallet_balance = 10**15          # 0.001 ETH
    with pytest.raises(TransferError, match="needs up to"):
        wm.disperse(wallets[0], TARGETS, ONE_ETH)
    assert client.sent == []


def test_disperse_counts_gas_in_what_the_funder_needs(manager, wallets):
    wm, client = manager
    # Exactly the transfer value, not a wei for gas.
    client.wallet_balance = 3 * 10**15
    with pytest.raises(TransferError, match="including gas"):
        wm.disperse(wallets[0], TARGETS, 10**15)


@pytest.mark.parametrize("amount", [0, -1])
def test_disperse_refuses_a_non_positive_amount(manager, wallets, amount):
    wm, _ = manager
    with pytest.raises(TransferError, match="greater than zero"):
        wm.disperse(wallets[0], TARGETS, amount)


def test_disperse_refuses_an_empty_recipient_list(manager, wallets):
    wm, _ = manager
    with pytest.raises(TransferError, match="no recipients"):
        wm.disperse(wallets[0], [], 10**15)


# --------------------------------------------------------------------------- #
# consolidating
# --------------------------------------------------------------------------- #
def test_consolidate_sweeps_every_wallet_into_the_destination(manager, wallets):
    wm, client = manager
    batch = wm.consolidate(wallets, TARGETS[0])

    assert len(client.sent) == 2
    assert {decoded(raw)["to"].lower() for raw in client.sent} == {TARGETS[0].lower()}
    assert [t.status for t in batch.transfers] == ["confirmed"] * 2


def test_a_sweep_leaves_exactly_the_gas_behind(manager, wallets):
    wm, client = manager
    fees = wm.fees()
    wm.consolidate(wallets[:1], TARGETS[0])

    swept = decoded(client.sent[0])["value"]
    assert swept == client.wallet_balance - NATIVE_TRANSFER_GAS * fees.max_fee_wei


def test_a_sweep_can_leave_a_float_behind(manager, wallets):
    wm, client = manager
    fees = wm.fees()
    wm.consolidate(wallets[:1], TARGETS[0], leave_wei=10**15)

    swept = decoded(client.sent[0])["value"]
    assert swept == client.wallet_balance - NATIVE_TRANSFER_GAS * fees.max_fee_wei - 10**15


def test_a_wallet_too_poor_to_cover_gas_is_skipped_not_failed(manager, wallets):
    wm, client = manager
    client.wallet_balance = 1000            # far below one gas unit
    batch = wm.consolidate(wallets, TARGETS[0])

    assert client.sent == []
    assert [t.status for t in batch.transfers] == ["skipped"] * 2
    assert "does not cover gas" in batch.transfers[0].detail


def test_the_destination_is_never_asked_to_sweep_itself(manager, wallets):
    wm, client = manager
    batch = wm.consolidate(wallets, wallets[0].address)

    assert len(client.sent) == 1
    assert batch.transfers[0].sender == wallets[1].address


# --------------------------------------------------------------------------- #
# dry run and failures
# --------------------------------------------------------------------------- #
def test_a_dry_run_broadcasts_nothing(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    wm = WalletManager(client, config.gas, Reporter(None), dry_run=True)

    batch = wm.disperse(wallets[0], TARGETS, 10**15)

    assert client.sent == []
    assert [t.status for t in batch.transfers] == ["skipped"] * 3


def test_one_failing_leg_does_not_stop_the_others(config, abi, wallets):
    class FailsOnce(FakeClient):
        def send_raw(self, raw):
            if len(self.sent) == 1:
                self.sent.append(b"")       # keep the count honest
                raise ValueError("nonce too low")
            return super().send_raw(raw)

    client = FailsOnce(config.chain, abi, live=True)
    wm = WalletManager(client, config.gas, Reporter(None), dry_run=False)

    batch = wm.disperse(wallets[0], TARGETS, 10**15)

    assert [t.status for t in batch.transfers] == ["confirmed", "failed", "confirmed"]
    assert len(batch.confirmed) == 2
    assert len(batch.failed) == 1


def test_a_reverted_transfer_is_reported_as_reverted(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    client.receipt_status = 0
    wm = WalletManager(client, config.gas, Reporter(None), dry_run=False)

    batch = wm.disperse(wallets[0], TARGETS[:1], 10**15)
    assert batch.transfers[0].status == "reverted"
    assert batch.failed == batch.transfers


def test_the_batch_summary_counts_what_landed(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    wm = WalletManager(client, config.gas, Reporter(None), dry_run=False)
    batch = wm.disperse(wallets[0], TARGETS, 10**15)
    assert batch.summary().startswith("3/3 confirmed")


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #
def test_a_token_transfer_calls_the_token_not_the_recipient(manager, wallets):
    wm, client = manager
    token = "0x" + "77" * 20

    wm.send_tokens(wallets[0], token, TARGETS[0], 500)

    tx = decoded(client.sent[0])
    assert tx["to"].lower() == token.lower()
    assert tx["value"] == 0
    assert tx["gas"] == TOKEN_TRANSFER_GAS
    # transfer(address,uint256) selector, then the recipient and the amount.
    assert tx["data"].hex().startswith("a9059cbb")
    assert tx["data"].hex().endswith(f"{500:064x}")


def test_token_balances_are_read_per_address(config, abi, wallets):
    class TokenClient(FakeClient):
        def call(self, tx):
            return (777).to_bytes(32, "big")

    client = TokenClient(config.chain, abi)
    wm = WalletManager(client, config.gas, Reporter(None), dry_run=True)

    balances = wm.token_balances("0x" + "77" * 20, [w.address for w in wallets])
    assert balances == {w.address: 777 for w in wallets}


def test_missing_token_metadata_falls_back_instead_of_raising(config, abi, wallets):
    class NoMetadata(FakeClient):
        def call(self, tx):
            raise ValueError("execution reverted")

    wm = WalletManager(NoMetadata(config.chain, abi), config.gas, Reporter(None))
    assert wm.token_metadata("0x" + "77" * 20) == ("TOKEN", 18)
