"""Command line interface: mintbot wallets | discover | preflight | run."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from web3 import Web3
from web3.exceptions import ContractLogicError

from . import __version__
from .chain import ChainClient, ChainError, GasTooHigh, load_abi, resolve_overload, signature_of
from .config import Config, ConfigError, load_config, resolve_args
from .discovery import DiscoveryError, fetch_abi, rank_mint_functions, rank_phase_getters, save_abi, suggest_config
from .eligibility import EligibilityChecker
from .keystore import generate_keystores
from .nft import NftManager
from .runner import MintRunner, Reporter
from .wallet_manager import TransferError, WalletManager
from .wallets import WalletError, load_wallets

log = logging.getLogger("mintbot")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _eth(wei: int) -> str:
    return f"{wei / 1e18:.6f} ETH"


def _load(args: argparse.Namespace) -> tuple[Config, ChainClient]:
    config = load_config(args.config)
    if getattr(args, "live", False):
        config = _with_live(config)
    abi_path = config.contract.abi_path
    if abi_path and not Path(abi_path).is_absolute() and config.source_path:
        candidate = config.source_path.parent / abi_path
        abi_path = str(candidate) if candidate.exists() else abi_path
    return config, ChainClient(config.chain, load_abi(abi_path))


def _with_live(config: Config) -> Config:
    from dataclasses import replace

    return replace(config, safety=replace(config.safety, dry_run=False))


def _resolve_wallets_path(config: Config, override: str | None) -> str:
    if override:
        return override
    path = Path(config.wallets_file)
    if not path.is_absolute() and config.source_path:
        candidate = config.source_path.parent / path
        if candidate.exists():
            return str(candidate)
    return str(path)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_wallets(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    wallets = load_wallets(
        _resolve_wallets_path(config, args.wallets),
        default_quantity=config.mint.quantity,
        max_per_wallet=config.mint.max_per_wallet,
    )
    client = ChainClient(config.chain, [])
    print(f"{len(wallets)} wallet(s) loaded from {_resolve_wallets_path(config, args.wallets)}\n")
    print(f"{'label':<14} {'address':<44} {'qty':>4}  balance")
    for wallet in wallets:
        try:
            balance = _eth(client.balance(wallet.address))
        except ChainError as exc:
            balance = f"(unreachable: {str(exc)[:40]})"
        print(f"{wallet.label:<14} {wallet.address:<44} {wallet.quantity:>4}  {balance}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    explorer = args.explorer or config.chain.explorer_api
    if not explorer:
        print("set [chain].explorer_api in the config, or pass --explorer", file=sys.stderr)
        return 2

    address = args.address or config.contract.address
    print(f"fetching ABI for {address} from {explorer} …\n")
    abi = fetch_abi(explorer, address, timeout=config.chain.request_timeout)

    mints = rank_mint_functions(abi)
    getters = rank_phase_getters(abi)

    print("mint candidates (best first):")
    if not mints:
        print("  none found — inspect the ABI by hand")
    for candidate in mints[:8]:
        print(f"  [{candidate.score:>4}] {candidate.signature:<48} {candidate.reason}")

    print("\nphase-flag candidates (best first):")
    if not getters:
        print("  none found — use watch.mode = \"simulate\"")
    for candidate in getters[:8]:
        print(f"  [{candidate.score:>4}] {candidate.signature:<48} {candidate.reason}")

    out = args.out or "abi/mint.json"
    if config.source_path and not Path(out).is_absolute():
        out = str(config.source_path.parent / out)
    saved = save_abi(abi, out)
    print(f"\nABI saved to {saved} ({len(abi)} entries)")
    print("\nsuggested config:\n")
    print(suggest_config(address, mints[0] if mints else None, getters[0] if getters else None))
    print("\nVerify the price and quantity against the project's own announcement before minting.")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    config, client = _load(args)
    wallets = load_wallets(
        _resolve_wallets_path(config, args.wallets),
        default_quantity=config.mint.quantity,
        max_per_wallet=config.mint.max_per_wallet,
    )
    problems, notes = _preflight(config, client, wallets)

    for note in notes:
        print(f"  ok   {note}")
    for problem in problems:
        print(f"  FAIL {problem}")
    print()
    if problems:
        print(f"{len(problems)} problem(s) must be fixed before the drop.")
        return 1
    print("Preflight clean." + ("" if config.safety.dry_run else "  Bot is armed for a LIVE run."))
    return 0


def _preflight(config: Config, client: ChainClient, wallets) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    runner = MintRunner(config, wallets, client, Reporter(None))

    try:
        client.verify_chain_id()
        notes.append(f"chain id {config.chain.chain_id} confirmed via {client.endpoint}")
    except ChainError as exc:
        problems.append(str(exc))
        return problems, notes

    address = Web3.to_checksum_address(config.contract.address)
    code = client.code_at(address)
    if len(code) == 0:
        problems.append(f"no contract code at {address} — wrong address or wrong chain")
    else:
        notes.append(f"contract at {address} has {len(code)} bytes of code")

    sample = resolve_args(
        config.mint.args,
        {"quantity": config.mint.quantity, "address": address, "proof": []},
    )
    try:
        entry = resolve_overload(client.abi, config.mint.function, sample)
        notes.append(f"mint entrypoint resolves to {signature_of(entry)}")
        if entry.get("stateMutability") != "payable" and config.mint.price_wei:
            problems.append(
                f"{signature_of(entry)} is not payable, but [mint].price is "
                f"{config.mint.price_wei} wei — the call would revert"
            )
    except ChainError as exc:
        problems.append(str(exc))

    if config.watch.mode in ("getter", "both"):
        try:
            ok, value = runner.getter_says_live()
            notes.append(
                f"{config.watch.getter_function}() currently returns {value!r} "
                f"(expecting {config.watch.getter_expect!r}) — "
                f"{'phase is OPEN' if ok else 'phase is closed, as expected pre-drop'}"
            )
        except (ChainError, ContractLogicError) as exc:
            problems.append(
                f"cannot read {config.watch.getter_function}(): {str(exc)[:160]}"
            )

    try:
        fees = runner.fees.current(force=True)
        notes.append(
            f"base fee {fees.base_fee_wei / 1e9:.4f} gwei, "
            f"sending at up to {fees.max_fee_wei / 1e9:.4f} gwei"
        )
    except GasTooHigh as exc:
        notes.append(f"gas currently above your ceiling ({exc}) — the bot will hold, not overpay")
    except ChainError as exc:
        problems.append(f"cannot read gas prices: {exc}")

    total = 0
    for wallet in wallets:
        try:
            plan = runner.build_plan(wallet)
            plan.fees = runner.fees.current()
        except (ChainError, GasTooHigh, ConfigError) as exc:
            problems.append(f"wallet '{wallet.label}': {str(exc)[:160]}")
            continue

        balance = client.balance(wallet.address)
        total += plan.max_cost
        if balance < plan.max_cost:
            problems.append(
                f"wallet '{wallet.label}' ({wallet.short}) holds {_eth(balance)} but needs "
                f"up to {_eth(plan.max_cost)} (mint value + worst-case gas)"
            )
        else:
            notes.append(
                f"wallet '{wallet.label}' ({wallet.short}) qty {wallet.quantity}: "
                f"balance {_eth(balance)}, worst case {_eth(plan.max_cost)}"
            )

        live, reason = runner.is_live(plan)
        notes.append(
            f"wallet '{wallet.label}' liveness probe: "
            + ("MINT IS OPEN RIGHT NOW" if live else f"closed ({reason[:100]})")
        )

    cap = config.safety.max_total_spend_wei
    if cap and total > cap:
        problems.append(
            f"worst-case total spend {_eth(total)} exceeds "
            f"[safety].max_total_spend_eth ({_eth(cap)})"
        )
    else:
        notes.append(f"worst-case total across all wallets: {_eth(total)}")

    return problems, notes


def _wallets_for(config: Config, args: argparse.Namespace):
    return load_wallets(
        _resolve_wallets_path(config, args.wallets),
        default_quantity=config.mint.quantity,
        max_per_wallet=config.mint.max_per_wallet,
    )


def cmd_generate(args: argparse.Namespace) -> int:
    import getpass

    password = os.environ.get("MINTBOT_PASSWORD") or getpass.getpass(
        "Password to encrypt the new keystores: "
    )
    if not password:
        print("a password is required", file=sys.stderr)
        return 2

    created = generate_keystores(args.count, password, keys_dir=args.keys_dir, prefix=args.prefix)
    print(f"{len(created)} wallet(s) created in {args.keys_dir}\n")
    for key in created:
        print(f"{key.label:<14} {key.address}")
    print(
        "\nAdd them to wallets.toml (or the control panel) before funding. "
        "Back up that directory — the keystores are the only copy of these keys."
    )
    return 0


def cmd_eligibility(args: argparse.Namespace) -> int:
    config, client = _load(args)
    wallets = _wallets_for(config, args)
    runner = MintRunner(config, wallets, client, Reporter(None))
    checker = EligibilityChecker(client, config.contract.address, runner)

    print(f"Checking {len(wallets)} wallet(s) against {config.contract.address}\n")
    verdicts = checker.check(wallets)
    print(f"{'label':<14} {'address':<44} {'minted':>7}  verdict")
    for verdict in verdicts:
        minted = "-" if verdict.already_minted is None else str(verdict.already_minted)
        print(f"{verdict.label:<14} {verdict.address:<44} {minted:>7}  {verdict.verdict}")
        for note in verdict.notes:
            print(f"{'':<14} note: {note}")
        if verdict.proof_matches:
            print(f"{'':<14} proof verifies as {verdict.proof_matches}")

    blocked = [v for v in verdicts if not v.ok]
    if blocked:
        print(f"\n{len(blocked)} wallet(s) look ineligible: {', '.join(v.label for v in blocked)}")
    return 0


def cmd_disperse(args: argparse.Namespace) -> int:
    config, client = _load(args)
    wallets = _wallets_for(config, args)
    funder = next((w for w in wallets if w.label == args.source), None)
    if funder is None:
        print(f"no wallet labelled '{args.source}' in your wallets file", file=sys.stderr)
        return 2

    recipients = [w.address for w in wallets if w.label != args.source]
    if not recipients:
        print("nothing to fund — the wallets file has only the funder", file=sys.stderr)
        return 2

    amount = int(args.amount * 1e18)
    manager = WalletManager(client, config.gas, Reporter(config.log_file), config.safety.dry_run)
    try:
        batch = manager.disperse(funder, recipients, amount)
    except TransferError as exc:
        log.error("%s", exc)
        return 1

    for transfer in batch.transfers:
        print(f"{transfer.label:<28} {transfer.status:<10} {transfer.tx_hash or transfer.detail}")
    print(f"\n{batch.summary()}")
    return 1 if batch.failed else 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    config, client = _load(args)
    wallets = _wallets_for(config, args)
    manager = WalletManager(client, config.gas, Reporter(config.log_file), config.safety.dry_run)

    batch = manager.consolidate(wallets, args.to, leave_wei=int(args.leave * 1e18))
    for transfer in batch.transfers:
        print(
            f"{transfer.label:<14} {transfer.status:<10} {transfer.amount_eth:>12.6f}  "
            f"{transfer.tx_hash or transfer.detail}"
        )
    print(f"\n{batch.summary()}")
    return 1 if batch.failed else 0


def cmd_nft(args: argparse.Namespace) -> int:
    config, client = _load(args)
    wallets = _wallets_for(config, args)
    manager = NftManager(client, config.gas, Reporter(config.log_file), config.safety.dry_run)
    collection = args.contract or config.contract.address

    holdings = manager.balances(collection, [w.address for w in wallets])
    print(f"{manager.collection_name(collection)} — {collection}\n")
    print(f"{'label':<14} {'address':<44} {'held':>5}")
    for wallet in wallets:
        print(f"{wallet.label:<14} {wallet.address:<44} {holdings[wallet.address]:>5}")

    if not args.to:
        return 0

    moved = 0
    for wallet in wallets:
        ids = manager.owned_ids(collection, wallet.address)
        if not ids:
            continue
        batch = manager.transfer(wallet, collection, args.to, ids)
        moved += len(batch.confirmed)
        for transfer in batch.transfers:
            print(f"{transfer.label:<20} {transfer.status:<10} {transfer.tx_hash or transfer.detail}")
    print(f"\n{moved} token(s) moved to {args.to}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config, client = _load(args)
    wallets = load_wallets(
        _resolve_wallets_path(config, args.wallets),
        default_quantity=config.mint.quantity,
        max_per_wallet=config.mint.max_per_wallet,
    )

    if not args.skip_preflight:
        problems, notes = _preflight(config, client, wallets)
        for note in notes:
            log.info("preflight ok: %s", note)
        if problems:
            for problem in problems:
                log.error("preflight FAIL: %s", problem)
            log.error("refusing to run with %d unresolved problem(s)", len(problems))
            return 1

    if config.safety.dry_run:
        log.warning("DRY RUN — nothing will be broadcast. Re-run with --live to mint for real.")
    elif config.safety.confirm and not args.yes:
        total = sum(config.mint.price_wei * w.quantity for w in wallets)
        print(
            f"\nAbout to arm {len(wallets)} wallet(s) for a LIVE mint on chain "
            f"{config.chain.chain_id}, spending up to {_eth(total)} in mint value plus gas."
        )
        if input("Type MINT to confirm: ").strip() != "MINT":
            print("aborted.")
            return 130

    runner = MintRunner(config, wallets, client, Reporter(config.log_file))
    results = runner.run()

    print("\n--- result ---")
    exit_code = 0
    for result in results.values():
        line = f"{result.label:<14} {result.status:<8} attempts={result.attempts}"
        if result.tx_hash:
            line += f" {result.tx_hash}"
        if result.detail:
            line += f"  ({result.detail})"
        print(line)
        if result.status == "failed":
            exit_code = 1
    return exit_code


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mintbot", description="Auto-mint bot for scheduled EVM NFT drops."
    )
    parser.add_argument("--version", action="version", version=f"mintbot {__version__}")
    parser.add_argument("-c", "--config", default="mintbot.toml", help="path to the config TOML")
    parser.add_argument("-w", "--wallets", default=None, help="override the wallets file path")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    wallets = sub.add_parser("wallets", help="list the configured wallets and their balances")
    wallets.set_defaults(func=cmd_wallets)

    discover = sub.add_parser("discover", help="fetch the ABI and rank mint/phase functions")
    discover.add_argument("--address", help="contract address (defaults to the config)")
    discover.add_argument("--explorer", help="explorer API base (defaults to the config)")
    discover.add_argument("--out", help="where to write the ABI (default abi/mint.json)")
    discover.set_defaults(func=cmd_discover)

    preflight = sub.add_parser("preflight", help="check everything without sending anything")
    preflight.set_defaults(func=cmd_preflight)

    generate = sub.add_parser("generate", help="create new wallets as encrypted keystores")
    generate.add_argument("-n", "--count", type=int, default=1, help="how many to create")
    generate.add_argument("--prefix", default="wallet", help="label prefix (default: wallet)")
    generate.add_argument("--keys-dir", default="keys", help="where to write the keystores")
    generate.set_defaults(func=cmd_generate)

    eligibility = sub.add_parser(
        "eligibility", help="check every wallet against the drop's allowlist"
    )
    eligibility.set_defaults(func=cmd_eligibility)

    disperse = sub.add_parser("disperse", help="fund every wallet from one of them")
    disperse.add_argument("--from", dest="source", required=True, help="label of the funding wallet")
    disperse.add_argument("--amount", type=float, required=True, help="ETH to send to each wallet")
    disperse.add_argument("--live", action="store_true", help="actually broadcast")
    disperse.set_defaults(func=cmd_disperse)

    consolidate = sub.add_parser("consolidate", help="sweep every wallet into one address")
    consolidate.add_argument("--to", required=True, help="destination address")
    consolidate.add_argument(
        "--leave", type=float, default=0.0, help="ETH to leave behind in each wallet"
    )
    consolidate.add_argument("--live", action="store_true", help="actually broadcast")
    consolidate.set_defaults(func=cmd_consolidate)

    nft = sub.add_parser("nft", help="list NFT holdings, and optionally move them")
    nft.add_argument("--contract", help="collection address (defaults to the mint contract)")
    nft.add_argument("--to", help="move every held token to this address")
    nft.add_argument("--live", action="store_true", help="actually broadcast")
    nft.set_defaults(func=cmd_nft)

    run = sub.add_parser("run", help="watch the contract and mint when the phase opens")
    run.add_argument("--live", action="store_true", help="actually broadcast (overrides dry_run)")
    run.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    run.add_argument("--skip-preflight", action="store_true", help="do not run preflight first")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except (ConfigError, WalletError, ChainError, DiscoveryError, TransferError) as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
