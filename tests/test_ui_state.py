"""Tests for the logic behind the mint bot UI."""

import json
import os
from pathlib import Path

import pytest
from eth_account import Account

from mintbot.keystore import create_keystore, list_keystores, verify_password
from mintbot.settings import SHARED_PASSWORD_ENV, ConfigDraft, WalletEntry, read_wallets, write_config
from mintbot.ui_state import (
    Workspace,
    add_wallet_from_env,
    add_wallet_from_key,
    build_run_command,
    check_keystore_password,
    hosted_environment,
    keystore_password,
    parse_proof,
    read_events,
    readiness,
    remove_wallet,
    reset_events,
    tail,
    update_wallet,
)
from mintbot.wallets import WalletError, load_wallets
from tests.conftest import KEY_A, KEY_B

PASSWORD = "correct-horse-battery"


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return Workspace(tmp_path)


# --------------------------------------------------------------------------- #
# adding wallets
# --------------------------------------------------------------------------- #
def test_a_pasted_key_is_encrypted_and_never_stored_in_the_clear(workspace):
    entry = add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)

    assert entry.address == Account.from_key(KEY_A).address
    assert entry.source == "keystore"
    assert verify_password(entry.keystore, PASSWORD)

    on_disk = workspace.wallets.read_text() + Path(entry.keystore).read_text()
    assert KEY_A not in on_disk and KEY_A[2:] not in on_disk


def test_the_keystore_file_is_owner_only(workspace):
    entry = add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    assert oct(Path(entry.keystore).stat().st_mode & 0o777) == "0o600"


def test_a_wallet_written_by_the_ui_loads_through_the_normal_path(workspace):
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD, quantity=2)
    with keystore_password(PASSWORD):
        (wallet,) = load_wallets(workspace.wallets, allow_prompt=False)
    assert wallet.address == Account.from_key(KEY_A).address
    assert wallet.quantity == 2
    assert wallet.key == KEY_A


def test_a_short_password_is_refused_before_anything_is_written(workspace):
    with pytest.raises(WalletError, match="at least 8 characters"):
        add_wallet_from_key(workspace, "main", KEY_A, "short")
    assert list_keystores(workspace.keys) == []
    assert read_wallets(workspace.wallets) == []


def test_a_duplicate_label_is_refused(workspace):
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    with pytest.raises(WalletError, match="already exists"):
        add_wallet_from_key(workspace, "main", KEY_B, PASSWORD)


def test_the_same_address_under_a_new_label_is_refused_and_leaves_no_keystore(workspace):
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    with pytest.raises(WalletError, match="already configured"):
        add_wallet_from_key(workspace, "duplicate", KEY_A, PASSWORD)
    assert [k.label for k in list_keystores(workspace.keys)] == ["main"]


def test_an_env_backed_wallet_needs_no_keystore(workspace):
    entry = add_wallet_from_env(workspace, "alt", "MINT_KEY_ALT", quantity=3)
    assert entry.source == "env"
    assert not workspace.keys.exists()
    assert read_wallets(workspace.wallets)[0].quantity == 3


def test_wallets_survive_a_write_read_round_trip(workspace):
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD, proof=("0x" + "cd" * 32,))
    add_wallet_from_env(workspace, "alt", "MINT_KEY_ALT")
    entries = read_wallets(workspace.wallets)
    assert [e.label for e in entries] == ["main", "alt"]
    assert entries[0].proof == ("0x" + "cd" * 32,)
    assert entries[0].password_env == SHARED_PASSWORD_ENV


# --------------------------------------------------------------------------- #
# editing wallets
# --------------------------------------------------------------------------- #
def test_quantity_and_armed_state_can_be_edited(workspace):
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    update_wallet(workspace, "main", quantity=5, enabled=False)
    entry = read_wallets(workspace.wallets)[0]
    assert entry.quantity == 5 and entry.enabled is False


def test_removing_a_wallet_deletes_its_keystore(workspace):
    entry = add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    remove_wallet(workspace, "main")
    assert read_wallets(workspace.wallets) == []
    assert not Path(entry.keystore).exists()


def test_a_wallet_can_be_removed_while_keeping_its_keystore(workspace):
    entry = add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    remove_wallet(workspace, "main", drop_keystore=False)
    assert Path(entry.keystore).exists()


def test_a_wrong_password_names_the_wallets_it_cannot_open(workspace):
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    add_wallet_from_key(workspace, "alt", KEY_B, PASSWORD)
    entries = read_wallets(workspace.wallets)
    assert check_keystore_password(entries, PASSWORD) == []
    assert set(check_keystore_password(entries, "wrong-password")) == {"main", "alt"}


