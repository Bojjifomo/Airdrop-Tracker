"""Logic behind the mint bot UI, kept out of the Streamlit file so it can be tested.

Streamlit modules are awkward to import under pytest, so everything here is
plain functions over paths and dataclasses. mintbot_ui.py is the thin view on top.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .keystore import (
    DEFAULT_KEYS_DIR, StoredKey, create_keystore, delete_keystore,
    generate_keystores, verify_password,
)
from .settings import SHARED_PASSWORD_ENV, ConfigDraft, WalletEntry, read_wallets, write_wallets
from .wallets import WalletError

# Environments where a pasted private key would leave the user's own machine.
HOSTED_MARKERS = (
    ("/mount/src", "Streamlit Community Cloud"),
)
HOSTED_ENV_VARS = (
    ("STREAMLIT_SHARING_MODE", "Streamlit sharing"),
    ("SPACE_ID", "Hugging Face Spaces"),
    ("K_SERVICE", "Google Cloud Run"),
    ("DYNO", "Heroku"),
    ("RENDER", "Render"),
    ("RAILWAY_ENVIRONMENT", "Railway"),
    ("CODESPACE_NAME", "GitHub Codespaces"),
)


def hosted_environment() -> str | None:
    """Name the hosting platform if this looks like someone else's machine.

    Key entry is blocked when this returns a name. MINTBOT_ALLOW_KEYS=1 is the
    deliberate override for a VPS the user controls themselves.
    """
    if os.environ.get("MINTBOT_ALLOW_KEYS") == "1":
        return None
    for marker, name in HOSTED_MARKERS:
        if Path(marker).exists():
            return name
    for var, name in HOSTED_ENV_VARS:
        if os.environ.get(var):
            return name
    return None


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "mintbot.toml"

    @property
    def wallets(self) -> Path:
        return self.root / "wallets.toml"

    @property
    def keys(self) -> Path:
        return self.root / DEFAULT_KEYS_DIR

    @property
    def abi(self) -> Path:
        return self.root / "abi" / "mint.json"

    @property
    def run_log(self) -> Path:
        return self.root / "mintbot.run.log"

    @property
    def events(self) -> Path:
        return self.root / "mintbot.jsonl"


# --------------------------------------------------------------------------- #
# wallets
# --------------------------------------------------------------------------- #
def add_wallet_from_key(
    workspace: Workspace,
    label: str,
    private_key: str,
    password: str,
    quantity: int = 1,
    proof: tuple[str, ...] = (),
) -> WalletEntry:
    """Encrypt a pasted key into a keystore and record the wallet.

    The plaintext key is used to derive the address and to encrypt, then
    dropped. It is never written anywhere except inside the encrypted keystore.
    """
    entries = read_wallets(workspace.wallets)
    if any(e.label == label for e in entries):
        raise WalletError(f"a wallet labelled '{label}' already exists")

    stored = create_keystore(label, private_key, password, keys_dir=workspace.keys)
    if any(e.address.lower() == stored.address.lower() for e in entries if e.address):
        delete_keystore(stored.path)
        raise WalletError(f"{stored.address} is already configured under another label")

    entry = WalletEntry(
        label=stored.label,
        address=stored.address,
        keystore=str(stored.path),
        password_env=SHARED_PASSWORD_ENV,
        quantity=quantity,
        proof=proof,
    )
    entry.validate()
    write_wallets(workspace.wallets, entries + [entry])
    return entry


def add_wallet_from_env(
    workspace: Workspace,
    label: str,
    key_env: str,
    address: str = "",
    quantity: int = 1,
    proof: tuple[str, ...] = (),
) -> WalletEntry:
    entries = read_wallets(workspace.wallets)
    if any(e.label == label for e in entries):
        raise WalletError(f"a wallet labelled '{label}' already exists")
    entry = WalletEntry(
        label=label, address=address, key_env=key_env, quantity=quantity, proof=proof
    )
    entry.validate()
    write_wallets(workspace.wallets, entries + [entry])
    return entry


def add_generated_wallets(
    workspace: Workspace,
    count: int,
    password: str,
    quantity: int = 1,
    prefix: str = "wallet",
) -> list[WalletEntry]:
    """Create fresh wallets and record them, in one step."""
    existing = read_wallets(workspace.wallets)
    taken = {e.label for e in existing}

    created = generate_keystores(count, password, keys_dir=workspace.keys, prefix=prefix)
    added = []
    for stored in created:
        if stored.label in taken:
            delete_keystore(stored.path)
            continue
        entry = WalletEntry(
            label=stored.label,
            address=stored.address,
            keystore=str(stored.path),
            password_env=SHARED_PASSWORD_ENV,
            quantity=quantity,
        )
        entry.validate()
        added.append(entry)
    write_wallets(workspace.wallets, existing + added)
    return added


def update_wallet(workspace: Workspace, label: str, **changes: Any) -> list[WalletEntry]:
    entries = read_wallets(workspace.wallets)
    for entry in entries:
        if entry.label == label:
            for name, value in changes.items():
                setattr(entry, name, value)
            entry.validate()
    write_wallets(workspace.wallets, entries)
    return entries


def remove_wallet(workspace: Workspace, label: str, drop_keystore: bool = True) -> list[WalletEntry]:
    entries = read_wallets(workspace.wallets)
    remaining = []
    for entry in entries:
        if entry.label != label:
            remaining.append(entry)
        elif drop_keystore and entry.keystore:
            delete_keystore(entry.keystore)
    write_wallets(workspace.wallets, remaining)
    return remaining


def check_keystore_password(entries: list[WalletEntry], password: str) -> list[str]:
    """Return the labels whose keystore does not open with this password."""
    bad = []
    for entry in entries:
        if entry.keystore and Path(entry.keystore).exists():
            if not verify_password(entry.keystore, password):
                bad.append(entry.label)
    return bad


@contextmanager
def keystore_password(password: str) -> Iterator[None]:
    """Expose the password to wallet loading for the length of one call only."""
    previous = os.environ.get(SHARED_PASSWORD_ENV)
    os.environ[SHARED_PASSWORD_ENV] = password
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(SHARED_PASSWORD_ENV, None)
        else:
            os.environ[SHARED_PASSWORD_ENV] = previous


def parse_proof(text: str) -> tuple[str, ...]:
    """Read a merkle proof pasted as one hex string per line, or as JSON."""
    text = text.strip()
    if not text:
        return ()
    if text.startswith("["):
        return tuple(str(p).strip() for p in json.loads(text))
    # Tolerate lines pasted straight out of JSON: quotes, commas and stray spaces.
    parts = [line.strip(" \t,\"'") for line in text.splitlines()]
    return tuple(p for p in parts if p)


def coerce_expect(text: str) -> Any:
    """Read a hand-typed phase value as a bool or int where that is what it is.

    Comparing a getter's bool against the string "True" would silently never
    match, so the type has to survive the trip through the text box.
    """
    cleaned = str(text).strip()
    if cleaned.lower() in ("true", "false"):
        return cleaned.lower() == "true"
    try:
        return int(cleaned, 0)
    except ValueError:
        return cleaned


# --------------------------------------------------------------------------- #
# running the bot as a subprocess
# --------------------------------------------------------------------------- #
def build_run_command(workspace: Workspace, live: bool) -> list[str]:
    command = [sys.executable, "-m", "mintbot", "-c", str(workspace.config), "run", "--yes"]
    if live:
        command.append("--live")
    return command


def start_run(workspace: Workspace, live: bool, password: str) -> subprocess.Popen:
    """Launch the bot in its own process, logging to the workspace run log."""
    environment = {**os.environ, SHARED_PASSWORD_ENV: password}
    workspace.run_log.write_text("", encoding="utf-8")
    handle = workspace.run_log.open("a", encoding="utf-8")
    return subprocess.Popen(
        build_run_command(workspace, live),
        cwd=str(workspace.root),
        env=environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )


def tail(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def read_events(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def reset_events(workspace: Workspace) -> None:
    for path in (workspace.events, workspace.run_log):
        if path.exists():
            path.unlink()


# --------------------------------------------------------------------------- #
# readiness
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Readiness:
    has_wallets: bool
    has_contract: bool
    has_abi: bool

    @property
    def ready(self) -> bool:
        return self.has_wallets and self.has_contract and self.has_abi

    @property
    def missing(self) -> list[str]:
        gaps = []
        if not self.has_wallets:
            gaps.append("add at least one wallet")
        if not self.has_contract:
            gaps.append("set the mint contract address")
        if not self.has_abi:
            gaps.append("fetch the contract ABI")
        return gaps


def readiness(workspace: Workspace, draft: ConfigDraft) -> Readiness:
    entries = read_wallets(workspace.wallets)
    address = draft.contract_address.strip()
    return Readiness(
        has_wallets=any(e.enabled for e in entries),
        has_contract=address.startswith("0x") and len(address) == 42,
        has_abi=workspace.abi.exists(),
    )
