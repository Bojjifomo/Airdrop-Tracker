"""Reading token ids out of a mint receipt and sweeping them to a vault."""

import pytest
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3

from mintbot.config import parse_config
from mintbot.nft import NFT_TRANSFER_GAS, NftManager, token_ids_from_receipt
from mintbot.runner import MintRunner, Reporter
from tests.conftest import CONTRACT, FakeClient, base_config_dict

TRANSFER = HexBytes(keccak(text="Transfer(address,address,uint256)"))
VAULT = Web3.to_checksum_address("0x" + "99" * 20)


def topic(value: str | int) -> HexBytes:
    if isinstance(value, int):
        return HexBytes(value.to_bytes(32, "big"))
    return HexBytes(HexBytes(value).rjust(32, b"\x00"))


def mint_log(contract: str, to: str, token_id: int, topics: int = 4) -> dict:
    entries = [TRANSFER, topic("0x" + "00" * 20), topic(to), topic(token_id)]
    return {"address": contract, "topics": entries[:topics], "data": HexBytes("0x")}


def decoded(raw: bytes) -> dict:
    fields = dict(TypedTransaction.from_bytes(HexBytes(raw)).as_dict())
    fields["to"] = Web3.to_checksum_address(fields["to"])
    return fields


# --------------------------------------------------------------------------- #
# reading the receipt
# --------------------------------------------------------------------------- #
def test_token_ids_are_read_out_of_a_mint_receipt(wallets):
    owner = wallets[0].address
    receipt = {"logs": [mint_log(CONTRACT, owner, 41), mint_log(CONTRACT, owner, 42)]}
    assert token_ids_from_receipt(receipt, CONTRACT, owner) == [41, 42]


def test_tokens_delivered_to_someone_else_are_not_claimed(wallets):
    receipt = {"logs": [mint_log(CONTRACT, wallets[1].address, 7)]}
    assert token_ids_from_receipt(receipt, CONTRACT, wallets[0].address) == []


def test_transfers_from_another_contract_are_ignored(wallets):
    other = Web3.to_checksum_address("0x" + "cc" * 20)
    receipt = {"logs": [mint_log(other, wallets[0].address, 7)]}
    assert token_ids_from_receipt(receipt, CONTRACT, wallets[0].address) == []


def test_an_erc20_transfer_sharing_the_signature_is_not_a_token_id(wallets):
    """ERC20 leaves the amount in data, so its Transfer log carries three topics."""
    receipt = {"logs": [mint_log(CONTRACT, wallets[0].address, 7, topics=3)]}
    assert token_ids_from_receipt(receipt, CONTRACT, wallets[0].address) == []


def test_a_receipt_with_no_logs_yields_nothing(wallets):
    assert token_ids_from_receipt({"logs": []}, CONTRACT, wallets[0].address) == []


# --------------------------------------------------------------------------- #
# transferring
# --------------------------------------------------------------------------- #
@pytest.fixture
def manager(config, abi):
    client = FakeClient(config.chain, abi, live=True)
    return NftManager(client, config.gas, Reporter(None), dry_run=False), client


def test_each_token_moves_in_its_own_transaction(manager, wallets):
    nft, client = manager
    batch = nft.transfer(wallets[0], CONTRACT, VAULT, [1, 2, 3])

    assert len(client.sent) == 3
    assert [decoded(raw)["nonce"] for raw in client.sent] == [7, 8, 9]
    assert len(batch.confirmed) == 3


def test_a_transfer_calls_the_collection_with_safe_transfer_from(manager, wallets):
    nft, client = manager
    nft.transfer(wallets[0], CONTRACT, VAULT, [42])

    tx = decoded(client.sent[0])
    assert tx["to"] == Web3.to_checksum_address(CONTRACT)
    assert tx["value"] == 0
    assert tx["gas"] == NFT_TRANSFER_GAS
    # safeTransferFrom(address,address,uint256)
    assert tx["data"].hex().startswith("42842e0e")
    assert tx["data"].hex().endswith(f"{42:064x}")
    assert wallets[0].address[2:].lower() in tx["data"].hex()
    assert VAULT[2:].lower() in tx["data"].hex()


def test_transferring_nothing_does_nothing(manager, wallets):
    nft, client = manager
    batch = nft.transfer(wallets[0], CONTRACT, VAULT, [])
    assert batch.transfers == [] and client.sent == []