# --------------------------------------------------------------------------- #
# password handling
# --------------------------------------------------------------------------- #
def test_the_password_is_removed_from_the_environment_afterwards(monkeypatch):
    monkeypatch.delenv(SHARED_PASSWORD_ENV, raising=False)
    with keystore_password("temporary"):
        assert os.environ[SHARED_PASSWORD_ENV] == "temporary"
    assert SHARED_PASSWORD_ENV not in os.environ


def test_an_existing_password_variable_is_restored(monkeypatch):
    monkeypatch.setenv(SHARED_PASSWORD_ENV, "original")
    with keystore_password("temporary"):
        assert os.environ[SHARED_PASSWORD_ENV] == "temporary"
    assert os.environ[SHARED_PASSWORD_ENV] == "original"


def test_the_password_is_restored_even_when_the_body_raises(monkeypatch):
    monkeypatch.delenv(SHARED_PASSWORD_ENV, raising=False)
    with pytest.raises(RuntimeError):
        with keystore_password("temporary"):
            raise RuntimeError("boom")
    assert SHARED_PASSWORD_ENV not in os.environ


# --------------------------------------------------------------------------- #
# hosted detection
# --------------------------------------------------------------------------- #
def test_a_local_machine_allows_key_entry(monkeypatch):
    for var, _ in [("STREAMLIT_SHARING_MODE", ""), ("SPACE_ID", ""), ("DYNO", ""),
                   ("K_SERVICE", ""), ("RENDER", ""), ("RAILWAY_ENVIRONMENT", ""),
                   ("CODESPACE_NAME", ""), ("MINTBOT_ALLOW_KEYS", "")]:
        monkeypatch.delenv(var, raising=False)
    assert hosted_environment() is None


@pytest.mark.parametrize(
    ("variable", "expected"),
    [("SPACE_ID", "Hugging Face Spaces"), ("DYNO", "Heroku"), ("K_SERVICE", "Google Cloud Run")],
)
def test_known_hosts_block_key_entry(monkeypatch, variable, expected):
    monkeypatch.delenv("MINTBOT_ALLOW_KEYS", raising=False)
    monkeypatch.setenv(variable, "1")
    assert hosted_environment() == expected


def test_the_override_re_enables_key_entry_on_a_self_hosted_box(monkeypatch):
    monkeypatch.setenv("SPACE_ID", "1")
    monkeypatch.setenv("MINTBOT_ALLOW_KEYS", "1")
    assert hosted_environment() is None


# --------------------------------------------------------------------------- #
# proofs, readiness, running
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ()),
        ("0xaa\n0xbb", ("0xaa", "0xbb")),
        ('  0xaa ,\n "0xbb" ', ("0xaa", "0xbb")),
        ('["0xaa", "0xbb"]', ("0xaa", "0xbb")),
    ],
)
def test_a_merkle_proof_can_be_pasted_in_any_of_the_usual_shapes(text, expected):
    assert parse_proof(text) == expected


def test_readiness_names_every_missing_step(workspace):
    state = readiness(workspace, ConfigDraft())
    assert state.ready is False
    assert state.missing == [
        "add at least one wallet",
        "set the mint contract address",
        "fetch the contract ABI",
    ]


def test_readiness_clears_once_setup_is_complete(workspace):
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    workspace.abi.parent.mkdir(parents=True, exist_ok=True)
    workspace.abi.write_text("[]")
    state = readiness(workspace, ConfigDraft(contract_address="0x" + "ab" * 20))
    assert state.ready is True and state.missing == []


def test_a_disarmed_wallet_does_not_count_towards_readiness(workspace):
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    update_wallet(workspace, "main", enabled=False)
    assert readiness(workspace, ConfigDraft()).has_wallets is False


def test_the_run_command_only_goes_live_when_asked(workspace):
    assert "--live" not in build_run_command(workspace, live=False)
    assert "--live" in build_run_command(workspace, live=True)
    assert build_run_command(workspace, live=True)[-2:] == ["--yes", "--live"]


def test_events_are_read_back_and_bad_lines_skipped(workspace):
    workspace.events.write_text(
        json.dumps({"event": "armed"}) + "\nnot json\n" + json.dumps({"event": "minted"}) + "\n"
    )
    assert [e["event"] for e in read_events(workspace.events)] == ["armed", "minted"]


def test_resetting_clears_the_previous_run(workspace):
    workspace.events.write_text("{}\n")
    workspace.run_log.write_text("old output\n")
    reset_events(workspace)
    assert read_events(workspace.events) == [] and tail(workspace.run_log) == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("true", True), ("True", True), ("false", False), ("FALSE", False),
        ("1", 1), (" 2 ", 2), ("0x3", 3), ("open", "open"),
    ],
)
def test_a_typed_phase_value_keeps_its_type(text, expected):
    from mintbot.ui_state import coerce_expect

    result = coerce_expect(text)
    assert result == expected and type(result) is type(expected)
