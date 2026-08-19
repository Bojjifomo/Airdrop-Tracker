"""NFT inventory and post-mint transfers.

A freshly minted NFT sits in the wallet that holds a hot private key. Moving it
to a vault the moment it lands shrinks the window where a leaked key costs you
the mint, so the runner can do that automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3

from .chain import ChainClient, load_abi
from .config import GasConfig
from .runner import Reporter
from .wallet_manager import Batch, Leg, Transfer, WalletManager
from .wallets import Wallet

log = logging.getLogger("mintbot")

ERC721_ABI = load_abi(Path(__file__).parent / "abi" / "erc721.json")
TRANSFER_TOPIC = HexBytes(keccak(text="Transfer(address,address,uint256)"))
# safeTransferFrom runs the recipient's onERC721Received hook, so leave room.
NFT_TRANSFER_GAS = 180_000


def _as_hexbytes(value: Any) -> HexBytes:
    return value if isinstance(value, HexBytes) else HexBytes(value)


def token_ids_from_receipt(receipt: dict[str, Any], contract: str, owner: str) -> list[int]:
    """Pull the token ids a mint receipt delivered to `owner`.

    ERC721 indexes all three Transfer arguments, so a four-topic log is a token
    movement and a three-topic one is ERC20 sharing the same event signature.
    """
    contract = Web3.to_checksum_address(contract)
    owner_topic = HexBytes(Web3.to_checksum_address(owner)).rjust(32, b"\x00")

    ids: list[int] = []
    for entry in receipt.get("logs", []):
        address = entry.get("address")
        if not address or Web3.to_checksum_address(address) != contract:
            continue
        topics = [_as_hexbytes(t) for t in entry.get("topics", [])]
        if len(topics) != 4 or topics[0] != TRANSFER_TOPIC:
            continue
        if bytes(topics[2]) != bytes(owner_topic):
            continue
        ids.append(int.from_bytes(bytes(topics[3]), "big"))
    return ids


class NftManager:
    """Reads NFT holdings and moves them."""

    def __init__(
        self,
        client: ChainClient,
        gas: GasConfig,
        reporter: Reporter | None = None,
        dry_run: bool = True,
    ):
        self.client = client
        self.gas = gas
        self.reporter = reporter or Reporter(None)
        self.dry_run = dry_run
        self._funds = WalletManager(client, gas, self.reporter, dry_run)

    def collection_name(self, contract: str) -> str:
        try:
            contract = Web3.to_checksum_address(contract)
            calldata = self.client.encode_call(contract, "name", [], abi=ERC721_ABI)
            raw = self.client.call({"to": contract, "data": calldata})
            return str(self.client.decode_result("name", [], raw, abi=ERC721_ABI))
        except Exception:  # noqa: BLE001 - a nameless collection is not an error
            return "collection"

    def balances(self, contract: str, addresses: list[str]) -> dict[str, int]:
        """How many tokens of this collection each address holds."""
        contract = Web3.to_checksum_address(contract)
        holdings = {}
        for address in addresses:
            args = [Web3.to_checksum_address(address)]
            calldata = self.client.encode_call(contract, "balanceOf", args, abi=ERC721_ABI)
            raw = self.client.call({"to": contract, "data": calldata})
            holdings[address] = int(
                self.client.decode_result("balanceOf", args, raw, abi=ERC721_ABI)
            )
        return holdings

    def owned_ids(self, contract: str, address: str, limit: int = 50) -> list[int]:
        """Enumerate an address's token ids, where the collection supports it."""
        contract = Web3.to_checksum_address(contract)
        owner = Web3.to_checksum_address(address)
        held = self.balances(contract, [address])[address]

        ids: list[int] = []
        for index in range(min(held, limit)):
            try:
                args = [owner, index]
                calldata = self.client.encode_call(
                    contract, "tokenOfOwnerByIndex", args, abi=ERC721_ABI
                )
                raw = self.client.call({"to": contract, "data": calldata})
                ids.append(int(self.client.decode_result("tokenOfOwnerByIndex", args, raw, abi=ERC721_ABI)))
            except Exception:  # noqa: BLE001 - enumeration is an optional extension
                break
        return ids

    def transfer(
        self, wallet: Wallet, contract: str, destination: str, token_ids: list[int]
    ) -> Batch:
        """Move specific token ids out of `wallet` into `destination`."""
        if not token_ids:
            return Batch([])

        contract = Web3.to_checksum_address(contract)
        destination = Web3.to_checksum_address(destination)
        sender = Web3.to_checksum_address(wallet.address)
        nonce = self.client.pending_nonce(wallet.address)

        legs = []
        for offset, token_id in enumerate(token_ids):
            args = [sender, destination, token_id]
            calldata = self.client.encode_call(
                contract, "safeTransferFrom", args, abi=ERC721_ABI
            )
            transfer = Transfer(
                label=f"{wallet.label}#{token_id}",
                sender=sender,
                recipient=destination,
                amount_wei=0,
                token=contract,
                detail=f"token id {token_id}",
            )
            legs.append(
                Leg(
                    wallet=wallet, transfer=transfer, to=contract, value=0,
                    calldata=calldata, gas_limit=NFT_TRANSFER_GAS, nonce=nonce + offset,
                )
            )

        self.reporter.event(
            "post_mint_transfer",
            f"[{wallet.label}] moving {len(token_ids)} token(s) to {destination[:10]}…",
            wallet=wallet.label, destination=destination, token_ids=token_ids,
        )
        return self._funds.run_batch(legs)

    def rescue_from_receipt(
        self, wallet: Wallet, contract: str, destination: str, receipt: dict[str, Any]
    ) -> Batch:
        """Transfer exactly what a mint receipt just delivered to this wallet."""
        token_ids = token_ids_from_receipt(receipt, contract, wallet.address)
        if not token_ids:
            self.reporter.event(
                "post_mint_skipped",
                f"[{wallet.label}] no token ids found in the mint receipt — nothing moved",
                level=logging.WARNING, wallet=wallet.label,
            )
            return Batch([])
        return self.transfer(wallet, contract, destination, token_ids)
