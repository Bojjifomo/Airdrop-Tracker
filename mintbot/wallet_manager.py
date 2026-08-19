"""Funding operations across a set of wallets.

Before a drop you need every wallet funded; after one you want the proceeds
back in a single place. Both are the same primitive — a batch of native or
token transfers — so they share one implementation and one set of guardrails.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import Web3

from .chain import ChainClient, FeeParams, compute_fees, load_abi
from .config import GasConfig
from .runner import RETRYABLE_SEND_ERRORS, Reporter
from .wallets import Wallet

log = logging.getLogger("mintbot")

NATIVE_TRANSFER_GAS = 21_000
# ERC20 transfers vary by token; this covers the ordinary ones with room to spare.
TOKEN_TRANSFER_GAS = 100_000
ERC20_ABI = load_abi(Path(__file__).parent / "abi" / "erc20.json")


class TransferError(ValueError):
    """Raised when a requested transfer cannot be made safely."""


@dataclass
class Transfer:
    """One leg of a batch, and what became of it."""

    label: str
    sender: str
    recipient: str
    amount_wei: int
    token: str | None = None
    tx_hash: str | None = None
    status: str = "planned"          # planned | sent | confirmed | reverted | failed | skipped
    detail: str = ""

    @property
    def amount_eth(self) -> float:
        return self.amount_wei / 1e18


@dataclass
class Leg:
    """One transfer, resolved down to the transaction it will become."""

    wallet: Wallet
    transfer: Transfer
    to: str
    value: int
    calldata: str = "0x"
    gas_limit: int = NATIVE_TRANSFER_GAS
    nonce: int = 0


@dataclass
class Batch:
    transfers: list[Transfer] = field(default_factory=list)

    @property
    def confirmed(self) -> list[Transfer]:
        return [t for t in self.transfers if t.status == "confirmed"]

    @property
    def failed(self) -> list[Transfer]:
        return [t for t in self.transfers if t.status in ("failed", "reverted")]

    @property
    def total_wei(self) -> int:
        return sum(t.amount_wei for t in self.transfers)

    def summary(self) -> str:
        return (
            f"{len(self.confirmed)}/{len(self.transfers)} confirmed, "
            f"{self.total_wei / 1e18:.6f} moved"
        )


class WalletManager:
    """Native and ERC20 movement across many wallets."""

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

    # -- reads ---------------------------------------------------------------
    def fees(self) -> FeeParams:
        return compute_fees(self.client.base_fee(), self.gas)

    def native_balances(self, addresses: list[str]) -> dict[str, int]:
        return {address: self.client.balance(address) for address in addresses}

    def token_balances(self, token: str, addresses: list[str]) -> dict[str, int]:
        """Read an ERC20 balance for each address, via eth_call."""
        token = Web3.to_checksum_address(token)
        balances = {}
        for address in addresses:
            calldata = self.client.encode_call(
                token, "balanceOf", [Web3.to_checksum_address(address)], abi=ERC20_ABI
            )
            raw = self.client.call({"to": token, "data": calldata})
            balances[address] = int(
                self.client.decode_result("balanceOf", [address], raw, abi=ERC20_ABI)
            )
        return balances

    def token_metadata(self, token: str) -> tuple[str, int]:
        """Return (symbol, decimals), falling back when the token omits them."""
        token = Web3.to_checksum_address(token)
        symbol, decimals = "TOKEN", 18
        for name in ("symbol", "decimals"):
            try:
                calldata = self.client.encode_call(token, name, [], abi=ERC20_ABI)
                raw = self.client.call({"to": token, "data": calldata})
                value = self.client.decode_result(name, [], raw, abi=ERC20_ABI)
                if name == "symbol":
                    symbol = str(value)
                else:
                    decimals = int(value)
            except Exception:  # noqa: BLE001 - optional metadata, never fatal
                pass
        return symbol, decimals

    # -- writes --------------------------------------------------------------
    def _send(
        self,
        wallet: Wallet,
        to: str,
        value: int,
        nonce: int,
        fees: FeeParams,
        data: str = "0x",
        gas_limit: int = NATIVE_TRANSFER_GAS,
    ) -> str:
        tx: dict[str, Any] = {
            "chainId": self.client.chain.chain_id,
            "to": Web3.to_checksum_address(to),
            "from": Web3.to_checksum_address(wallet.address),
            "value": value,
            "data": data,
            "gas": gas_limit,
            "nonce": nonce,
            **fees.as_tx_fields(),
        }
        if not fees.legacy:
            tx["type"] = 2
        signed = Account.sign_transaction(tx, wallet.key)
        return self.client.send_raw(bytes(signed.raw_transaction))

    def run_batch(self, legs: list[Leg]) -> Batch:
        """Broadcast every leg, then confirm them.

        Shared by every batch operation here and by the NFT manager.

        Broadcasting first and confirming after keeps one slow receipt from
        holding up the rest of the batch.
        """
        batch = Batch([leg.transfer for leg in legs])
        if self.dry_run:
            for leg in legs:
                leg.transfer.status = "skipped"
                leg.transfer.detail = "dry run — nothing was broadcast"
            self.reporter.event(
                "batch_dry_run",
                f"DRY RUN — {len(legs)} transfer(s), {batch.total_wei / 1e18:.6f} total",
                transfers=len(legs), total_wei=batch.total_wei,
            )
            return batch

        fees = self.fees()
        pending: list[Transfer] = []
        for leg in legs:
            transfer = leg.transfer
            try:
                transfer.tx_hash = self._send(
                    leg.wallet, leg.to, leg.value, leg.nonce, fees,
                    data=leg.calldata, gas_limit=leg.gas_limit,
                )
                transfer.status = "sent"
                pending.append(transfer)
                self.reporter.event(
                    "transfer_sent",
                    f"[{transfer.label}] sent {transfer.amount_eth:.6f} to "
                    f"{transfer.recipient[:10]}… — {transfer.tx_hash}",
                    label=transfer.label, tx_hash=transfer.tx_hash,
                    amount_wei=transfer.amount_wei,
                )
            except Exception as exc:  # noqa: BLE001 - one leg failing is not the batch failing
                transfer.status = "failed"
                transfer.detail = str(exc)[:200]
                level = (
                    logging.WARNING
                    if any(t in str(exc).lower() for t in RETRYABLE_SEND_ERRORS)
                    else logging.ERROR
                )
                self.reporter.event(
                    "transfer_failed", f"[{transfer.label}] {str(exc)[:160]}",
                    level=level, label=transfer.label, error=str(exc)[:200],
                )

        for transfer in pending:
            try:
                receipt = self.client.wait_for_receipt(transfer.tx_hash or "")
                if receipt.get("status") == 1:
                    transfer.status = "confirmed"
                    transfer.detail = f"block {receipt.get('blockNumber')}"
                else:
                    transfer.status = "reverted"
                    transfer.detail = "reverted on-chain"
            except Exception as exc:  # noqa: BLE001 - broadcast but unconfirmed
                transfer.detail = f"sent, no receipt yet: {str(exc)[:120]}"

        self.reporter.event("batch_done", batch.summary(), **{"summary": batch.summary()})
        return batch

    # -- one to many ---------------------------------------------------------
    def disperse(
        self, funder: Wallet, recipients: list[str], amount_wei: int
    ) -> Batch:
        """Send the same amount of native currency to each recipient."""
        if amount_wei <= 0:
            raise TransferError("amount must be greater than zero")
        if not recipients:
            raise TransferError("no recipients given")

        fees = self.fees()
        gas_cost = NATIVE_TRANSFER_GAS * fees.max_fee_wei
        needed = len(recipients) * (amount_wei + gas_cost)
        balance = self.client.balance(funder.address)
        if balance < needed:
            raise TransferError(
                f"{funder.label} holds {balance / 1e18:.6f} but dispersing to "
                f"{len(recipients)} wallet(s) needs up to {needed / 1e18:.6f} "
                f"(including gas)"
            )

        nonce = self.client.pending_nonce(funder.address)
        legs = []
        for offset, recipient in enumerate(recipients):
            transfer = Transfer(
                label=f"{funder.label}→{recipient[:8]}…",
                sender=funder.address,
                recipient=Web3.to_checksum_address(recipient),
                amount_wei=amount_wei,
            )
            legs.append(
                Leg(
                    wallet=funder, transfer=transfer, to=transfer.recipient,
                    value=amount_wei, nonce=nonce + offset,
                )
            )
        return self.run_batch(legs)

    # -- many to one ---------------------------------------------------------
    def consolidate(
        self, wallets: list[Wallet], destination: str, leave_wei: int = 0
    ) -> Batch:
        """Sweep each wallet's native balance into one address.

        Gas is deducted from the amount sent, so a sweep empties the wallet
        rather than failing for one wei. `leave_wei` keeps a float behind.
        """
        destination = Web3.to_checksum_address(destination)
        fees = self.fees()
        gas_cost = NATIVE_TRANSFER_GAS * fees.max_fee_wei

        legs = []
        skipped = []
        for wallet in wallets:
            if wallet.address.lower() == destination.lower():
                continue
            balance = self.client.balance(wallet.address)
            amount = balance - gas_cost - leave_wei
            if amount <= 0:
                skipped.append(
                    Transfer(
                        label=wallet.label, sender=wallet.address, recipient=destination,
                        amount_wei=0, status="skipped",
                        detail=f"balance {balance / 1e18:.6f} does not cover gas",
                    )
                )
                continue
            transfer = Transfer(
                label=wallet.label, sender=wallet.address,
                recipient=destination, amount_wei=amount,
            )
            legs.append(
                Leg(
                    wallet=wallet, transfer=transfer, to=destination, value=amount,
                    nonce=self.client.pending_nonce(wallet.address),
                )
            )

        batch = self.run_batch(legs)
        batch.transfers.extend(skipped)
        return batch

    # -- tokens --------------------------------------------------------------
    def send_tokens(
        self, wallet: Wallet, token: str, recipient: str, amount: int
    ) -> Batch:
        """Move an ERC20 balance out of one wallet."""
        token = Web3.to_checksum_address(token)
        recipient = Web3.to_checksum_address(recipient)
        calldata = self.client.encode_call(
            token, "transfer", [recipient, amount], abi=ERC20_ABI
        )

        transfer = Transfer(
            label=wallet.label, sender=wallet.address, recipient=recipient,
            amount_wei=amount, token=token,
        )
        return self.run_batch(
            [
                Leg(
                    wallet=wallet, transfer=transfer, to=token, value=0, calldata=calldata,
                    gas_limit=TOKEN_TRANSFER_GAS,
                    nonce=self.client.pending_nonce(wallet.address),
                )
            ]
        )
