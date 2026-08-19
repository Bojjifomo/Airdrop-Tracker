"""Pre-drop eligibility checks and merkle proof matching."""

import pytest
from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3

from mintbot.eligibility import (
    EligibilityChecker,
    find_allowlist_getter,
    find_minted_getter,
    leaf_variants,
    match_proof,
    verify_proof,
)
from mintbot.runner import MintRunner, Reporter
from tests.conftest import CONTRACT, FakeClient

ADDRESS = Web3.to_checksum_address("0x" + "ab" * 20)


def view(name, inputs=("address",), outputs=("bool",)):
    return {
        "type": "function", "name": name, "stateMutability": "view",
        "inputs": [{"name": "a", "type": t} for t in inputs],
        "outputs": [{"name": "", "type": t} for t in outputs],
    }


# --------------------------------------------------------------------------- #
# picking the right view functions
# --------------------------------------------------------------------------- #
def test_a_boolean_allowlist_flag_wins_over_a_counter():
    abi = [view("allowlistSpots", outputs=("uint256",)), view("isWhitelisted")]
    assert find_allowlist_getter(abi)["name"] == "isWhitelisted"


def test_functions_that_do_not_take_an_address_are_ignored():
    assert find_allowlist_getter([view("allowlistOpen", inputs=())]) is None


def test_an_unrelated_view_is_not_mistaken_for_an_allowlist():
    assert find_allowlist_getter([view("ownerOf", inputs=("uint256",), outputs=("address",))]) is None


def test_a_dedicated_counter_is_preferred_over_balance_of():
    abi = [view("balanceOf", outputs=("uint256",)), view("numberMinted", outputs=("uint256",))]
    assert find_minted_getter(abi)["name"] == "numberMinted"


def test_balance_of_is_used_when_nothing_better_exists():
    assert find_minted_getter([view("balanceOf", outputs=("uint256",))])["name"] == "balanceOf"


# --------------------------------------------------------------------------- #
# merkle proofs
# --------------------------------------------------------------------------- #
def test_both_leaf_encodings_are_offered():
    variants = leaf_variants(ADDRESS)
    assert set(variants) == {"keccak(address)", "keccak(keccak(address))"}
    assert variants["keccak(keccak(address))"] == keccak(variants["keccak(address)"])


def test_a_proof_verifies_against_the_root_it_was_built_for():
    leaf = leaf_variants(ADDRESS)["keccak(address)"]
    sibling = keccak(b"sibling")
    root = keccak(leaf + sibling if leaf <= sibling else sibling + leaf)
    assert verify_proof(leaf, [sibling], root) is True


def test_pairs_are_sorted_so_sibling_order_does_not_matter():
    leaf = keccak(b"\xff" * 32)          # deliberately large, to sort second
    sibling = keccak(b"\x00" * 32)
    root = keccak(sibling + leaf)
    assert verify_proof(leaf, [sibling], root) is True


def test_a_proof_for_a_different_root_does_not_verify():
    leaf = leaf_variants(ADDRESS)["keccak(address)"]
    assert verify_proof(leaf, [keccak(b"sibling")], keccak(b"someone elses root")) is False


def test_match_proof_names_the_encoding_the_project_used():
    leaf = leaf_variants(ADDRESS)["keccak(keccak(address))"]
    sibling = keccak(b"sibling")
    root = keccak(leaf + sibling if leaf <= sibling else sibling + leaf)
    assert match_proof(ADDRESS, [sibling], root) == "keccak(keccak(address))"


def test_match_proof_returns_nothing_when_neither_encoding_fits():
    assert match_proof(ADDRESS, [keccak(b"x")], keccak(b"unrelated")) == ""


def test_match_proof_is_quiet_without_a_proof_or_a_root():
    assert match_proof(ADDRESS, [], keccak(b"root")) == ""
    assert match_proof(ADDRESS, [keccak(b"x")], b"") == ""


# --------------------------------------------------------------------------- #
# the checker
# --------------------------------------------------------------------------- #
ALLOWLIST_ABI = [
    view("isAllowlisted"),
    view("numberMinted", outputs=("uint256",)),
    {"type": "function", "name": "mint", "stateMutability": "payable",
     "inputs": [{"name": "quantity", "type": "uint256"}], "outputs": []},
]


class AnswerClient(FakeClient):
    """Answers view calls from a table keyed by function selector prefix."""

    def __init__(self, *args, allowed=True, minted=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed = allowed
        self.minted = minted

    def call(self, tx):
        if tx.get("from") is not None:            # the mint simulation
            return super().call(tx)
        selector = tx["data"][:10]
        allow = self.encode_call(CONTRACT, "isAllowlisted", [ADDRESS])[:10]
        if selector == allow:
            return int(bool(self.allowed)).to_bytes(32, "big")
        return int(self.minted).to_bytes(32, "big")


def test_an_allowlisted_wallet_is_reported_as_such(config, wallets):
    client = AnswerClient(config.chain, ALLOWLIST_ABI, allowed=True, minted=0)
    (verdict,) = EligibilityChecker(client, CONTRACT).check(wallets[:1])

    assert verdict.allowlisted is True
    assert verdict.already_minted == 0
    assert verdict.allowlist_source == "isAllowlisted(address)"
    assert verdict.verdict == "on the allowlist, phase closed"
    assert verdict.ok is True


def test_a_wallet_that_is_not_on_the_list_is_flagged_before_the_drop(config, wallets):
    client = AnswerClient(config.chain, ALLOWLIST_ABI, allowed=False)
    (verdict,) = EligibilityChecker(client, CONTRACT).check(wallets[:1])

    assert verdict.allowlisted is False
    assert verdict.verdict == "not on the allowlist"
    assert verdict.ok is False


def test_a_wallet_that_already_minted_shows_its_count(config, wallets):
    client = AnswerClient(config.chain, ALLOWLIST_ABI, allowed=True, minted=3)
    (verdict,) = EligibilityChecker(client, CONTRACT).check(wallets[:1])
    assert verdict.already_minted == 3


def test_a_contract_without_an_allowlist_says_so_rather_than_guessing(config, abi, wallets):
    client = FakeClient(config.chain, abi)
    (verdict,) = EligibilityChecker(client, CONTRACT).check(wallets[:1])

    assert verdict.allowlisted is None
    assert "exposes no allowlist flag" in verdict.notes[0]
    assert verdict.verdict == "unknown — phase closed"


def test_a_live_simulation_overrides_every_other_signal(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=True)
    runner = MintRunner(config, wallets, client, Reporter(None))
    (verdict,) = EligibilityChecker(client, CONTRACT, runner).check(wallets[:1])

    assert verdict.simulation_ok is True
    assert verdict.verdict == "can mint now"
    assert verdict.ok is True


def test_a_closed_phase_reports_the_revert_reason(config, abi, wallets):
    client = FakeClient(config.chain, abi, live=False)
    runner = MintRunner(config, wallets, client, Reporter(None))
    (verdict,) = EligibilityChecker(client, CONTRACT, runner).check(wallets[:1])

    assert verdict.simulation_ok is False
    assert "mint not started" in verdict.simulation_detail


def test_every_wallet_gets_its_own_verdict(config, abi, wallets):
    client = FakeClient(config.chain, abi)
    verdicts = EligibilityChecker(client, CONTRACT).check(wallets)
    assert [v.label for v in verdicts] == ["a", "b"]
    assert [v.address for v in verdicts] == [w.address for w in wallets]
