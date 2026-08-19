"""Encrypted keystore handling for keys entered through the UI.

A pasted private key is encrypted in memory and written straight out as a
standard web3 keystore JSON. The plaintext key is never written to disk, never
stored in session state, and never returned to the caller.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from eth_utils import to_checksum_address

from .wallets import WalletError, _normalise_key

DEFAULT_KEYS_DIR = "keys"
# scrypt work factor: 2**18 is the eth-account default and takes ~1s to unlock.
# The bot decrypts once at arm time, well before the drop, so the cost is fine.
KDF_ITERATIONS = 2**18


@dataclass(frozen=True)
class StoredKey:
    label: str
    address: str
    path: Path

    @property
    def short(self) -> str:
        return f"{self.address[:6]}…{self.address[-4:]}"


def _safe_label(label: str) -> str:
    """Reduce a user-supplied label to something safe to use as a filename."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in label.strip())
    cleaned = cleaned.strip("-")
    if not cleaned:
        raise WalletError("label must contain at least one letter, digit, - or _")
    return cleaned[:48]


def address_for_key(private_key: str) -> str:
    """Derive the address for a key without storing anything."""
    return Account.from_key(_normalise_key(private_key, "input")).address


def create_keystore(
    label: str,
    private_key: str,
    password: str,
    keys_dir: str | Path = DEFAULT_KEYS_DIR,
    overwrite: bool = False,
) -> StoredKey:
    """Encrypt `private_key` into keys_dir/<label>.json and return its address."""
    if len(password) < 8:
        raise WalletError("keystore password must be at least 8 characters")

    safe = _safe_label(label)
    directory = Path(keys_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, stat.S_IRWXU)  # 0700 — owner only

    path = directory / f"{safe}.json"
    if path.exists() and not overwrite:
        raise WalletError(f"a keystore for '{safe}' already exists at {path}")

    normalised = _normalise_key(private_key, label)
    address = Account.from_key(normalised).address
    keyfile = Account.encrypt(normalised, password, kdf="scrypt", iterations=KDF_ITERATIONS)

    path.write_text(json.dumps(keyfile), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner only
    return StoredKey(label=safe, address=address, path=path)


def generate_keystores(
    count: int,
    password: str,
    keys_dir: str | Path = DEFAULT_KEYS_DIR,
    prefix: str = "wallet",
    start: int = 1,
) -> list[StoredKey]:
    """Create `count` brand-new wallets, encrypted on the way to disk.

    Keys come from eth-account's CSPRNG-backed generator. Nothing is returned
    but labels and addresses; the only copy of each key is the keystore file,
    so back that directory up before funding anything.
    """
    if count < 1:
        raise WalletError("count must be at least 1")

    created: list[StoredKey] = []
    index = start
    for _ in range(count):
        label = f"{prefix}{index}"
        while (Path(keys_dir).expanduser() / f"{_safe_label(label)}.json").exists():
            index += 1
            label = f"{prefix}{index}"
        account = Account.create()
        created.append(create_keystore(label, account.key.hex(), password, keys_dir=keys_dir))
        index += 1
    return created


def address_of_keystore(path: str | Path) -> str:
    """Read the address out of a keystore without needing the password."""
    keyfile = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    address = keyfile.get("address")
    if not address:
        raise WalletError(f"keystore '{path}' has no address field")
    # Keystores store the address unprefixed and lowercase.
    return to_checksum_address(address if address.startswith("0x") else "0x" + address)


def verify_password(path: str | Path, password: str) -> bool:
    """Check a password against a keystore. Used to fail early, before a drop."""
    keyfile = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    try:
        Account.decrypt(keyfile, password)
    except ValueError:
        return False
    return True


def list_keystores(keys_dir: str | Path = DEFAULT_KEYS_DIR) -> list[StoredKey]:
    directory = Path(keys_dir).expanduser()
    if not directory.exists():
        return []
    stored: list[StoredKey] = []
    for path in sorted(directory.glob("*.json")):
        try:
            stored.append(
                StoredKey(label=path.stem, address=address_of_keystore(path), path=path)
            )
        except (WalletError, json.JSONDecodeError):
            continue  # not a keystore; leave it alone
    return stored


def delete_keystore(path: str | Path) -> None:
    target = Path(path).expanduser()
    if target.exists():
        target.unlink()
