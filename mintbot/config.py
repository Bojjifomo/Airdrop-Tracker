"""Configuration loading and validation for mintbot.

Everything the bot does is driven by a TOML file (see mintbot.example.toml).
Nothing secret lives here — private keys are resolved separately in wallets.py.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WEI_PER_ETH = 10**18
WEI_PER_GWEI = 10**9

WATCH_MODES = ("simulate", "getter", "both")
FIRE_MODES = ("probe", "instant")
# A salvo larger than this is RPC abuse, not a strategy.
MAX_SALVO = 10


class ConfigError(ValueError):
    """Raised when a config file is missing required values or malformed."""


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChainConfig:
    rpc_url: str
    chain_id: int
    fallback_rpc_urls: tuple[str, ...] = ()
    explorer_api: str | None = None
    request_timeout: float = 10.0

    @property
    def rpc_urls(self) -> tuple[str, ...]:
        """Primary endpoint first, then any fallbacks, de-duplicated."""
        seen, ordered = set(), []
        for url in (self.rpc_url, *self.fallback_rpc_urls):
            if url and url not in seen:
                seen.add(url)
                ordered.append(url)
        return tuple(ordered)


@dataclass(frozen=True)
class ContractConfig:
    address: str
    abi_path: str | None = None


@dataclass(frozen=True)
class MintConfig:
    function: str
    args: tuple[Any, ...]
    price_wei: int
    quantity: int
    max_per_wallet: int


@dataclass(frozen=True)
class GasConfig:
    max_fee_gwei: float
    priority_fee_gwei: float
    gas_limit: int | None = None          # None -> estimate against the node
    gas_limit_buffer: float = 1.30        # applied to estimates only
    bump_percent: int = 25                # per retry, compounding
    resign_interval_s: float = 5.0
    legacy: bool = False                  # pre-EIP-1559 gasPrice txs

    @property
    def max_fee_wei(self) -> int:
        return int(self.max_fee_gwei * WEI_PER_GWEI)

    @property
    def priority_fee_wei(self) -> int:
        return int(self.priority_fee_gwei * WEI_PER_GWEI)


@dataclass(frozen=True)
class WatchConfig:
    poll_interval_ms: int = 400
    jitter_ms: int = 120
    mode: str = "simulate"
    getter_function: str | None = None
    getter_args: tuple[Any, ...] = ()
    getter_expect: Any = None
    start_at: datetime | None = None      # do not probe before this instant
    deadline_at: datetime | None = None   # give up after this instant

    @property
    def poll_interval_s(self) -> float:
        return self.poll_interval_ms / 1000.0

    @property
    def jitter_s(self) -> float:
        return self.jitter_ms / 1000.0


@dataclass(frozen=True)
class FireConfig:
    """How the transaction goes out once the moment arrives."""

    mode: str = "probe"                 # probe = wait for the contract; instant = go on time
    at: datetime | None = None          # instant mode: the exact moment to fire
    transactions: int = 1               # sequential-nonce transactions per wallet
    interval_ms: int = 120              # gap between them
    rebroadcast: bool = False           # push signed bytes to every endpoint at once

    @property
    def interval_s(self) -> float:
        return self.interval_ms / 1000.0


@dataclass(frozen=True)
class PostMintConfig:
    """What happens to a token the moment it lands."""

    enabled: bool = False
    destination: str = ""          # a wallet whose key the bot does not hold

    def validate(self) -> None:
        if not self.enabled:
            return
        target = self.destination.strip()
        if not target.startswith("0x") or len(target) != 42:
            raise ConfigError(
                "[postmint].enabled is on, so [postmint].destination must be a "
                "20-byte hex address to send minted tokens to"
            )


@dataclass(frozen=True)
class SafetyConfig:
    dry_run: bool = True
    max_total_spend_eth: float = 0.0      # 0 -> derived from price * quantity
    max_attempts_per_wallet: int = 3
    confirm: bool = True
    stop_on_first_success: bool = False  # True -> whole run stops once any wallet mints

    @property
    def max_total_spend_wei(self) -> int:
        return int(self.max_total_spend_eth * WEI_PER_ETH)


@dataclass(frozen=True)
class Config:
    chain: ChainConfig
    contract: ContractConfig
    mint: MintConfig
    gas: GasConfig
    watch: WatchConfig
    fire: FireConfig
    postmint: PostMintConfig
    safety: SafetyConfig
    wallets_file: str = "wallets.toml"
    log_file: str = "mintbot.jsonl"
    source_path: Path | None = field(default=None, compare=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require(table: dict[str, Any], key: str, section: str) -> Any:
    if key not in table:
        raise ConfigError(f"[{section}] is missing required key '{key}'")
    return table[key]


def _as_wei(value: Any, section: str, key: str) -> int:
    """Accept an int, a decimal string, or a '<amount> <unit>' string."""
    if isinstance(value, bool):
        raise ConfigError(f"[{section}].{key} must be a number, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        parts = value.strip().split()
        if len(parts) == 1:
            try:
                return int(parts[0], 0)
            except ValueError as exc:
                raise ConfigError(f"[{section}].{key}: cannot parse '{value}' as wei") from exc
        if len(parts) == 2:
            amount, unit = parts
            factor = {"wei": 1, "gwei": WEI_PER_GWEI, "eth": WEI_PER_ETH}.get(unit.lower())
            if factor is None:
                raise ConfigError(f"[{section}].{key}: unknown unit '{unit}' (use wei/gwei/eth)")
            return int(float(amount) * factor)
    raise ConfigError(f"[{section}].{key}: cannot parse '{value!r}' as an amount")


def _as_datetime(value: Any, section: str, key: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigError(f"[{section}].{key}: '{value}' is not an ISO-8601 timestamp") from exc
    else:
        raise ConfigError(f"[{section}].{key}: expected an ISO-8601 timestamp")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_args(args: tuple[Any, ...], context: dict[str, Any]) -> list[Any]:
    """Substitute {placeholders} in mint arguments with per-wallet values.

    A bare "{quantity}" becomes the int itself rather than a string, so lists
    and ints survive templating; "{proof}" expands to the wallet's merkle proof.
    """
    resolved: list[Any] = []
    for arg in args:
        if isinstance(arg, str) and arg.startswith("{") and arg.endswith("}"):
            name = arg[1:-1]
            if name not in context:
                raise ConfigError(
                    f"mint argument '{arg}' has no value "
                    f"(known placeholders: {', '.join(sorted(context))})"
                )
            resolved.append(context[name])
        elif isinstance(arg, str) and "{" in arg:
            resolved.append(arg.format(**context))
        else:
            resolved.append(arg)
    return resolved


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(
            f"config file '{path}' not found — copy mintbot.example.toml to {path.name} first"
        )
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return parse_config(raw, source_path=path)


def parse_config(raw: dict[str, Any], source_path: Path | None = None) -> Config:
    chain_raw = raw.get("chain", {})
    chain = ChainConfig(
        rpc_url=str(_require(chain_raw, "rpc_url", "chain")),
        chain_id=int(_require(chain_raw, "chain_id", "chain")),
        fallback_rpc_urls=tuple(str(u) for u in chain_raw.get("fallback_rpc_urls", [])),
        explorer_api=chain_raw.get("explorer_api") or None,
        request_timeout=float(chain_raw.get("request_timeout", 10.0)),
    )

    contract_raw = raw.get("contract", {})
    address = str(_require(contract_raw, "address", "contract")).strip()
    if not address.startswith("0x") or len(address) != 42:
        raise ConfigError(f"[contract].address '{address}' is not a 20-byte hex address")
    contract = ContractConfig(address=address, abi_path=contract_raw.get("abi_file") or None)

    mint_raw = raw.get("mint", {})
    quantity = int(mint_raw.get("quantity", 1))
    max_per_wallet = int(mint_raw.get("max_per_wallet", quantity))
    if quantity < 1:
        raise ConfigError("[mint].quantity must be at least 1")
    if quantity > max_per_wallet:
        raise ConfigError(
            f"[mint].quantity ({quantity}) exceeds [mint].max_per_wallet ({max_per_wallet})"
        )
    mint = MintConfig(
        function=str(_require(mint_raw, "function", "mint")),
        args=tuple(mint_raw.get("args", [])),
        price_wei=_as_wei(mint_raw.get("price", 0), "mint", "price"),
        quantity=quantity,
        max_per_wallet=max_per_wallet,
    )

    gas_raw = raw.get("gas", {})
    gas_limit = gas_raw.get("gas_limit")
    if isinstance(gas_limit, str) and gas_limit.lower() in ("estimate", "auto", ""):
        gas_limit = None
    gas = GasConfig(
        max_fee_gwei=float(_require(gas_raw, "max_fee_gwei", "gas")),
        priority_fee_gwei=float(gas_raw.get("priority_fee_gwei", 0.01)),
        gas_limit=int(gas_limit) if gas_limit is not None else None,
        gas_limit_buffer=float(gas_raw.get("gas_limit_buffer", 1.30)),
        bump_percent=int(gas_raw.get("bump_percent", 25)),
        resign_interval_s=float(gas_raw.get("resign_interval_s", 5.0)),
        legacy=bool(gas_raw.get("legacy", False)),
    )
    if gas.max_fee_gwei <= 0:
        raise ConfigError("[gas].max_fee_gwei must be greater than 0")
    if gas.priority_fee_wei > gas.max_fee_wei:
        raise ConfigError("[gas].priority_fee_gwei cannot exceed max_fee_gwei")

    watch_raw = raw.get("watch", {})
    mode = str(watch_raw.get("mode", "simulate")).lower()
    if mode not in WATCH_MODES:
        raise ConfigError(f"[watch].mode must be one of {', '.join(WATCH_MODES)} (got '{mode}')")
    getter_raw = watch_raw.get("getter", {})
    if mode in ("getter", "both") and not getter_raw.get("function"):
        raise ConfigError(f"[watch].mode = '{mode}' requires [watch.getter].function")
    watch = WatchConfig(
        poll_interval_ms=int(watch_raw.get("poll_interval_ms", 400)),
        jitter_ms=int(watch_raw.get("jitter_ms", 120)),
        mode=mode,
        getter_function=getter_raw.get("function") or None,
        getter_args=tuple(getter_raw.get("args", [])),
        getter_expect=getter_raw.get("expect"),
        start_at=_as_datetime(watch_raw.get("start_at"), "watch", "start_at"),
        deadline_at=_as_datetime(watch_raw.get("deadline_at"), "watch", "deadline_at"),
    )
    if watch.poll_interval_ms < 50:
        raise ConfigError("[watch].poll_interval_ms below 50 will get you rate-limited; raise it")
    if watch.start_at and watch.deadline_at and watch.deadline_at <= watch.start_at:
        raise ConfigError("[watch].deadline_at must be after start_at")

    fire_raw = raw.get("fire", {})
    fire_mode = str(fire_raw.get("mode", "probe")).lower()
    if fire_mode not in FIRE_MODES:
        raise ConfigError(f"[fire].mode must be one of {', '.join(FIRE_MODES)} (got '{fire_mode}')")
    transactions = int(fire_raw.get("transactions", 1))
    if not 1 <= transactions <= MAX_SALVO:
        raise ConfigError(f"[fire].transactions must be between 1 and {MAX_SALVO}")
    fire = FireConfig(
        mode=fire_mode,
        at=_as_datetime(fire_raw.get("at"), "fire", "at"),
        transactions=transactions,
        interval_ms=int(fire_raw.get("interval_ms", 120)),
        rebroadcast=bool(fire_raw.get("rebroadcast", False)),
    )
    if fire.mode == "instant" and fire.at is None and watch.start_at is None:
        raise ConfigError(
            "[fire].mode = 'instant' fires without checking the contract, so it needs "
            "[fire].at (or [watch].start_at) to say when"
        )

    postmint_raw = raw.get("postmint", {})
    postmint = PostMintConfig(
        enabled=bool(postmint_raw.get("enabled", False)),
        destination=str(postmint_raw.get("destination", "")).strip(),
    )
    postmint.validate()

    safety_raw = raw.get("safety", {})
    safety = SafetyConfig(
        dry_run=bool(safety_raw.get("dry_run", True)),
        max_total_spend_eth=float(safety_raw.get("max_total_spend_eth", 0.0)),
        max_attempts_per_wallet=int(safety_raw.get("max_attempts_per_wallet", 3)),
        confirm=bool(safety_raw.get("confirm", True)),
        stop_on_first_success=bool(safety_raw.get("stop_on_first_success", False)),
    )
    if safety.max_attempts_per_wallet < 1:
        raise ConfigError("[safety].max_attempts_per_wallet must be at least 1")

    return Config(
        chain=chain,
        contract=contract,
        mint=mint,
        gas=gas,
        watch=watch,
        fire=fire,
        postmint=postmint,
        safety=safety,
        wallets_file=str(raw.get("wallets_file", "wallets.toml")),
        log_file=str(raw.get("log_file", "mintbot.jsonl")),
        source_path=source_path,
    )
