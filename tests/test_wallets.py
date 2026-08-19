import json

import pytest
from eth_account import Account

from mintbot.wallets import WalletError, load_wallets
from tests.conftest import KEY_A, KEY_B

ADDR_A = Account.from_key(KEY_A).address


def write(tmp_path, body: str):
    path = tmp_path / "wallets.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_a_key_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A)
    path = write(tmp_path, '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\nquantity = 2\n')
    (wallet,) = load_wallets(path)
    assert wallet.address == ADDR_A
    assert wallet.quantity == 2


def test_accepts_a_key_without_the_0x_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A[2:])
    path = write(tmp_path, '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\n')
    (wallet,) = load_wallets(path)
    assert wallet.address == ADDR_A


def test_missing_env_var_names_the_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("MB_ABSENT", raising=False)
    path = write(tmp_path, '[[wallet]]\nlabel = "a"\nkey_env = "MB_ABSENT"\n')
    with pytest.raises(WalletError, match="MB_ABSENT is not set"):
        load_wallets(path)


def test_rejects_a_truncated_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", "0x1234")
    path = write(tmp_path, '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\n')
    with pytest.raises(WalletError, match="must be 32 bytes"):
        load_wallets(path)


def test_refuses_a_key_that_does_not_match_the_declared_address(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A)
    path = write(
        tmp_path,
        '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\n'
        'address = "0x0000000000000000000000000000000000000001"\n',
    )
    with pytest.raises(WalletError, match="mismatched key"):
        load_wallets(path)


def test_rejects_two_entries_for_the_same_address(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A)
    monkeypatch.setenv("MB_KEY_DUP", KEY_A)
    path = write(
        tmp_path,
        '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\n\n'
        '[[wallet]]\nlabel = "dup"\nkey_env = "MB_KEY_DUP"\n',
    )
    with pytest.raises(WalletError, match="same address"):
        load_wallets(path)


def test_disabled_entries_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A)
    monkeypatch.setenv("MB_KEY_B", KEY_B)
    path = write(
        tmp_path,
        '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\n\n'
        '[[wallet]]\nlabel = "b"\nkey_env = "MB_KEY_B"\nenabled = false\n',
    )
    assert [w.label for w in load_wallets(path)] == ["a"]


def test_quantity_falls_back_to_the_config_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A)
    path = write(tmp_path, '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\n')
    (wallet,) = load_wallets(path, default_quantity=3)
    assert wallet.quantity == 3


def test_quantity_above_the_cap_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A)
    path = write(tmp_path, '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\nquantity = 9\n')
    with pytest.raises(WalletError, match="exceeds"):
        load_wallets(path, max_per_wallet=2)


def test_requires_exactly_one_key_source(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A)
    path = write(
        tmp_path,
        '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\nkeystore = "/tmp/x.json"\n',
    )
    with pytest.raises(WalletError, match="only one of"):
        load_wallets(path)


def test_loads_from_an_encrypted_keystore(tmp_path, monkeypatch):
    keystore = tmp_path / "key.json"
    keystore.write_text(json.dumps(Account.encrypt(KEY_A, "hunter2")), encoding="utf-8")
    monkeypatch.setenv("MB_PW", "hunter2")
    path = write(
        tmp_path,
        f'[[wallet]]\nlabel = "ks"\nkeystore = "{keystore}"\npassword_env = "MB_PW"\n',
    )
    (wallet,) = load_wallets(path)
    assert wallet.address == ADDR_A


def test_wrong_keystore_password_is_reported_clearly(tmp_path, monkeypatch):
    keystore = tmp_path / "key.json"
    keystore.write_text(json.dumps(Account.encrypt(KEY_A, "hunter2")), encoding="utf-8")
    monkeypatch.setenv("MB_PW", "wrong")
    path = write(
        tmp_path,
        f'[[wallet]]\nlabel = "ks"\nkeystore = "{keystore}"\npassword_env = "MB_PW"\n',
    )
    with pytest.raises(WalletError, match="could not decrypt"):
        load_wallets(path)


def test_the_private_key_never_appears_in_a_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_KEY_A", KEY_A)
    path = write(tmp_path, '[[wallet]]\nlabel = "a"\nkey_env = "MB_KEY_A"\n')
    (wallet,) = load_wallets(path)
    assert KEY_A not in repr(wallet) and KEY_A not in str(wallet)
    assert wallet.key == KEY_A
