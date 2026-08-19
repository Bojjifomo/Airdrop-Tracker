"""Wallet loading.

Private keys never live in this repo. A wallet entry points at either an
environment variable holding the key, or an encrypted keystore JSON file whose
password comes from the environment or an interactive prompt.
"""

from __future__ import annotations

import getpass
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eth_account import Account


class WalletError(ValueError):
    """Raised when a wallet entry cannot be resolved into a usable key."""


@dataclass
class Wallet:
    label: str
    address: str
    quantity: int = 1
    proof: tuple[str, ...] = ()
    _key: str = field(repr=False, default="")

    @property
    def key(self) -> str:
        return self._key

    def __repr__(self) -> str:  # keep keys out of tracebacks and logs
        return f"Wallet(label={self.label!r}, address={self.address!r}, quantity={self.quantity})"

    __str__ = __repr__

    @property
    def short(self) -> str:
        return f"{self.address[:6]}…{self.address[-4:]}"


def _normalise_key(raw: str, label: str) -> str:
    key = raw.strip().strip('"').strip("'")
    if not key:
        raise WalletError(f"wallet '{label}': private key is empty")
    if not key.startswith("0x"):
        key = "0x" + key
    if len(key) != 66:
        raise WalletError(
            f"wallet '{label}': private key must be 32 bytes of hex "
            f"(got {len(key) - 2} hex chars)"
        )
    try:
        int(key, 16)
    except ValueError as exc:
        raise WalletError(f"wallet '{label}': private key is not valid hex") from exc
    return key


def _key_from_env(entry: dict[str, Any], label: str) -> str:
    var = str(entry["key_env"])
    raw = os.environ.get(var)
    if not raw:
        raise WalletError(
            f"wallet '{label}': environment variable {var} is not set. "
            f"Export it (or source your gitignored env file) before running."
        )
    return _normalise_key(raw, label)


def _key_from_keystore(entry: dict[str, Any], label: str, allow_prompt: bool) -> str:
    path = Path(str(entry["keystore"])).expanduser()
    if not path.exists():
        raise WalletError(f"wallet '{label}': keystore file '{path}' not found")

    password_env = entry.get("password_env")
    if password_env:
        password = os.environ.get(str(password_env))
        if password is None:
            raise WalletError(
                f"wallet '{label}': environment variable {password_env} is not set"
            )
    elif allow_prompt:
        password = getpass.getpass(f"Password for keystore '{label}' ({path.name}): ")
    else:
        raise WalletError(
            f"wallet '{label}': set password_env, or run interactively to be prompted"
        )

    with path.open("r", encoding="utf-8") as handle:
        keyfile = json.load(handle)
    try:
        private_key = Account.decrypt(keyfile, password)
    except ValueError as exc:
        raise WalletError(f"wallet '{label}': could not decrypt keystore ({exc})") from exc
    return _normalise_key(private_key.hex(), label)


def _load_entry(entry: dict[str, Any], index: int, allow_prompt: bool) -> Wallet:
    label = str(entry.get("label") or f"wallet{index + 1}")

    sources = [k for k in ("key_env", "keystore") if entry.get(k)]
    if not sources:
        raise WalletError(f"wallet '{label}': set either key_env or keystore")
    if len(sources) > 1:
        raise WalletError(f"wallet '{label}': set only one of key_env or keystore")

    key = (
        _key_from_env(entry, label)
        if sources[0] == "key_env"
        else _key_from_keystore(entry, label, allow_prompt)
    )
    address = Account.from_key(key).address

    expected = entry.get("address")
    if expected and expected.lower() != address.lower():
        raise WalletError(
            f"wallet '{label}': key derives {address}, but config expects {expected}. "
            f"Refusing to run with a mismatched key."
        )

    quantity = int(entry.get("quantity", 0)) or 0
    proof = tuple(str(p) for p in entry.get("proof", []))
    return Wallet(label=label, address=address, quantity=quantity, proof=proof, _key=key)


def load_wallets(
    path: str | Path,
    default_quantity: int = 1,
    max_per_wallet: int | None = None,
    allow_prompt: bool = True,
) -> list[Wallet]:
    """Read the wallets file and resolve every enabled entry into a Wallet."""
    path = Path(path).expanduser()
    if not path.exists():
        raise WalletError(
            f"wallets file '{path}' not found — copy wallets.example.toml to {path.name}"
        )
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    entries = raw.get("wallet", [])
    if not entries:
        raise WalletError(f"'{path}' has no [[wallet]] entries")

    wallets: list[Wallet] = []
    seen: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not entry.get("enabled", True):
            continue
        wallet = _load_entry(entry, index, allow_prompt)

        if wallet.quantity <= 0:
            wallet.quantity = default_quantity
        if max_per_wallet is not None and wallet.quantity > max_per_wallet:
            raise WalletError(
                f"wallet '{wallet.label}': quantity {wallet.quantity} exceeds "
                f"[mint].max_per_wallet ({max_per_wallet})"
            )

        duplicate = seen.get(wallet.address.lower())
        if duplicate:
            raise WalletError(
                f"wallet '{wallet.label}' has the same address as '{duplicate}' — "
                f"two entries for one address would race each other's nonce"
            )
        seen[wallet.address.lower()] = wallet.label
        wallets.append(wallet)

    if not wallets:
        raise WalletError(f"'{path}' has no enabled wallets")
    return wallets
