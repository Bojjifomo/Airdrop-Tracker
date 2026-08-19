"""Pre-drop eligibility checks across every wallet.

Finding out at go-time that half your wallets were never on the allowlist is
the expensive way to learn it. This asks the contract in advance, three
different ways, and reports what each one said.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError

from .chain import ChainClient, ChainError, signature_of
from .runner import revert_reason

# View functions that take one address and answer "is this wallet allowed".
ALLOWLIST_WORDS = ("allowlist", "allowlisted", "whitelist", "whitelisted", "eligible", "claimed")
# View functions that count what an address has already taken.
MINTED_WORDS = ("numberminted", "minted", "mintcount", "mintedcount", "balanceof", "claimed")
ANSWER_TYPES = ("bool", "uint256", "uint8", "uint16")


@dataclass
class WalletVerdict:
    """What every check said about one wallet."""

    label: str
    address: str
    allowlisted: bool | None = None       # None when the contract exposes no flag
    allowlist_source: str = ""
    already_minted: int | None = None
    simulation_ok: bool = False
    simulation_detail: str = ""
    proof_matches: str = ""               # which leaf encoding matched, if any
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.simulation_ok:
            return "can mint now"
        if self.allowlisted is False:
            return "not on the allowlist"
        if self.allowlisted is True:
            return "on the allowlist, phase closed"
        return "unknown — phase closed"

    @property
    def ok(self) -> bool:
        return self.simulation_ok or self.allowlisted is True


# --------------------------------------------------------------------------- #
# finding the right view functions
# --------------------------------------------------------------------------- #
def _address_getters(abi: list[dict[str, Any]], words: tuple[str, ...]) -> list[dict[str, Any]]:
    matches = []
    for entry in abi:
        if entry.get("type") != "function" or entry.get("stateMutability") not in ("view", "pure"):
            continue
        inputs = entry.get("inputs", [])
        outputs = entry.get("outputs", [])
        if len(inputs) != 1 or inputs[0]["type"] != "address":
            continue
        if len(outputs) != 1 or outputs[0]["type"] not in ANSWER_TYPES:
            continue
        if any(word in entry["name"].lower() for word in words):
            matches.append(entry)
    return matches


def find_allowlist_getter(abi: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The view most likely to answer 'is this address allowed to mint'."""
    candidates = _address_getters(abi, ALLOWLIST_WORDS)
    # A bool answer is a far clearer signal than a count.
    candidates.sort(key=lambda e: (e["outputs"][0]["type"] != "bool", e["name"]))
    return candidates[0] if candidates else None


def find_minted_getter(abi: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The view most likely to answer 'how many has this address taken already'."""
    candidates = _address_getters(abi, MINTED_WORDS)
    candidates.sort(
        key=lambda e: (e["outputs"][0]["type"] == "bool", "balanceof" in e["name"].lower(), e["name"])
    )
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------- #
# merkle proofs
# --------------------------------------------------------------------------- #
def leaf_variants(address: str) -> dict[str, bytes]:
    """The two leaf encodings projects actually use, so we can report which fits.

    OpenZeppelin's older guidance hashes the packed address once; the current
    generator hashes it twice to make second-preimage attacks harder. A proof
    only verifies against the one the project used.
    """
    packed = HexBytes(Web3.to_checksum_address(address))
    single = keccak(packed)
    return {"keccak(address)": single, "keccak(keccak(address))": keccak(single)}


def verify_proof(leaf: bytes, proof: list[str | bytes], root: bytes) -> bool:
    """Standard sorted-pair merkle verification, as OpenZeppelin implements it."""
    computed = bytes(leaf)
    for step in proof:
        sibling = bytes(HexBytes(step))
        computed = keccak(
            computed + sibling if computed <= sibling else sibling + computed
        )
    return computed == bytes(root)


def match_proof(address: str, proof: list[str | bytes], root: bytes) -> str:
    """Return the leaf encoding whose proof verifies, or an empty string."""
    if not proof or not root:
        return ""
    for name, leaf in leaf_variants(address).items():
        if verify_proof(leaf, proof, root):
            return name
    return ""


# --------------------------------------------------------------------------- #
# the checker
# --------------------------------------------------------------------------- #
class EligibilityChecker:
    """Reads the contract to say, per wallet, whether it can mint."""

    def __init__(self, client: ChainClient, contract: str, runner=None):
        self.client = client
        self.contract = Web3.to_checksum_address(contract)
        self.runner = runner
        self.allowlist_getter = find_allowlist_getter(client.abi)
        self.minted_getter = find_minted_getter(client.abi)

    def _read(self, entry: dict[str, Any], address: str) -> Any:
        args = [Web3.to_checksum_address(address)]
        calldata = self.client.encode_call(self.contract, entry["name"], args)
        raw = self.client.call({"to": self.contract, "data": calldata})
        return self.client.decode_result(entry["name"], args, raw)

    def merkle_root(self) -> bytes | None:
        """Read merkleRoot() if the contract has one."""
        for name in ("merkleRoot", "root", "allowlistRoot", "whitelistRoot"):
            entries = [
                e for e in self.client.abi
                if e.get("type") == "function" and e.get("name") == name and not e.get("inputs")
            ]
            if not entries:
                continue
            try:
                raw = self.client.call(
                    {"to": self.contract, "data": self.client.encode_call(self.contract, name, [])}
                )
                return bytes(self.client.decode_result(name, [], raw))
            except (ChainError, ContractLogicError, ValueError):
                continue
        return None

    def check(self, wallets: list) -> list[WalletVerdict]:
        """Run every available check against every wallet."""
        root = self.merkle_root()
        verdicts = []

        for wallet in wallets:
            verdict = WalletVerdict(label=wallet.label, address=wallet.address)

            if self.allowlist_getter:
                verdict.allowlist_source = signature_of(self.allowlist_getter)
                try:
                    answer = self._read(self.allowlist_getter, wallet.address)
                    verdict.allowlisted = bool(answer)
                except (ChainError, ContractLogicError) as exc:
                    verdict.notes.append(f"allowlist read failed: {revert_reason(exc)}")
            else:
                verdict.notes.append("contract exposes no allowlist flag")

            if self.minted_getter:
                try:
                    verdict.already_minted = int(self._read(self.minted_getter, wallet.address))
                except (ChainError, ContractLogicError, TypeError) as exc:
                    verdict.notes.append(f"mint count read failed: {revert_reason(exc)}")

            if root and getattr(wallet, "proof", ()):
                verdict.proof_matches = match_proof(wallet.address, list(wallet.proof), root)
                if not verdict.proof_matches:
                    verdict.notes.append(
                        "the proof in wallets.toml does not verify against the contract's root"
                    )
            elif root and not getattr(wallet, "proof", ()):
                verdict.notes.append("contract uses a merkle allowlist but no proof is configured")

            if self.runner is not None:
                try:
                    plan = self.runner.build_plan(wallet)
                    ok, detail = self.runner.is_live(plan)
                    verdict.simulation_ok = ok
                    verdict.simulation_detail = detail
                except (ChainError, ContractLogicError) as exc:
                    verdict.simulation_detail = revert_reason(exc)

            verdicts.append(verdict)
        return verdicts
