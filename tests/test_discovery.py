import json

import pytest

from mintbot.discovery import (
    _extract_abi,
    rank_mint_functions,
    rank_phase_getters,
    save_abi,
    suggest_config,
)


def fn(name, inputs=(), mutability="payable", outputs=()):
    return {
        "type": "function",
        "name": name,
        "stateMutability": mutability,
        "inputs": [{"name": f"a{i}", "type": t} for i, t in enumerate(inputs)],
        "outputs": [{"name": "", "type": t} for t in outputs],
    }


def test_public_mint_outranks_the_team_entrypoints():
    abi = [
        fn("ownerMint", ["address", "uint256"], "nonpayable"),
        fn("mint", ["uint256"]),
        fn("devMintReserve", ["uint256"], "nonpayable"),
    ]
    ranked = rank_mint_functions(abi)
    assert ranked[0].name == "mint"
    assert ranked[0].score > ranked[-1].score


def test_allowlist_signature_is_recognised():
    (candidate,) = rank_mint_functions([fn("allowlistMint", ["uint256", "bytes32[]"])])
    assert "allowlist signature" in candidate.reason
    assert candidate.score > 0


def test_view_functions_are_never_mint_candidates():
    assert rank_mint_functions([fn("mintPrice", [], "view", ["uint256"])]) == []


def test_unrelated_functions_are_ignored():
    assert rank_mint_functions([fn("transferFrom", ["address", "address", "uint256"])]) == []


def test_boolean_phase_flags_outrank_numeric_ones():
    abi = [
        fn("saleState", [], "view", ["uint8"]),
        fn("mintActive", [], "view", ["bool"]),
        fn("totalSupply", [], "view", ["uint256"]),
    ]
    ranked = rank_phase_getters(abi)
    assert [c.name for c in ranked][0] == "mintActive"
    assert "totalSupply" not in [c.name for c in ranked]


def test_a_paused_flag_is_flagged_as_inverted():
    (candidate,) = rank_phase_getters([fn("paused", [], "view", ["bool"])])
    assert "inverted" in candidate.reason


def test_getters_taking_arguments_are_skipped():
    assert rank_phase_getters([fn("phaseActive", ["uint256"], "view", ["bool"])]) == []


@pytest.mark.parametrize(
    "payload",
    [
        [{"type": "function", "name": "mint"}],
        {"abi": [{"type": "function", "name": "mint"}]},
        {"status": "1", "result": '[{"type": "function", "name": "mint"}]'},
    ],
)
def test_abi_is_extracted_from_every_explorer_envelope(payload):
    assert _extract_abi(payload) == [{"type": "function", "name": "mint"}]


def test_extract_returns_none_when_there_is_no_abi():
    assert _extract_abi({"message": "Contract source code not verified"}) is None


def test_suggested_config_templates_the_mint_arguments():
    (mint,) = rank_mint_functions([fn("allowlistMint", ["uint256", "bytes32[]"])])
    (getter,) = rank_phase_getters([fn("mintActive", [], "view", ["bool"])])
    snippet = suggest_config("0x" + "ab" * 20, mint, getter)
    assert 'function = "allowlistMint"' in snippet
    assert 'args = ["{quantity}", "{proof}"]' in snippet
    assert 'mode = "both"' in snippet
    assert "expect = true" in snippet


def test_suggested_config_falls_back_to_simulation_without_a_getter():
    (mint,) = rank_mint_functions([fn("mint", ["uint256"])])
    snippet = suggest_config("0x" + "ab" * 20, mint, None)
    assert 'mode = "simulate"' in snippet
    assert "[watch.getter]" not in snippet


def test_saved_abi_round_trips(tmp_path):
    abi = [fn("mint", ["uint256"])]
    path = save_abi(abi, tmp_path / "nested" / "mint.json")
    assert json.loads(path.read_text()) == abi


# --------------------------------------------------------------------------- #
# the explorer HTTP path, against a local stub
# --------------------------------------------------------------------------- #
class _ExplorerStub:
    """Serves one of the two explorer response shapes on 127.0.0.1."""

    def __init__(self, *, v2_ok: bool):
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        abi = [fn("mint", ["uint256"])]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/api/v2/smart-contracts/"):
                    body = {"abi": abi} if v2_ok else {"message": "Not found"}
                else:
                    body = {"status": "1", "result": _json.dumps(abi)}
                encoded = _json.dumps(body).encode()
                self.send_response(200 if v2_ok or "module=contract" in self.path else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}/api"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("v2_ok", [True, False])
def test_fetch_abi_handles_both_explorer_routes(v2_ok):
    from mintbot.discovery import fetch_abi

    stub = _ExplorerStub(v2_ok=v2_ok)
    try:
        abi = fetch_abi(stub.base, "0x" + "ab" * 20)
    finally:
        stub.close()
    assert [e["name"] for e in abi] == ["mint"]


def test_fetch_abi_explains_an_unverified_contract():
    from mintbot.discovery import DiscoveryError, fetch_abi

    with pytest.raises(DiscoveryError, match="unverified"):
        fetch_abi("http://127.0.0.1:1/api", "0x" + "ab" * 20, timeout=0.5)
