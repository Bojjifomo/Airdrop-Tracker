"""Shared fixtures: an offline ChainClient and a minimal working config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from web3.exceptions import ContractLogicError

from mintbot.chain import ChainClient, load_abi
from mintbot.config import parse_config
from mintbot.wallets import Wallet

# Deterministic throwaway keys — test vectors only, never funded.
KEY_A = "0x" + "11" * 32
KEY_B = "0x" + "22" * 32
CONTRACT = "0x00000000000000000000000000000000000000ff"


class FakeClient(ChainClient):
    """A ChainClient whose network calls are canned.

    ABI encoding still runs for real (web3 does that offline), so the tests
    exercise the same calldata path the live bot uses.
    """

    def __init__(self, chain, abi, *, live: bool = False, base_fee: int = 1_000_000_000):
        super().__init__(chain, abi)
        self.live = live
        self._base_fee = base_fee
        self.sent: list[bytes] = []
        self.receipt_status = 1
        self.call_count = 0
        self.getter_value: Any = 0
        self.send_error: Exception | None = None
        self.has_code = True
        self.wallet_balance = 10**18

    def verify_chain_id(self) -> int:
        return self.chain.chain_id

    def base_fee(self) -> int:
        return self._base_fee

    def code_at(self, address: str) -> bytes:
        return b"\x60\x80\x60\x40" if self.has_code else b""

    def balance(self, address: str) -> int:
        return self.wallet_balance

    def pending_nonce(self, address: str) -> int:
        return 7

    def estimate_gas(self, tx: dict[str, Any]) -> int:
        if not self.live:
            raise ContractLogicError("execution reverted: mint not started")
        return 120_000

    def call(self, tx: dict[str, Any]) -> bytes:
        self.call_count += 1
        if tx.get("from") is None:  # a view read, e.g. the phase getter
            return int(self.getter_value).to_bytes(32, "big")
        if not self.live:
            raise ContractLogicError("execution reverted: mint not started")
        return b""

    def send_raw(self, raw: bytes) -> str:
        if self.send_error is not None:
            error, self.send_error = self.send_error, None
            raise error
        self.sent.append(raw)
        return "0x" + "ab" * 32

    def wait_for_receipt(self, tx_hash: str, timeout: float = 120.0) -> dict[str, Any]:
        return {"status": self.receipt_status, "blockNumber": 4663, "gasUsed": 90_000}


def base_config_dict(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "chain": {"rpc_url": "http://localhost:8545", "chain_id": 4663},
        "contract": {"address": CONTRACT},
        "mint": {"function": "mint", "args": ["{quantity}"], "price": "0 eth", "quantity": 1},
        "gas": {"max_fee_gwei": 5.0, "gas_limit": 250_000},
        "watch": {"poll_interval_ms": 50, "jitter_ms": 0},
        "safety": {"dry_run": False, "confirm": False},
    }
    for section, values in overrides.items():
        if isinstance(values, dict):
            config.setdefault(section, {}).update(values)
        else:
            config[section] = values
    return config


@pytest.fixture
def abi() -> list[dict[str, Any]]:
    return load_abi(None)


@pytest.fixture
def config():
    return parse_config(base_config_dict())


@pytest.fixture
def wallets() -> list[Wallet]:
    from eth_account import Account

    return [
        Wallet(label="a", address=Account.from_key(KEY_A).address, quantity=1, _key=KEY_A),
        Wallet(label="b", address=Account.from_key(KEY_B).address, quantity=2, _key=KEY_B),
    ]


@pytest.fixture
def client(config, abi) -> FakeClient:
    return FakeClient(config.chain, abi)
