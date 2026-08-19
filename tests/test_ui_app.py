"""Rendering tests for the Streamlit control panel.

Each test copies mintbot_ui.py into a temp directory so the app's workspace
points there instead of at the repo.
"""

import json
import shutil
from pathlib import Path

import pytest

from mintbot.settings import ConfigDraft, write_config
from mintbot.ui_state import Workspace, add_wallet_from_key
from tests.conftest import KEY_A

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).parent.parent / "mintbot_ui.py"
PASSWORD = "correct-horse-battery"
CONTRACT = "0x" + "ab" * 20


def submit(at: AppTest, label: str):
    """Find a form submit button by its label."""
    return next(b for b in at.button if b.label == label)


def app_in(directory: Path) -> AppTest:
    shutil.copy(APP, directory / "mintbot_ui.py")
    return AppTest.from_file(str(directory / "mintbot_ui.py"), default_timeout=60)


def configured(directory: Path) -> Workspace:
    """A workspace with a wallet, a saved config and an ABI on disk."""
    workspace = Workspace(directory)
    add_wallet_from_key(workspace, "main", KEY_A, PASSWORD)
    write_config(workspace.config, ConfigDraft(contract_address=CONTRACT))
    workspace.abi.parent.mkdir(parents=True, exist_ok=True)
    workspace.abi.write_text(
        json.dumps(
            [
                {"type": "function", "name": "mint", "stateMutability": "payable",
                 "inputs": [{"name": "quantity", "type": "uint256"}], "outputs": []},
                {"type": "function", "name": "mintActive", "stateMutability": "view",
                 "inputs": [], "outputs": [{"name": "", "type": "bool"}]},
            ]
        )
    )
    return workspace


def test_an_empty_workspace_renders_and_lists_what_is_missing(tmp_path):
    at = app_in(tmp_path).run()
    assert at.exception == []
    assert len(at.tabs) == 4
    (warning,) = at.sidebar.warning
    assert "add at least one wallet" in warning.value
    assert "set the mint contract address" in warning.value


def test_a_configured_workspace_reports_ready(tmp_path):
    configured(tmp_path)
    at = app_in(tmp_path).run()
    assert at.exception == []
    assert any("Ready to arm" in s.value for s in at.sidebar.success)
    assert at.sidebar.metric[0].value == "1"


def test_the_saved_contract_and_abi_drive_the_drop_tab(tmp_path):
    configured(tmp_path)
    at = app_in(tmp_path).run()
    assert at.exception == []
    assert any(CONTRACT in i.value for i in at.text_input)
    # The mint entrypoint from the ABI is offered as a choice.
    assert any(
        any("mint(uint256)" in option for option in radio.options) for radio in at.radio
    )


def test_key_entry_is_blocked_on_hosted_infrastructure(tmp_path, monkeypatch):
    monkeypatch.delenv("MINTBOT_ALLOW_KEYS", raising=False)
    monkeypatch.setenv("SPACE_ID", "someone-elses-machine")
    at = app_in(tmp_path).run()
    assert at.exception == []
    assert any("Hugging Face Spaces" in e.value for e in at.error)
    assert not any("Private key" in i.label for i in at.text_input)


def test_key_entry_is_offered_on_a_local_machine(tmp_path, monkeypatch):
    for var in ("SPACE_ID", "DYNO", "K_SERVICE", "RENDER", "RAILWAY_ENVIRONMENT",
                "CODESPACE_NAME", "STREAMLIT_SHARING_MODE"):
        monkeypatch.delenv(var, raising=False)
    at = app_in(tmp_path).run()
    assert at.exception == []
    assert any("Private key" in i.label for i in at.text_input)


def test_a_wallet_can_be_added_through_the_form(tmp_path, monkeypatch):
    from mintbot.settings import read_wallets

    for var in ("SPACE_ID", "DYNO", "K_SERVICE", "RENDER", "RAILWAY_ENVIRONMENT",
                "CODESPACE_NAME", "STREAMLIT_SHARING_MODE"):
        monkeypatch.delenv(var, raising=False)

    at = app_in(tmp_path).run()
    at.text_input(key="add_label").set_value("main")
    at.text_input(key="add_key").set_value(KEY_A)
    at.text_input(key="add_pw").set_value(PASSWORD)
    at.text_input(key="add_pw2").set_value(PASSWORD)
    submit(at, "Add wallet").click().run()

    assert at.exception == []
    (entry,) = read_wallets(tmp_path / "wallets.toml")
    assert entry.label == "main"
    assert entry.source == "keystore"
    assert Path(entry.keystore).exists()


def test_mismatched_passwords_add_nothing(tmp_path, monkeypatch):
    from mintbot.settings import read_wallets

    for var in ("SPACE_ID", "DYNO", "K_SERVICE", "RENDER", "RAILWAY_ENVIRONMENT",
                "CODESPACE_NAME", "STREAMLIT_SHARING_MODE"):
        monkeypatch.delenv(var, raising=False)

    at = app_in(tmp_path).run()
    at.text_input(key="add_label").set_value("main")
    at.text_input(key="add_key").set_value(KEY_A)
    at.text_input(key="add_pw").set_value(PASSWORD)
    at.text_input(key="add_pw2").set_value("something-else")
    submit(at, "Add wallet").click().run()

    assert any("do not match" in e.value for e in at.error)
    assert read_wallets(tmp_path / "wallets.toml") == []


def test_preflight_runs_from_the_ui_against_a_live_node(tmp_path, monkeypatch):
    """The preflight tab unlocks the keystore and reports the same checks as the CLI."""
    from tests.test_integration import CHAIN_ID, StubState, make_handler
    import threading
    from http.server import ThreadingHTTPServer

    for var in ("SPACE_ID", "DYNO", "K_SERVICE", "RENDER", "RAILWAY_ENVIRONMENT",
                "CODESPACE_NAME", "STREAMLIT_SHARING_MODE"):
        monkeypatch.delenv(var, raising=False)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(StubState()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        workspace = configured(tmp_path)
        write_config(
            workspace.config,
            ConfigDraft(contract_address=CONTRACT, rpc_url=f"http://{host}:{port}",
                        chain_id=CHAIN_ID),
        )

        at = app_in(tmp_path).run()
        at.text_input(key="preflight_password").set_value(PASSWORD)
        next(b for b in at.button if b.label == "Run preflight").click().run()
    finally:
        server.shutdown()
        server.server_close()

    assert at.exception == []
    body = " ".join(m.value for m in at.markdown) + " ".join(s.value for s in at.success)
    assert "chain id 4663 confirmed" in body
    assert "mint entrypoint resolves to mint(uint256)" in body
    assert "Preflight clean." in body
    assert not at.error
