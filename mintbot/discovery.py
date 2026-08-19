"""Contract introspection.

`mintbot discover` pulls the verified ABI from a Blockscout/Etherscan-style
explorer and ranks the functions that look like the mint entrypoint and the
phase flag, so the config can be filled in without reading Solidity by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .chain import signature_of

MINT_WORDS = ("mint", "claim", "purchase", "buy")
# Entrypoints reserved for the team, not for the public drop.
PRIVILEGED_WORDS = (
    "owner", "admin", "dev", "team", "reserve", "airdrop", "gift",
    "promo", "treasury", "burn", "set", "withdraw", "batch",
)
PHASE_WORDS = (
    "sale", "phase", "active", "live", "state", "status", "paused",
    "started", "open", "enabled", "stage", "round",
)
PHASE_RETURN_TYPES = ("bool", "uint8", "uint256", "uint16")


@dataclass(frozen=True)
class Candidate:
    entry: dict[str, Any]
    score: int
    reason: str

    @property
    def signature(self) -> str:
        return signature_of(self.entry)

    @property
    def name(self) -> str:
        return self.entry["name"]

    @property
    def input_types(self) -> list[str]:
        return [i["type"] for i in self.entry.get("inputs", [])]


class DiscoveryError(RuntimeError):
    """Raised when an ABI cannot be retrieved from the explorer."""


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def _extract_abi(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("abi", "result"):
            if key in payload:
                nested = _extract_abi(payload[key])
                if nested is not None:
                    return nested
    return None


def fetch_abi(explorer_api: str, address: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Fetch a verified ABI, trying the v2 and the Etherscan-compatible routes."""
    base = explorer_api.rstrip("/")
    root = base[: -len("/api")] if base.endswith("/api") else base
    attempts = [
        (f"{root}/api/v2/smart-contracts/{address}", None),
        (f"{base}", {"module": "contract", "action": "getabi", "address": address}),
    ]

    failures: list[str] = []
    for url, params in attempts:
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            abi = _extract_abi(response.json())
            if abi:
                return abi
            failures.append(f"{url}: no ABI in response (contract may not be verified)")
        except requests.RequestException as exc:
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
        except json.JSONDecodeError as exc:
            failures.append(f"{url}: response was not JSON ({exc})")

    raise DiscoveryError(
        "could not fetch a verified ABI — " + " | ".join(failures) + ". "
        "If the contract is unverified, save the ABI by hand and set [contract].abi_file."
    )


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #
def rank_mint_functions(abi: list[dict[str, Any]]) -> list[Candidate]:
    """Score every function that plausibly opens a public mint."""
    candidates: list[Candidate] = []
    for entry in abi:
        if entry.get("type") != "function":
            continue
        name = entry.get("name", "")
        lowered = name.lower()
        if not any(word in lowered for word in MINT_WORDS):
            continue
        if entry.get("stateMutability") in ("view", "pure"):
            continue

        score, notes = 0, []
        if entry.get("stateMutability") == "payable":
            score += 30
            notes.append("payable")

        types = [i["type"] for i in entry.get("inputs", [])]
        if types in ([], ["uint256"]):
            score += 40
            notes.append("public-style signature")
        elif types == ["uint256", "bytes32[]"]:
            score += 35
            notes.append("allowlist signature (quantity + merkle proof)")
        elif types and types[0] == "address":
            score += 5
            notes.append("takes a recipient address")
        else:
            score -= 10
            notes.append("unusual signature")

        privileged = [word for word in PRIVILEGED_WORDS if word in lowered]
        if privileged:
            score -= 45
            notes.append(f"looks team-only ({', '.join(privileged)})")

        if lowered in ("mint", "publicmint", "mintpublic"):
            score += 20
            notes.append("canonical name")

        candidates.append(Candidate(entry, score, "; ".join(notes)))

    return sorted(candidates, key=lambda c: (-c.score, c.name))


def rank_phase_getters(abi: list[dict[str, Any]]) -> list[Candidate]:
    """Score view functions that plausibly report whether the mint is open."""
    candidates: list[Candidate] = []
    for entry in abi:
        if entry.get("type") != "function":
            continue
        if entry.get("stateMutability") not in ("view", "pure"):
            continue
        if entry.get("inputs"):
            continue
        outputs = entry.get("outputs", [])
        if len(outputs) != 1 or outputs[0]["type"] not in PHASE_RETURN_TYPES:
            continue

        lowered = entry["name"].lower()
        matches = [word for word in PHASE_WORDS if word in lowered]
        if not matches:
            continue

        score = 20 * len(matches)
        notes = [f"matches {', '.join(matches)}"]
        if outputs[0]["type"] == "bool":
            score += 25
            notes.append("returns bool")
        if "paused" in lowered:
            notes.append("inverted: expect false")
        candidates.append(Candidate(entry, score, "; ".join(notes)))

    return sorted(candidates, key=lambda c: (-c.score, c.name))


def template_args_for(entry: dict[str, Any]) -> tuple[str, ...]:
    """Map a mint signature onto the bot's per-wallet placeholders."""
    placeholders = {"uint256": "{quantity}", "address": "{address}", "bytes32[]": "{proof}"}
    return tuple(
        placeholders.get(i["type"], f"<{i['type']} {i.get('name') or 'arg'}>")
        for i in entry.get("inputs", [])
    )


def expected_open_value(entry: dict[str, Any]) -> Any:
    """The value a phase getter takes when the mint is open."""
    if entry["outputs"][0]["type"] != "bool":
        return 1
    return "paused" not in entry["name"].lower()


def suggest_config(address: str, mint: Candidate | None, getter: Candidate | None) -> str:
    """Render a config snippet for the top-ranked candidates."""
    lines = ["[contract]", f'address = "{address}"', 'abi_file = "abi/mint.json"', "", "[mint]"]
    if mint is None:
        lines += ['function = "mint"  # nothing matched — check the ABI by hand', "args = []"]
    else:
        args = [f'"{a}"' for a in template_args_for(mint.entry)]
        lines += [f'function = "{mint.name}"', f"args = [{', '.join(args)}]"]
    lines += ['price = "0 eth"  # <- set the real mint price', "quantity = 1", ""]

    if getter is None:
        lines += ["[watch]", 'mode = "simulate"']
    else:
        value = expected_open_value(getter.entry)
        expect = str(value).lower() if isinstance(value, bool) else str(value)
        lines += [
            "[watch]",
            'mode = "both"',
            "",
            "[watch.getter]",
            f'function = "{getter.name}"',
            f"expect = {expect}",
        ]
    return "\n".join(lines)


def save_abi(abi: list[dict[str, Any]], path: str | Path) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(abi, indent=2) + "\n", encoding="utf-8")
    return target
