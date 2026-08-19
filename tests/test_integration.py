"""End-to-end run against a local stub JSON-RPC server.

The unit tests stub out ChainClient; this one exercises the real web3 HTTP
transport, signing and eth_sendRawTransaction path. Everything stays on
127.0.0.1 — no external network is touched.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from eth_account import Account

from mintbot.chain import ChainClient, load_abi
from mintbot.cli import _preflight
from mintbot.config import parse_config
from mintbot.runner import MintRunner, Reporter
from mintbot.wallets import Wallet
from tests.conftest import KEY_A, base_config_dict

CHAIN_ID = 4663
TX_HASH = "0x" + "cd" * 32


class StubState:
    """Shared mutable state for the stub node."""

    def __init__(self, open_after_calls: int = 2):
        self.open_after_calls = open_after_calls
        self.eth_calls = 0
        self.raw_transactions: list[str] = []


def make_handler(state: StubState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

        def _dispatch(self, method: str, params: list):
            if method == "eth_chainId":
                return hex(CHAIN_ID)
            if method == "eth_blockNumber":
                return "0x1234"
            if method == "eth_getBlockByNumber":
                return {"number": "0x1234", "baseFeePerGas": hex(10**9), "gasLimit": "0x1c9c380"}
            if method == "eth_getCode":
                return "0x608060405260ff"
            if method == "eth_getBalance":
                return hex(10**18)
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_gasPrice":
                return hex(10**9)
            if method == "eth_call":
                state.eth_calls += 1
                if state.eth_calls <= state.open_after_calls:
                    raise RevertError("execution reverted: mint not started")
                return "0x"
            if method == "eth_estimateGas":
                raise RevertError("execution reverted: mint not started")
            if method == "eth_sendRawTransaction":
                state.raw_transactions.append(params[0])
                return TX_HASH
            if method == "eth_getTransactionReceipt":
                return {
                    "transactionHash": TX_HASH,
                    "status": "0x1",
                    "blockNumber": "0x1235",
                    "blockHash": "0x" + "11" * 32,
                    "gasUsed": "0x1d4c0",
                    "cumulativeGasUsed": "0x1d4c0",
                    "transactionIndex": "0x0",
                    "from": Account.from_key(KEY_A).address,
                    "to": None,
                    "contractAddress": None,
                    "logs": [],
                    "logsBloom": "0x" + "00" * 256,
                    "effectiveGasPrice": hex(10**9),
                    "type": "0x2",
                }
            raise RevertError(f"stub node does not implement {method}")

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                body = {"jsonrpc": "2.0", "id": payload["id"],
                        "result": self._dispatch(payload["method"], payload.get("params", []))}
            except RevertError as exc:
                body = {"jsonrpc": "2.0", "id": payload["id"],
                        "error": {"code": 3, "message": str(exc), "data": "0x"}}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


class RevertError(Exception):
    pass


@pytest.fixture
def stub_node():
    state = StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()


def build(url: str, **overrides):
    chain = {"rpc_url": url, **overrides.pop("chain", {})}
    config = parse_config(base_config_dict(chain=chain, **overrides))
    wallet = Wallet(label="a", address=Account.from_key(KEY_A).address, quantity=1, _key=KEY_A)
    return config, ChainClient(config.chain, load_abi(None)), [wallet]


def test_preflight_passes_against_a_live_node(stub_node):
    url, _ = stub_node
    config, client, wallets = build(url)
    problems, notes = _preflight(config, client, wallets)
    assert problems == []
    assert any("chain id 4663 confirmed" in n for n in notes)
    assert any("closed (" in n and "mint not started" in n for n in notes)


def test_a_chain_id_mismatch_is_caught_before_anything_is_signed(stub_node):
    url, _ = stub_node
    config, client, wallets = build(url, chain={"chain_id": 1})
    problems, _ = _preflight(config, client, wallets)
    assert any("RPC reports chain id 4663" in p for p in problems)


def test_the_bot_polls_until_the_stub_opens_then_broadcasts_a_valid_tx(stub_node):
    url, state = stub_node
    config, client, wallets = build(url)
    runner = MintRunner(config, wallets, client, Reporter(None))

    results = runner.run()

    assert results["a"].status == "minted"
    assert results["a"].tx_hash == TX_HASH
    assert state.eth_calls > state.open_after_calls          # it really waited

    (raw,) = state.raw_transactions
    assert Account.recover_transaction(raw) == wallets[0].address


def test_a_dead_endpoint_fails_over_to_the_working_one(stub_node):
    url, state = stub_node
    config, client, wallets = build(
        url, chain={"rpc_url": "http://127.0.0.1:1", "fallback_rpc_urls": [url]}
    )
    assert client.verify_chain_id() == CHAIN_ID
    assert client.endpoint == url
