"""Mint bot control panel.

    streamlit run mintbot_ui.py

Run this on your own machine. It handles private keys, so it deliberately
refuses to accept them when it detects it is running on hosted infrastructure.
This is a separate app from the airdrop tracker in app.py; neither touches the
other's state.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from mintbot.chain import ChainClient, ChainError, load_abi          # noqa: E402
from mintbot.cli import _preflight                                    # noqa: E402
from mintbot.config import ConfigError, load_config                   # noqa: E402
from mintbot.discovery import (                                       # noqa: E402
    DiscoveryError, expected_open_value, fetch_abi, rank_mint_functions,
    rank_phase_getters, save_abi, template_args_for,
)
from mintbot.settings import (                                        # noqa: E402
    ConfigDraft, read_draft, read_wallets, write_config,
)
from mintbot.ui_state import (                                        # noqa: E402
    Workspace, add_wallet_from_env, add_wallet_from_key, check_keystore_password,
    coerce_expect, hosted_environment, keystore_password, parse_proof, read_events, readiness,
    remove_wallet, reset_events, start_run, tail, update_wallet,
)
from mintbot.wallets import WalletError, load_wallets                 # noqa: E402

WS = Workspace(ROOT)
WATCH_HELP = {
    "simulate": "Poll the real mint call and fire when it stops reverting. Works on any contract.",
    "getter": "Watch a phase flag on the contract and fire when it flips.",
    "both": "Require the flag AND a clean simulation. Strictest; avoids a flag that flips early.",
}

st.set_page_config(page_title="Mint Bot", page_icon="🤖", layout="wide")
st.markdown(
    """<style>
