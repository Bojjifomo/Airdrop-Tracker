"""RPC client, ABI plumbing, and gas policy.

The client keeps a list of endpoints and rotates to the next one whenever a
request fails, so a rate-limited public RPC does not take the bot down mid-drop.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from web3 import Web3
from web3.exceptions import ContractLogicError

from .config import ChainConfig, GasConfig

T = TypeVar("T")

DEFAULT_ABI_PATH = Path(__file__).parent / "abi" / "common_mint.json"


class ChainError(RuntimeError):
    """Raised when every configured RPC endpoint fails a request."""


class GasTooHigh(RuntimeError):
    """Raised when the network base fee sits above the configured ceiling."""


# --------------------------------------------------------------------------- #
# ABI helpers
# --------------------------------------------------------------------------- #
def load_abi(path: str | Path | None) -> list[dict[str, Any]]:
    """Load an ABI file, falling back to the bundled common-signature set."""
    target = Path(path).expanduser() if path else DEFAULT_ABI_PATH
    if not target.exists():
        raise ChainError(f"ABI file '{target}' not found — run `mintbot discover` to fetch it")
    with target.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    # Explorers sometimes wrap the ABI in {"result": "[...]"} or {"abi": [...]}.
    if isinstance(data, dict):
        data = data.get("abi", data.get("result", data))
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, list):
        raise ChainError(f"ABI file '{target}' does not contain a JSON array")
    return data


def functions_named(abi: Iterable[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in abi if e.get("type") == "function" and e.get("name") == name]


def signature_of(entry: dict[str, Any]) -> str:
    return f"{entry['name']}({','.join(i['type'] for i in entry.get('inputs', []))})"


def resolve_overload(abi: list[dict[str, Any]], name: str, args: list[Any]) -> dict[str, Any]:
    """Pick the ABI entry for `name` whose arity matches the supplied args."""
    candidates = functions_named(abi, name)
    if not candidates:
        available = sorted({e["name"] for e in abi if e.get("type") == "function"})
        raise ChainError(
            f"function '{name}' is not in the ABI. Available: {', '.join(available) or '(none)'}"
        )
    exact = [e for e in candidates if len(e.get("inputs", [])) == len(args)]
    if not exact:
        arities = sorted({len(e.get("inputs", [])) for e in candidates})
        raise ChainError(
            f"'{name}' takes {' or '.join(str(a) for a in arities)} argument(s), "
            f"but [mint].args supplies {len(args)}"
        )
    if len(exact) > 1:
        raise ChainError(
            f"'{name}' is overloaded with several {len(args)}-argument forms; "
            f"trim the ABI file to the one you want ({', '.join(signature_of(e) for e in exact)})"
        )
    return exact[0]


# --------------------------------------------------------------------------- #
# gas
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FeeParams:
    max_fee_wei: int
    priority_fee_wei: int
    base_fee_wei: int
    legacy: bool = False

    def as_tx_fields(self) -> dict[str, int]:
        if self.legacy:
            return {"gasPrice": self.max_fee_wei}
        return {"maxFeePerGas": self.max_fee_wei, "maxPriorityFeePerGas": self.priority_fee_wei}

    def bumped(self, percent: int, ceiling_wei: int) -> "FeeParams":
        """Raise both fees by `percent`, clamped to the configured ceiling."""
        factor = 1 + percent / 100
        return FeeParams(
            max_fee_wei=min(int(self.max_fee_wei * factor), ceiling_wei),
            priority_fee_wei=min(int(self.priority_fee_wei * factor), ceiling_wei),
            base_fee_wei=self.base_fee_wei,
            legacy=self.legacy,
        )


def compute_fees(base_fee_wei: int, gas: GasConfig) -> FeeParams:
    """Target 2x base fee plus tip, never exceeding the configured ceiling."""
    ceiling = gas.max_fee_wei
    if base_fee_wei > ceiling:
        raise GasTooHigh(
            f"base fee is {base_fee_wei / 1e9:.3f} gwei, above your "
            f"[gas].max_fee_gwei ceiling of {gas.max_fee_gwei} gwei"
        )
    if gas.legacy:
        target = min(max(base_fee_wei + gas.priority_fee_wei, gas.priority_fee_wei), ceiling)
        return FeeParams(target, target, base_fee_wei, legacy=True)
    priority = min(gas.priority_fee_wei, ceiling)
    target = min(base_fee_wei * 2 + priority, ceiling)
    return FeeParams(max(target, priority), priority, base_fee_wei, legacy=False)


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class ChainClient:
    """A thread-safe Web3 wrapper with endpoint failover."""

    def __init__(self, chain: ChainConfig, abi: list[dict[str, Any]] | None = None):
        self.chain = chain
        self.abi = abi if abi is not None else []
        self._urls = chain.rpc_urls
        if not self._urls:
            raise ChainError("no RPC endpoints configured")
        self._lock = threading.RLock()
        self._index = 0
        self._clients: dict[str, Web3] = {}

    # -- endpoint management -------------------------------------------------
    def _client_for(self, url: str) -> Web3:
        with self._lock:
            client = self._clients.get(url)
            if client is not None:
                return client
            client = Web3(
                Web3.HTTPProvider(url, request_kwargs={"timeout": self.chain.request_timeout})
            )
            self._clients[url] = client
            return client

    @property
    def w3(self) -> Web3:
        with self._lock:
            return self._client_for(self._urls[self._index])

    @property
    def endpoint(self) -> str:
        with self._lock:
            return self._urls[self._index]

    def _rotate(self) -> None:
        with self._lock:
            self._index = (self._index + 1) % len(self._urls)

    def run(self, operation: Callable[[Web3], T]) -> T:
        """Run `operation`, trying each endpoint once before giving up.

        Contract reverts are surfaced immediately: they are an answer from the
        chain, not an endpoint failure, so retrying elsewhere would be pointless.
        """
        errors: list[str] = []
        for _ in range(len(self._urls)):
            url = self.endpoint
            try:
                return operation(self._client_for(url))
            except ContractLogicError:
                raise
            except Exception as exc:  # noqa: BLE001 - any transport error means "try the next one"
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                self._rotate()
        raise ChainError("all RPC endpoints failed — " + " | ".join(errors))

    # -- reads ---------------------------------------------------------------
    def verify_chain_id(self) -> int:
        actual = self.run(lambda w3: w3.eth.chain_id)
        if actual != self.chain.chain_id:
            raise ChainError(
                f"RPC reports chain id {actual}, but config says {self.chain.chain_id}"
            )
        return actual

    def block_number(self) -> int:
        return self.run(lambda w3: w3.eth.block_number)

    def base_fee(self) -> int:
        def read(w3: Web3) -> int:
            block = w3.eth.get_block("latest")
            return int(block.get("baseFeePerGas") or w3.eth.gas_price)

        return self.run(read)

    def code_at(self, address: str) -> bytes:
        return bytes(self.run(lambda w3: w3.eth.get_code(Web3.to_checksum_address(address))))

    def balance(self, address: str) -> int:
        return self.run(lambda w3: w3.eth.get_balance(Web3.to_checksum_address(address)))

    def pending_nonce(self, address: str) -> int:
        return self.run(
            lambda w3: w3.eth.get_transaction_count(Web3.to_checksum_address(address), "pending")
        )

    def call(self, tx: dict[str, Any]) -> bytes:
        """eth_call — raises ContractLogicError when the call would revert."""
        return self.run(lambda w3: w3.eth.call(tx))

    def estimate_gas(self, tx: dict[str, Any]) -> int:
        return self.run(lambda w3: w3.eth.estimate_gas(tx))

    def contract(self, address: str, abi: list[dict[str, Any]] | None = None):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=abi if abi is not None else self.abi
        )

    def encode_call(
        self, address: str, name: str, args: list[Any], abi: list[dict[str, Any]] | None = None
    ) -> str:
        """ABI-encode a function call into calldata.

        `abi` overrides the client's own — useful for standard interfaces like
        ERC20 that have nothing to do with the mint contract.
        """
        target = abi if abi is not None else self.abi
        entry = resolve_overload(target, name, args)
        fn = self.contract(address, target).get_function_by_signature(signature_of(entry))
        return fn(*args)._encode_transaction_data()

    def decode_result(
        self, name: str, args: list[Any], data: bytes,
        abi: list[dict[str, Any]] | None = None,
    ) -> Any:
        entry = resolve_overload(abi if abi is not None else self.abi, name, args)
        decoded = self.w3.codec.decode(
            [o["type"] for o in entry.get("outputs", [])], bytes(data)
        )
        return decoded[0] if len(decoded) == 1 else decoded

    # -- writes --------------------------------------------------------------
    def send_raw(self, raw: bytes) -> str:
        # hexbytes drops the 0x prefix from .hex(); to_hex keeps hashes canonical.
        return self.run(lambda w3: Web3.to_hex(w3.eth.send_raw_transaction(raw)))

    def broadcast_all(self, raw: bytes) -> tuple[str | None, list[str]]:
        """Push the same signed bytes to every endpoint at once.

        Propagation, not duplication: the transaction has one hash, so extra
        copies are dropped by any node that already has it. Returns the hash
        from the first endpoint that took it, plus a note per failure.
        """
        tx_hash: str | None = None
        failures: list[str] = []
        for url in self._urls:
            try:
                accepted = Web3.to_hex(self._client_for(url).eth.send_raw_transaction(raw))
                tx_hash = tx_hash or accepted
            except Exception as exc:  # noqa: BLE001 - one endpoint refusing is not fatal
                failures.append(f"{url}: {str(exc)[:120]}")
        if tx_hash is None:
            raise ChainError("no endpoint accepted the transaction — " + " | ".join(failures))
        return tx_hash, failures

    def wait_for_receipt(self, tx_hash: str, timeout: float = 120.0) -> dict[str, Any]:
        return self.run(
            lambda w3: dict(w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout))
        )
