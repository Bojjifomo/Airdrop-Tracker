"""Reading and writing the two TOML files the bot runs on.

The CLI only ever reads these files; the UI needs to write them too. Generated
files keep their explanatory comments, so a file written by the UI is the same
thing you would have hand-edited.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import FIRE_MODES, MAX_SALVO, WATCH_MODES, ConfigError
from .wallets import WalletError

SHARED_PASSWORD_ENV = "MINTBOT_PASSWORD"


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_list(values) -> str:
    return "[" + ", ".join(_toml_str(str(v)) for v in values) + "]"


# --------------------------------------------------------------------------- #
# wallets.toml
# --------------------------------------------------------------------------- #
@dataclass
class WalletEntry:
    """One wallet as it appears on disk — a key *reference*, never a key."""

    label: str
    address: str = ""
    keystore: str | None = None
    password_env: str | None = None
    key_env: str | None = None
    quantity: int = 1
    proof: tuple[str, ...] = ()
    enabled: bool = True

    @property
    def source(self) -> str:
        return "keystore" if self.keystore else "env" if self.key_env else "unset"

    @property
    def short(self) -> str:
        return f"{self.address[:6]}…{self.address[-4:]}" if self.address else "(unknown)"

    def validate(self) -> None:
        if not self.label.strip():
            raise WalletError("every wallet needs a label")
        if bool(self.keystore) == bool(self.key_env):
            raise WalletError(
                f"wallet '{self.label}': set exactly one of keystore or key_env"
            )
        if self.quantity < 1:
            raise WalletError(f"wallet '{self.label}': quantity must be at least 1")


def read_wallets(path: str | Path) -> list[WalletEntry]:
    """Read wallets.toml without resolving any key material."""
    target = Path(path).expanduser()
    if not target.exists():
        return []
    with target.open("rb") as handle:
        raw = tomllib.load(handle)
    entries = []
    for item in raw.get("wallet", []):
        entries.append(
            WalletEntry(
                label=str(item.get("label", "")),
                address=str(item.get("address", "")),
                keystore=item.get("keystore"),
                password_env=item.get("password_env"),
                key_env=item.get("key_env"),
                quantity=int(item.get("quantity", 1)),
                proof=tuple(str(p) for p in item.get("proof", [])),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return entries


def render_wallets_toml(entries: list[WalletEntry]) -> str:
    lines = [
        "# Written by the mint bot UI. Private keys are NOT in this file — each",
        "# entry points at an encrypted keystore or an environment variable.",
        "# This file is gitignored; keep it that way.",
        "",
    ]
    for entry in entries:
        entry.validate()
        lines.append("[[wallet]]")
        lines.append(f"label = {_toml_str(entry.label)}")
        if entry.address:
            lines.append(f"address = {_toml_str(entry.address)}")
        if entry.keystore:
            lines.append(f"keystore = {_toml_str(entry.keystore)}")
            if entry.password_env:
                lines.append(f"password_env = {_toml_str(entry.password_env)}")
        else:
            lines.append(f"key_env = {_toml_str(entry.key_env or '')}")
        lines.append(f"quantity = {entry.quantity}")
        if entry.proof:
            lines.append(f"proof = {_toml_list(entry.proof)}")
        if not entry.enabled:
            lines.append("enabled = false")
        lines.append("")
    return "\n".join(lines)


def write_wallets(path: str | Path, entries: list[WalletEntry]) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_wallets_toml(entries), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# mintbot.toml
# --------------------------------------------------------------------------- #
@dataclass
class ConfigDraft:
    """The subset of mintbot.toml the UI edits, with Robinhood Chain defaults."""

    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    chain_id: int = 4663
    fallback_rpc_urls: tuple[str, ...] = ()
    explorer_api: str = "https://robinhoodchain.blockscout.com/api"

    contract_address: str = ""
    abi_file: str = "abi/mint.json"

    mint_function: str = "mint"
    mint_args: tuple[str, ...] = ("{quantity}",)
    price: str = "0 eth"
    quantity: int = 1
    max_per_wallet: int = 1

    max_fee_gwei: float = 5.0
    priority_fee_gwei: float = 0.01
    gas_limit: int = 250_000

    fire_mode: str = "probe"
    fire_at: str = ""
    fire_transactions: int = 1
    fire_interval_ms: int = 120
    fire_rebroadcast: bool = False

    postmint_enabled: bool = False
    postmint_destination: str = ""

    watch_mode: str = "simulate"
    poll_interval_ms: int = 400
    getter_function: str = ""
    getter_expect: Any = True
    start_at: str = ""

    dry_run: bool = True
    max_total_spend_eth: float = 0.5
    max_attempts_per_wallet: int = 3
    stop_on_first_success: bool = False

    wallets_file: str = "wallets.toml"
    log_file: str = "mintbot.jsonl"

    def validate(self) -> None:
        address = self.contract_address.strip()
        if not address.startswith("0x") or len(address) != 42:
            raise ConfigError(
                f"contract address '{address or '(empty)'}' is not a 20-byte hex address"
            )
        if self.watch_mode not in WATCH_MODES:
            raise ConfigError(f"watch mode must be one of {', '.join(WATCH_MODES)}")
        if self.watch_mode in ("getter", "both") and not self.getter_function:
            raise ConfigError(f"watch mode '{self.watch_mode}' needs a phase getter function")
        if self.fire_mode not in FIRE_MODES:
            raise ConfigError(f"fire mode must be one of {', '.join(FIRE_MODES)}")
        if self.fire_mode == "instant" and not (self.fire_at or self.start_at):
            raise ConfigError("instant firing needs a time to fire at")
        if not 1 <= self.fire_transactions <= MAX_SALVO:
            raise ConfigError(f"transactions per wallet must be between 1 and {MAX_SALVO}")
        if self.postmint_enabled:
            target = self.postmint_destination.strip()
            if not target.startswith("0x") or len(target) != 42:
                raise ConfigError(
                    "post-mint transfers need a destination address to send tokens to"
                )
        if self.quantity > self.max_per_wallet:
            raise ConfigError("quantity cannot exceed max per wallet")
        if self.max_fee_gwei <= 0:
            raise ConfigError("max fee must be greater than 0")


def _format_expect(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _toml_str(str(value))


def render_config_toml(draft: ConfigDraft) -> str:
    draft.validate()
    lines = [
        "# Written by the mint bot UI. Safe to hand-edit — the UI reads it back.",
        "",
        f"wallets_file = {_toml_str(draft.wallets_file)}",
        f"log_file = {_toml_str(draft.log_file)}",
        "",
        "[chain]",
        f"rpc_url = {_toml_str(draft.rpc_url)}",
        f"chain_id = {draft.chain_id}",
        "# Put a dedicated provider first on drop day; the public endpoint throttles.",
        f"fallback_rpc_urls = {_toml_list(draft.fallback_rpc_urls)}",
        f"explorer_api = {_toml_str(draft.explorer_api)}",
        "",
        "[contract]",
        f"address = {_toml_str(draft.contract_address.strip())}",
        f"abi_file = {_toml_str(draft.abi_file)}",
        "",
        "[mint]",
        "# Placeholders are filled in per wallet: {quantity}, {address}, {proof}.",
        f"function = {_toml_str(draft.mint_function)}",
        f"args = {_toml_list(draft.mint_args)}",
        f"price = {_toml_str(draft.price)}",
        f"quantity = {draft.quantity}",
        f"max_per_wallet = {draft.max_per_wallet}",
        "",
        "[gas]",
        "# The bot holds rather than pay above this ceiling.",
        f"max_fee_gwei = {draft.max_fee_gwei}",
        f"priority_fee_gwei = {draft.priority_fee_gwei}",
        f"gas_limit = {draft.gas_limit}",
        "",
        "[watch]",
        f"mode = {_toml_str(draft.watch_mode)}",
        f"poll_interval_ms = {draft.poll_interval_ms}",
    ]
    if draft.start_at:
        lines.append(f"start_at = {_toml_str(draft.start_at)}")

    lines += [
        "",
        "[fire]",
        "# probe waits for the contract to accept the call; instant goes on time.",
        f"mode = {_toml_str(draft.fire_mode)}",
    ]
    if draft.fire_at:
        lines.append(f"at = {_toml_str(draft.fire_at)}")
    lines += [
        "# More than one transaction per wallet means the extras revert once the",
        "# wallet's allowance is used up — you pay their gas either way.",
        f"transactions = {draft.fire_transactions}",
        f"interval_ms = {draft.fire_interval_ms}",
        f"rebroadcast = {'true' if draft.fire_rebroadcast else 'false'}",
        "",
        "[postmint]",
        "# Move tokens to a wallet whose key the bot does not hold, the moment they land.",
        f"enabled = {'true' if draft.postmint_enabled else 'false'}",
    ]
    if draft.postmint_destination:
        lines.append(f"destination = {_toml_str(draft.postmint_destination.strip())}")

    lines += ["", "[watch.getter]"]
    if draft.getter_function:
        lines.append(f"function = {_toml_str(draft.getter_function)}")
        lines.append(f"expect = {_format_expect(draft.getter_expect)}")
    else:
        lines.append("# unused while watch mode is \"simulate\"")

    lines += [
        "",
        "[safety]",
        f"dry_run = {'true' if draft.dry_run else 'false'}",
        f"max_total_spend_eth = {draft.max_total_spend_eth}",
        f"max_attempts_per_wallet = {draft.max_attempts_per_wallet}",
        "confirm = false   # the UI asks for confirmation itself",
        f"stop_on_first_success = {'true' if draft.stop_on_first_success else 'false'}",
        "",
    ]
    return "\n".join(lines)


def write_config(path: str | Path, draft: ConfigDraft) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config_toml(draft), encoding="utf-8")
    return target


def read_draft(path: str | Path) -> ConfigDraft:
    """Load a config back into a draft, falling back to defaults per field."""
    target = Path(path).expanduser()
    if not target.exists():
        return ConfigDraft()
    with target.open("rb") as handle:
        raw = tomllib.load(handle)

    chain = raw.get("chain", {})
    contract = raw.get("contract", {})
    mint = raw.get("mint", {})
    gas = raw.get("gas", {})
    watch = raw.get("watch", {})
    getter = watch.get("getter", {})
    fire = raw.get("fire", {})
    postmint = raw.get("postmint", {})
    safety = raw.get("safety", {})
    default = ConfigDraft()

    return ConfigDraft(
        rpc_url=chain.get("rpc_url", default.rpc_url),
        chain_id=int(chain.get("chain_id", default.chain_id)),
        fallback_rpc_urls=tuple(chain.get("fallback_rpc_urls", ())),
        explorer_api=chain.get("explorer_api", default.explorer_api),
        contract_address=contract.get("address", default.contract_address),
        abi_file=contract.get("abi_file", default.abi_file),
        mint_function=mint.get("function", default.mint_function),
        mint_args=tuple(mint.get("args", default.mint_args)),
        price=str(mint.get("price", default.price)),
        quantity=int(mint.get("quantity", default.quantity)),
        max_per_wallet=int(mint.get("max_per_wallet", default.max_per_wallet)),
        max_fee_gwei=float(gas.get("max_fee_gwei", default.max_fee_gwei)),
        priority_fee_gwei=float(gas.get("priority_fee_gwei", default.priority_fee_gwei)),
        gas_limit=int(gas.get("gas_limit", default.gas_limit) or default.gas_limit),
        fire_mode=fire.get("mode", default.fire_mode),
        fire_at=str(fire.get("at", "") or ""),
        fire_transactions=int(fire.get("transactions", default.fire_transactions)),
        fire_interval_ms=int(fire.get("interval_ms", default.fire_interval_ms)),
        fire_rebroadcast=bool(fire.get("rebroadcast", default.fire_rebroadcast)),
        postmint_enabled=bool(postmint.get("enabled", default.postmint_enabled)),
        postmint_destination=str(postmint.get("destination", "") or ""),
        watch_mode=watch.get("mode", default.watch_mode),
        poll_interval_ms=int(watch.get("poll_interval_ms", default.poll_interval_ms)),
        getter_function=getter.get("function", "") or "",
        getter_expect=getter.get("expect", default.getter_expect),
        start_at=str(watch.get("start_at", "") or ""),
        dry_run=bool(safety.get("dry_run", default.dry_run)),
        max_total_spend_eth=float(safety.get("max_total_spend_eth", default.max_total_spend_eth)),
        max_attempts_per_wallet=int(
            safety.get("max_attempts_per_wallet", default.max_attempts_per_wallet)
        ),
        stop_on_first_success=bool(
            safety.get("stop_on_first_success", default.stop_on_first_success)
        ),
        wallets_file=raw.get("wallets_file", default.wallets_file),
        log_file=raw.get("log_file", default.log_file),
    )