def test_a_rescue_moves_exactly_what_the_receipt_delivered(manager, wallets):
    nft, client = manager
    receipt = {"logs": [mint_log(CONTRACT, wallets[0].address, 5)]}

    batch = nft.rescue_from_receipt(wallets[0], CONTRACT, VAULT, receipt)

    assert len(client.sent) == 1
    assert decoded(client.sent[0])["data"].hex().endswith(f"{5:064x}")
    assert len(batch.confirmed) == 1


def test_a_rescue_with_nothing_to_move_is_reported_not_attempted(manager, wallets, tmp_path):
    import json

    nft, client = manager
    nft.reporter = Reporter(tmp_path / "events.jsonl")

    batch = nft.rescue_from_receipt(wallets[0], CONTRACT, VAULT, {"logs": []})

    assert batch.transfers == [] and client.sent == []
    kinds = [json.loads(line)["event"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert kinds == ["post_mint_skipped"]


def test_holdings_are_read_per_address(config, abi, wallets):
    class Holder(FakeClient):
        def call(self, tx):
            return (2).to_bytes(32, "big")

    nft = NftManager(Holder(config.chain, abi), config.gas, Reporter(None))
    assert nft.balances(CONTRACT, [w.address for w in wallets]) == {w.address: 2 for w in wallets}


# --------------------------------------------------------------------------- #
# the runner sweeping after a mint
# --------------------------------------------------------------------------- #
def receipt_with_token(owner: str, token_id: int = 11) -> dict:
    return {
        "status": 1, "blockNumber": 4663, "gasUsed": 1,
        "logs": [mint_log(CONTRACT, owner, token_id)],
    }


def test_a_minted_token_is_swept_to_the_vault_automatically(abi, wallets):
    config = parse_config(
        base_config_dict(postmint={"enabled": True, "destination": VAULT})
    )

    class Minting(FakeClient):
        def wait_for_receipt(self, tx_hash, timeout=120.0):
            return receipt_with_token(wallets[0].address)

    client = Minting(config.chain, abi, live=True)
    results = MintRunner(config, wallets[:1], client, Reporter(None)).run()

    assert results["a"].status == "minted"
    assert results["a"].moved == 1
    assert len(client.sent) == 2                       # the mint, then the sweep
    assert decoded(client.sent[1])["data"].hex().startswith("42842e0e")


def test_nothing_is_swept_when_post_mint_is_off(config, abi, wallets):
    class Minting(FakeClient):
        def wait_for_receipt(self, tx_hash, timeout=120.0):
            return receipt_with_token(wallets[0].address)

    client = Minting(config.chain, abi, live=True)
    results = MintRunner(config, wallets[:1], client, Reporter(None)).run()

    assert results["a"].status == "minted"
    assert results["a"].moved == 0
    assert len(client.sent) == 1


def test_a_destination_equal_to_the_minting_wallet_is_a_no_op(abi, wallets):
    config = parse_config(
        base_config_dict(postmint={"enabled": True, "destination": wallets[0].address})
    )

    class Minting(FakeClient):
        def wait_for_receipt(self, tx_hash, timeout=120.0):
            return receipt_with_token(wallets[0].address)

    client = Minting(config.chain, abi, live=True)
    MintRunner(config, wallets[:1], client, Reporter(None)).run()
    assert len(client.sent) == 1


def test_a_failed_sweep_never_undoes_a_successful_mint(abi, wallets, tmp_path):
    import json

    config = parse_config(
        base_config_dict(postmint={"enabled": True, "destination": VAULT})
    )

    class SweepBreaks(FakeClient):
        def wait_for_receipt(self, tx_hash, timeout=120.0):
            return receipt_with_token(wallets[0].address)

        def pending_nonce(self, address):
            if self.sent:                       # only once the mint has gone out
                raise RuntimeError("rpc fell over")
            return 7

    log_file = tmp_path / "events.jsonl"
    client = SweepBreaks(config.chain, abi, live=True)
    results = MintRunner(config, wallets[:1], client, Reporter(log_file)).run()

    assert results["a"].status == "minted"
    assert results["a"].moved == 0
    kinds = [json.loads(line)["event"] for line in log_file.read_text().splitlines()]
    assert "post_mint_failed" in kinds


def test_post_mint_needs_a_destination_address():
    from mintbot.config import ConfigError

    with pytest.raises(ConfigError, match="destination"):
        parse_config(base_config_dict(postmint={"enabled": True}))