.block-container {max-width: 1200px;}
.mono {font-family:monospace;font-size:12px;color:#666;}
.pill {font-family:monospace;font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid #bbb;}
</style>""",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# shared state
# --------------------------------------------------------------------------- #
def draft() -> ConfigDraft:
    if "draft" not in st.session_state:
        st.session_state.draft = read_draft(WS.config)
    return st.session_state.draft


def save_draft(new: ConfigDraft) -> bool:
    try:
        write_config(WS.config, new)
    except ConfigError as exc:
        st.error(str(exc))
        return False
    st.session_state.draft = new
    return True


def running() -> bool:
    process = st.session_state.get("process")
    return process is not None and process.poll() is None


HOSTED = hosted_environment()


# --------------------------------------------------------------------------- #
# sidebar
# --------------------------------------------------------------------------- #
def render_sidebar() -> None:
    st.sidebar.title("🤖 Mint Bot")
    state = readiness(WS, draft())
    entries = read_wallets(WS.wallets)

    st.sidebar.metric("Wallets armed", sum(1 for e in entries if e.enabled))
    address = draft().contract_address.strip()
    st.sidebar.caption(
        f"contract: `{address[:10]}…{address[-6:]}`" if state.has_contract else "contract: not set"
    )
    st.sidebar.caption(f"chain {draft().chain_id} · {draft().watch_mode} mode")

    if state.ready:
        st.sidebar.success("Ready to arm")
    else:
        st.sidebar.warning("Still needed:\n\n" + "\n".join(f"- {m}" for m in state.missing))

    if running():
        st.sidebar.info("Bot is running")

    if HOSTED:
        st.sidebar.error(
            f"Detected **{HOSTED}**. Private key entry is disabled — run this on your "
            "own machine instead."
        )
    st.sidebar.caption(f"<span class='mono'>workspace: {ROOT}</span>", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# tab 1 — wallets
# --------------------------------------------------------------------------- #
def render_wallets_tab() -> None:
    entries = read_wallets(WS.wallets)

    if not entries:
        st.info("No wallets yet. Add one below — the key is encrypted before it touches disk.")
    for entry in entries:
        with st.container(border=True):
            cols = st.columns([3, 4, 2, 2, 1])
            cols[0].markdown(
                f"**{entry.label}**  \n<span class='pill'>{entry.source}</span>",
                unsafe_allow_html=True,
            )
            cols[1].markdown(
                f"<span class='mono'>{entry.address or 'address unknown until the key loads'}</span>",
                unsafe_allow_html=True,
            )
            quantity = cols[2].number_input(
                "qty", 1, 100, entry.quantity, key=f"qty_{entry.label}", label_visibility="collapsed"
            )
            enabled = cols[3].toggle("armed", entry.enabled, key=f"on_{entry.label}")
            if quantity != entry.quantity or enabled != entry.enabled:
                update_wallet(WS, entry.label, quantity=int(quantity), enabled=enabled)
                st.rerun()
            if cols[4].button("🗑", key=f"rm_{entry.label}", help="Remove wallet and its keystore"):
                remove_wallet(WS, entry.label)
                st.rerun()
            if entry.proof:
                st.caption(f"merkle proof: {len(entry.proof)} element(s)")

    if entries and st.button("Check balances"):
        if not WS.config.exists():
            st.warning("Save a configuration on the next tab first — that is where the RPC lives.")
        else:
            with st.spinner("Reading balances…"):
                try:
                    client = ChainClient(load_config(WS.config).chain, [])
                except ConfigError as exc:
                    st.error(str(exc))
                    client = None
                for entry in entries if client else []:
                    if not entry.address:
                        st.caption(f"`{entry.label}` — address unknown until the key loads")
                        continue
                    try:
                        st.write(f"`{entry.label}` — {client.balance(entry.address) / 1e18:.6f} ETH")
                    except ChainError as exc:
                        st.warning(f"`{entry.label}` — {str(exc)[:120]}")

    st.divider()
    st.subheader("Add a wallet")

    if HOSTED:
        st.error(
            f"This app is running on **{HOSTED}**, so a private key you paste here would leave "
            "your machine. Key entry is disabled. Clone the repo and run "
            "`streamlit run mintbot_ui.py` locally instead."
        )
        return

    method = st.radio(
        "How should the bot get this wallet's key?",
        ["Paste the private key (encrypted into a keystore)", "Point at an environment variable"],
        help="The keystore option is safer: the key is encrypted immediately and the plaintext "
        "is never written to disk.",
    )
    pasting = method.startswith("Paste")

    with st.form("add_wallet", clear_on_submit=True):
        cols = st.columns([2, 1])
        label = cols[0].text_input("Label", placeholder="main", key="add_label")
        quantity = cols[1].number_input("Quantity to mint", 1, 100, draft().quantity, key="add_qty")

        if pasting:
            private_key = st.text_input(
                "Private key", type="password", placeholder="0x…", key="add_key",
                help="Encrypted immediately with the password below, then discarded.",
            )
            pw_cols = st.columns(2)
            password = pw_cols[0].text_input("Keystore password", type="password", key="add_pw")
            confirm = pw_cols[1].text_input("Confirm password", type="password", key="add_pw2")
            key_env = ""
            address = ""
        else:
            key_env = st.text_input(
                "Environment variable holding the key", placeholder="MINT_KEY_MAIN", key="add_env"
            )
            address = st.text_input("Address (optional guard)", placeholder="0x…", key="add_addr")
            private_key = password = confirm = ""

        proof_text = st.text_area(
            "Merkle proof (optional)", placeholder="one 0x… hex string per line", key="add_proof",
            help="Only needed for an allowlist phase whose mint function takes a proof.",
        )

        if st.form_submit_button("Add wallet", type="primary"):
            try:
                proof = parse_proof(proof_text)
                if pasting:
                    if password != confirm:
                        raise WalletError("the two passwords do not match")
                    entry = add_wallet_from_key(
                        WS, label, private_key, password, int(quantity), proof
                    )
                else:
                    entry = add_wallet_from_env(
                        WS, label, key_env, address.strip(), int(quantity), proof
                    )
                st.success(f"Added `{entry.label}` — {entry.address or entry.key_env}")
                st.rerun()
            except (WalletError, ValueError) as exc:
                st.error(str(exc))

    if pasting:
        st.caption(
            "One password unlocks every keystore you create here. You will be asked for it "
            "once when you start the bot; it is never saved."
        )


# --------------------------------------------------------------------------- #
# tab 2 — the drop
# --------------------------------------------------------------------------- #
def render_drop_tab() -> None:
    current = draft()

    st.subheader("Contract")
    cols = st.columns([4, 1])
    address = cols[0].text_input(
        "Mint contract address", current.contract_address, placeholder="0x…"
    )
    cols[1].markdown("<br>", unsafe_allow_html=True)
    if cols[1].button("Analyse", type="primary", use_container_width=True):
        try:
            with st.spinner("Fetching the verified ABI…"):
                abi = fetch_abi(current.explorer_api, address.strip())
                save_abi(abi, WS.abi)
            st.session_state.abi = abi
            st.session_state.mints = rank_mint_functions(abi)
            st.session_state.getters = rank_phase_getters(abi)
            st.success(f"ABI saved to {WS.abi.relative_to(ROOT)} — {len(abi)} entries")
        except DiscoveryError as exc:
            st.error(str(exc))

    if WS.abi.exists() and "abi" not in st.session_state:
        abi = load_abi(WS.abi)
        st.session_state.abi = abi
        st.session_state.mints = rank_mint_functions(abi)
        st.session_state.getters = rank_phase_getters(abi)

    mints = st.session_state.get("mints", [])
    getters = st.session_state.get("getters", [])

    mint_function, mint_args = current.mint_function, current.mint_args
    getter_function, getter_expect = current.getter_function, current.getter_expect

    if mints:
        st.subheader("Mint function")
        labels = [f"{c.signature} — {c.reason}" for c in mints]
        default = next(
            (i for i, c in enumerate(mints) if c.name == current.mint_function), 0
        )
        chosen = st.radio("Which function opens the drop?", labels, index=default)
        candidate = mints[labels.index(chosen)]
        mint_function = candidate.name
        mint_args = template_args_for(candidate.entry)
        st.caption(f"arguments the bot will fill in per wallet: `{list(mint_args)}`")
        unresolved = [a for a in mint_args if a.startswith("<")]
        if unresolved:
            st.warning(
                f"The bot has no placeholder for {', '.join(unresolved)} — this signature needs "
                f"hand-editing in mintbot.toml."
            )
    elif st.session_state.get("abi"):
        st.warning("Nothing in this ABI looks like a public mint. Check the contract by hand.")

    st.subheader("Phase detection")
    modes = list(WATCH_HELP)
    mode = st.radio(
        "How should the bot decide the mint is open?",
        modes,
        index=modes.index(current.watch_mode),
        format_func=lambda m: f"{m} — {WATCH_HELP[m]}",
    )
    if mode in ("getter", "both"):
        if getters:
            labels = [f"{c.signature} — {c.reason}" for c in getters]
            default = next(
                (i for i, c in enumerate(getters) if c.name == current.getter_function), 0
            )
            chosen = st.radio("Phase flag", labels, index=default)
            candidate = getters[labels.index(chosen)]
            getter_function = candidate.name
            getter_expect = expected_open_value(candidate.entry)
            st.caption(f"the bot treats `{getter_function}() == {getter_expect}` as open")
        else:
            getter_function = st.text_input("Phase flag function", current.getter_function)
            getter_expect = coerce_expect(
                st.text_input("Value that means open", str(current.getter_expect))
            )

    st.subheader("Mint terms")
    cols = st.columns(3)
    price = cols[0].text_input(
        "Price per token", current.price, help='e.g. "0.01 eth", "5 gwei", or raw wei'
    )
    quantity = cols[1].number_input("Default quantity per wallet", 1, 100, current.quantity)
    max_per_wallet = cols[2].number_input(
        "Max per wallet", 1, 100, max(current.max_per_wallet, int(quantity))
    )

    st.subheader("Gas and timing")
    cols = st.columns(4)
    max_fee = cols[0].number_input(
        "Max fee (gwei)", 0.01, 5000.0, current.max_fee_gwei,
        help="Hard ceiling. The bot holds rather than pay above this.",
    )
    priority_fee = cols[1].number_input(
        "Priority fee (gwei)", 0.0, 5000.0, current.priority_fee_gwei
    )
    gas_limit = cols[2].number_input("Gas limit", 21_000, 5_000_000, current.gas_limit, step=10_000)
    poll = cols[3].number_input(
        "Poll interval (ms)", 50, 60_000, current.poll_interval_ms, step=50,
        help="Below ~250ms most public endpoints throttle you, which is slower, not faster.",
    )

    use_start = st.checkbox("Hold until a set time before polling", bool(current.start_at))
    start_at = ""
    if use_start:
        cols = st.columns(2)
        day = cols[0].date_input("Start date (UTC)")
        clock = cols[1].time_input("Start time (UTC)")
        start_at = datetime.combine(day, clock).replace(tzinfo=timezone.utc).isoformat()
        st.caption(f"the bot idles until `{start_at}`")

    st.subheader("Safety")
    cols = st.columns(3)
    max_spend = cols[0].number_input(
        "Max total spend (ETH)", 0.0, 1000.0, current.max_total_spend_eth,
        help="Worst case across every wallet: mint value plus gas limit × max fee. 0 disables.",
    )
    attempts = cols[1].number_input(
        "Retries per wallet", 1, 20, current.max_attempts_per_wallet
    )
    stop_first = cols[2].checkbox(
        "Stop everything after the first success", current.stop_on_first_success
    )

    rpc = st.text_input("RPC endpoint", current.rpc_url)
    fallbacks = st.text_input(
        "Fallback RPC endpoints", ", ".join(current.fallback_rpc_urls),
        help="Comma separated. The client rotates to these when a request fails.",
    )

    if st.button("Save configuration", type="primary"):
        updated = ConfigDraft(
            rpc_url=rpc.strip(),
            chain_id=current.chain_id,
            fallback_rpc_urls=tuple(u.strip() for u in fallbacks.split(",") if u.strip()),
            explorer_api=current.explorer_api,
            contract_address=address.strip(),
            abi_file=current.abi_file,
            mint_function=mint_function,
            mint_args=tuple(mint_args),
            price=price.strip(),
            quantity=int(quantity),
            max_per_wallet=int(max_per_wallet),
            max_fee_gwei=float(max_fee),
            priority_fee_gwei=float(priority_fee),
            gas_limit=int(gas_limit),
            watch_mode=mode,
            poll_interval_ms=int(poll),
            getter_function=getter_function,
            getter_expect=getter_expect,
            start_at=start_at,
            dry_run=current.dry_run,
            max_total_spend_eth=float(max_spend),
            max_attempts_per_wallet=int(attempts),
            stop_on_first_success=stop_first,
        )
        if save_draft(updated):
            st.success(f"Saved to {WS.config.relative_to(ROOT)}")
            st.rerun()


# --------------------------------------------------------------------------- #
# tab 3 — preflight
# --------------------------------------------------------------------------- #
def render_preflight_tab() -> None:
    state = readiness(WS, draft())
    if not state.ready:
        st.warning("Finish setup first: " + ", ".join(state.missing))
        return

    entries = read_wallets(WS.wallets)
    needs_password = any(e.keystore for e in entries if e.enabled)
    password = ""
    if needs_password:
        password = st.text_input(
            "Keystore password", type="password", key="preflight_password",
            help="Used to unlock your keystores for this check. Never saved.",
        )

    st.caption(
        "Preflight verifies the chain, the contract, the ABI, your balances, gas and the spend "
        "cap. It broadcasts nothing."
    )
    if not st.button("Run preflight", type="primary"):
        return
    if needs_password and not password:
        st.error("Enter the keystore password first.")
        return

    try:
        with st.spinner("Checking…"), keystore_password(password):
            config = load_config(WS.config)
            client = ChainClient(config.chain, load_abi(WS.abi))
            wallets = load_wallets(
                WS.wallets, config.mint.quantity, config.mint.max_per_wallet, allow_prompt=False
            )
            problems, notes = _preflight(config, client, wallets)
    except (ConfigError, WalletError, ChainError) as exc:
        st.error(str(exc))
        return

    for note in notes:
        st.success(note) if "MINT IS OPEN" in note else st.write(f"✅ {note}")
    for problem in problems:
        st.error(f"❌ {problem}")
    if not problems:
        st.success("Preflight clean.")


# --------------------------------------------------------------------------- #
# tab 4 — run
# --------------------------------------------------------------------------- #
def render_run_tab() -> None:
    state = readiness(WS, draft())
    if not state.ready:
        st.warning("Finish setup first: " + ", ".join(state.missing))
        return

    entries = [e for e in read_wallets(WS.wallets) if e.enabled]
    current = draft()

    cols = st.columns(4)
    cols[0].metric("Wallets", len(entries))
    cols[1].metric("Tokens", sum(e.quantity for e in entries))
    cols[2].metric("Price each", current.price)
    cols[3].metric("Mode", current.watch_mode)

    if running():
        st.info("Bot is running. It polls until the phase opens, then fires.")
        if st.button("Stop the bot", type="secondary"):
            st.session_state.process.terminate()
            st.rerun()
    else:
        mode = st.radio(
            "Run mode",
            ["Dry run — sign and probe, broadcast nothing", "LIVE — broadcast for real"],
            help="Always dry run once first. It exercises the whole path except the send.",
        )
        live = mode.startswith("LIVE")

        needs_password = any(e.keystore for e in entries)
        password = ""
        if needs_password:
            password = st.text_input("Keystore password", type="password", key="run_password")

        confirmed = True
        if live:
            st.warning(
                f"This will spend real funds from {len(entries)} wallet(s) on chain "
                f"{current.chain_id}, up to {current.max_total_spend_eth} ETH in total."
            )
            confirmed = st.text_input("Type MINT to confirm").strip() == "MINT"

        if st.button("Start the bot", type="primary", disabled=live and not confirmed):
            if needs_password and not password:
                st.error("Enter the keystore password first.")
            elif needs_password and (bad := check_keystore_password(entries, password)):
                st.error(f"That password does not open: {', '.join(bad)}")
            else:
                reset_events(WS)
                st.session_state.process = start_run(WS, live, password)
                st.rerun()

    st.divider()
    events = read_events(WS.events)
    if events:
        st.subheader("Events")
        for event in reversed(events[-25:]):
            stamp = event.get("ts", "")[11:19]
            message = event.get("message", event.get("event", ""))
            kind = event.get("event", "")
            if kind == "minted":
                st.success(f"`{stamp}` {message}")
            elif kind in ("send_failed", "wallet_error", "deadline", "reverted"):
                st.error(f"`{stamp}` {message}")
            elif kind in ("gas_too_high", "rpc_error", "send_retryable", "receipt_timeout"):
                st.warning(f"`{stamp}` {message}")
            else:
                st.write(f"`{stamp}` {message}")

    log = tail(WS.run_log, 60)
    if log:
        with st.expander("Raw output", expanded=not events):
            st.code(log, language="log")

    if running() and st.checkbox("Auto-refresh", value=True):
        time.sleep(2)
        st.rerun()


# --------------------------------------------------------------------------- #
render_sidebar()
st.title("Mint Bot")
st.caption(
    "Add your wallets, point it at the drop, then arm it. It watches the contract and fires "
    "the moment the phase actually opens."
)

wallets_tab, drop_tab, preflight_tab, run_tab = st.tabs(
    ["1 · Wallets", "2 · The drop", "3 · Preflight", "4 · Run"]
)
with wallets_tab:
    render_wallets_tab()
with drop_tab:
    render_drop_tab()
with preflight_tab:
    render_preflight_tab()
with run_tab:
    render_run_tab()
