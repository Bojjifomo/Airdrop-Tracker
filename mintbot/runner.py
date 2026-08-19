"""The watch / arm / fire loop.

Each wallet gets its own thread. A thread signs its mint transaction ahead of
time, polls until the contract actually accepts the call, then broadcasts the
already-signed bytes so the only work left at go-time is one RPC round trip.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError

from .chain import ChainClient, ChainError, FeeParams, GasTooHigh, compute_fees
from .config import Config, resolve_args
from .wallets import Wallet

log = logging.getLogger("mintbot")

DEFAULT_GAS_LIMIT = 300_000
# Send errors that are worth another attempt with refreshed parameters.
RETRYABLE_SEND_ERRORS = (
    "nonce too low", "nonce too high", "replacement transaction underpriced",
    "underpriced", "fee too low", "max fee per gas less than block base fee",
    "already known", "txpool", "future transaction",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def revert_reason(exc: BaseException) -> str:
    """Pull the human-readable revert string out of a web3 error.

    ContractLogicError stringifies to a (message, data) tuple, which reads
    badly in a log line, so prefer its .message attribute when present.
    """
    message = getattr(exc, "message", None)
    return str(message if message else exc)[:160]


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
class Reporter:
    """Console logging plus an append-only JSONL audit trail."""

    def __init__(self, log_file: str | Path | None):
        self.path = Path(log_file).expanduser() if log_file else None
        self._lock = threading.Lock()

    def event(self, kind: str, message: str = "", level: int = logging.INFO, **fields: Any) -> None:
        record = {"ts": _now().isoformat(), "event": kind, **fields}
        if message:
            record["message"] = message
        log.log(level, "%s", message or kind, extra={"mintbot_event": record})
        if not self.path:
            return
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")


# --------------------------------------------------------------------------- #
# fees
# --------------------------------------------------------------------------- #
class FeeCache:
    """One shared base-fee reading per refresh interval, for all wallets."""

    def __init__(self, client: ChainClient, config: Config):
        self.client = client
        self.config = config
        self._lock = threading.Lock()
        self._fees: FeeParams | None = None
        self._fetched_at = 0.0

    def current(self, force: bool = False) -> FeeParams:
        with self._lock:
            stale = time.monotonic() - self._fetched_at >= self.config.gas.resign_interval_s
            if self._fees is None or stale or force:
                self._fees = compute_fees(self.client.base_fee(), self.config.gas)
                self._fetched_at = time.monotonic()
            return self._fees


# --------------------------------------------------------------------------- #
# per-wallet plan
# --------------------------------------------------------------------------- #
@dataclass
class WalletPlan:
    wallet: Wallet
    calldata: str
    value: int
    gas_limit: int
    nonce: int = 0
    fees: FeeParams | None = None
    raw: bytes | None = None
    signed_at: float = 0.0

    @property
    def max_cost(self) -> int:
        fee = self.fees.max_fee_wei if self.fees else 0
        return self.value + self.gas_limit * fee


@dataclass
class WalletResult:
    label: str
    address: str
    # pending -> not decided yet; broadcast -> sent but no receipt seen
    status: str = "pending"          # pending | minted | broadcast | failed | skipped
    tx_hash: str | None = None
    attempts: int = 0
    detail: str = ""
    receipt: dict[str, Any] | None = None
    moved: int = 0                   # tokens swept to the post-mint destination
    events: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
class MintRunner:
    def __init__(
        self,
        config: Config,
        wallets: list[Wallet],
        client: ChainClient,
        reporter: Reporter | None = None,
    ):
        self.config = config
        self.wallets = wallets
        self.client = client
        self.reporter = reporter or Reporter(config.log_file)
        self.fees = FeeCache(client, config)
        self.stop_event = threading.Event()
        self.results: dict[str, WalletResult] = {
            w.label: WalletResult(w.label, w.address) for w in wallets
        }

    # -- planning ------------------------------------------------------------
    def _context(self, wallet: Wallet) -> dict[str, Any]:
        return {
            "quantity": wallet.quantity,
            "address": Web3.to_checksum_address(wallet.address),
            "proof": [HexBytes(p) for p in wallet.proof],
        }

    def build_plan(self, wallet: Wallet) -> WalletPlan:
        args = resolve_args(self.config.mint.args, self._context(wallet))
        calldata = self.client.encode_call(self.config.contract.address, self.config.mint.function, args)
        value = self.config.mint.price_wei * wallet.quantity

        gas_limit = self.config.gas.gas_limit
        if gas_limit is None:
            gas_limit = self._estimate_gas(wallet, calldata, value)
        return WalletPlan(wallet=wallet, calldata=calldata, value=value, gas_limit=gas_limit)

    def _estimate_gas(self, wallet: Wallet, calldata: str, value: int) -> int:
        """Estimate before the drop opens; fall back when the call still reverts."""
        try:
            estimate = self.client.estimate_gas(
                {
                    "from": Web3.to_checksum_address(wallet.address),
                    "to": Web3.to_checksum_address(self.config.contract.address),
                    "data": calldata,
                    "value": value,
                }
            )
            return int(estimate * self.config.gas.gas_limit_buffer)
        except (ContractLogicError, ChainError, ValueError) as exc:
            self.reporter.event(
                "gas_estimate_failed",
                f"[{wallet.label}] cannot estimate gas yet ({type(exc).__name__}) — "
                f"using {DEFAULT_GAS_LIMIT}. Set [gas].gas_limit explicitly before the drop.",
                level=logging.WARNING,
                wallet=wallet.label,
                error=str(exc)[:200],
            )
            return DEFAULT_GAS_LIMIT

    def sign(self, plan: WalletPlan, force_fees: FeeParams | None = None) -> WalletPlan:
        """(Re-)sign the plan's transaction with current fee parameters."""
        plan.fees = force_fees or self.fees.current()
        tx: dict[str, Any] = {
            "chainId": self.config.chain.chain_id,
            "to": Web3.to_checksum_address(self.config.contract.address),
            "from": Web3.to_checksum_address(plan.wallet.address),
            "data": plan.calldata,
            "value": plan.value,
            "gas": plan.gas_limit,
            "nonce": plan.nonce,
            **plan.fees.as_tx_fields(),
        }
        if not plan.fees.legacy:
            tx["type"] = 2
        signed = Account.sign_transaction(tx, plan.wallet.key)
        plan.raw = bytes(signed.raw_transaction)
        plan.signed_at = time.monotonic()
        return plan

    # -- liveness ------------------------------------------------------------
    def getter_says_live(self) -> tuple[bool, Any]:
        args = list(self.config.watch.getter_args)
        calldata = self.client.encode_call(
            self.config.contract.address, self.config.watch.getter_function, args
        )
        raw = self.client.call(
            {"to": Web3.to_checksum_address(self.config.contract.address), "data": calldata}
        )
        value = self.client.decode_result(self.config.watch.getter_function, args, raw)
        expected = self.config.watch.getter_expect
        if isinstance(expected, bool) or isinstance(value, bool):
            return bool(value) == bool(expected), value
        return value == expected, value

    def _simulation_passes(self, plan: WalletPlan) -> tuple[bool, str]:
        try:
            self.client.call(
                {
                    "from": Web3.to_checksum_address(plan.wallet.address),
                    "to": Web3.to_checksum_address(self.config.contract.address),
                    "data": plan.calldata,
                    "value": plan.value,
                }
            )
            return True, ""
        except ContractLogicError as exc:
            return False, revert_reason(exc)

    def is_live(self, plan: WalletPlan) -> tuple[bool, str]:
        mode = self.config.watch.mode
        if mode in ("getter", "both"):
            ok, value = self.getter_says_live()
            if not ok:
                return False, f"getter {self.config.watch.getter_function}() = {value!r}"
            if mode == "getter":
                return True, f"getter {self.config.watch.getter_function}() = {value!r}"
        ok, reason = self._simulation_passes(plan)
        return (True, "simulation succeeded") if ok else (False, reason or "simulation reverts")

    # -- firing --------------------------------------------------------------
    def _broadcast(
        self, plan: WalletPlan, result: WalletResult, primary: bool
    ) -> tuple[str, str | None]:
        """Send one signed transaction.

        Returns an outcome of "sent", "retry" (worth another pass with fresh
        parameters) or "fatal" (this wallet is done), plus the hash if it went.
        """
        wallet = plan.wallet
        try:
            if self.config.fire.rebroadcast:
                tx_hash, refused = self.client.broadcast_all(plan.raw or b"")
                for failure in refused:
                    self.reporter.event(
                        "endpoint_refused", f"[{wallet.label}] {failure}",
                        level=logging.WARNING, wallet=wallet.label,
                    )
            else:
                tx_hash = self.client.send_raw(plan.raw or b"")
        except Exception as exc:  # noqa: BLE001 - classified below
            message = str(exc).lower()
            if "insufficient funds" in message:
                result.status = "failed"
                result.detail = "insufficient funds for value + gas"
                self.reporter.event(
                    "send_failed", f"[{wallet.label}] insufficient funds — stopping this wallet",
                    level=logging.ERROR, wallet=wallet.label, error=str(exc)[:200],
                )
                return "fatal", None
            if any(token in message for token in RETRYABLE_SEND_ERRORS):
                self.reporter.event(
                    "send_retryable",
                    f"[{wallet.label}] send rejected ({str(exc)[:120]}) — refreshing and retrying",
                    level=logging.WARNING, wallet=wallet.label, error=str(exc)[:200],
                )
                return "retry", None
            result.status = "failed"
            result.detail = str(exc)[:200]
            self.reporter.event(
                "send_failed", f"[{wallet.label}] send failed: {str(exc)[:160]}",
                level=logging.ERROR, wallet=wallet.label, error=str(exc)[:200],
            )
            return "fatal", None

        if primary:
            result.tx_hash = tx_hash
        self.reporter.event(
            "sent", f"[{wallet.label}] sent {tx_hash}",
            wallet=wallet.label, address=wallet.address, tx_hash=tx_hash, nonce=plan.nonce,
        )
        return "sent", tx_hash

    def _confirm(
        self, plan: WalletPlan, tx_hash: str, result: WalletResult, primary: bool
    ) -> bool:
        """Wait for one receipt. True when that transaction minted."""
        wallet = plan.wallet
        try:
            receipt = self.client.wait_for_receipt(tx_hash)
        except Exception as exc:  # noqa: BLE001 - a pending tx is not a failed one
            if primary:
                result.status = "broadcast"
                result.detail = f"broadcast, but no receipt yet: {str(exc)[:120]}"
            self.reporter.event(
                "receipt_timeout", f"[{wallet.label}] no receipt yet for {tx_hash}",
                level=logging.WARNING, wallet=wallet.label, tx_hash=tx_hash,
            )
            return False

        if receipt.get("status") == 1:
            result.status = "minted"
            result.tx_hash = tx_hash
            result.receipt = receipt
            result.detail = f"mined in block {receipt.get('blockNumber')}"
            self.reporter.event(
                "minted",
                f"[{wallet.label}] MINTED — {tx_hash} in block {receipt.get('blockNumber')}",
                wallet=wallet.label, address=wallet.address, tx_hash=tx_hash,
                block=receipt.get("blockNumber"), gas_used=receipt.get("gasUsed"),
            )
            return True

        if primary:
            result.detail = "transaction reverted on-chain"
            self.reporter.event(
                "reverted", f"[{wallet.label}] tx {tx_hash} reverted — re-arming",
                level=logging.WARNING, wallet=wallet.label, tx_hash=tx_hash,
            )
        else:
            self.reporter.event(
                "extra_reverted",
                f"[{wallet.label}] follow-up tx {tx_hash} reverted — expected once the "
                f"wallet's mint allowance is used up",
                wallet=wallet.label, tx_hash=tx_hash,
            )
        return False

    def rearm(self, plans: list[WalletPlan], bump: bool = False) -> None:
        """Refresh nonces across the salvo and re-sign after a miss."""
        base = self.client.pending_nonce(plans[0].wallet.address)
        fees = None
        if bump:
            fees = (plans[0].fees or self.fees.current()).bumped(
                self.config.gas.bump_percent, self.config.gas.max_fee_wei
            )
        for offset, plan in enumerate(plans):
            plan.nonce = base + offset
            self.sign(plan, force_fees=fees)

    def _fire(self, plans: list[WalletPlan], result: WalletResult) -> bool:
        """Broadcast the whole salvo, then confirm it. True when any tx minted."""
        primary = plans[0]
        wallet = primary.wallet
        result.attempts += 1

        if self.config.safety.dry_run:
            self.reporter.event(
                "dry_run_fire",
                f"[{wallet.label}] DRY RUN — would send {len(plans)} signed transaction(s), "
                f"{len(primary.raw or b'')} bytes each (nonce {primary.nonce}, "
                f"value {primary.value} wei, gas {primary.gas_limit})",
                wallet=wallet.label, address=wallet.address, nonce=primary.nonce,
                value_wei=primary.value, gas_limit=primary.gas_limit, transactions=len(plans),
            )
            result.status = "skipped"
            result.detail = "dry run — no transaction was broadcast"
            return True

        sent: list[tuple[WalletPlan, str]] = []
        for index, plan in enumerate(plans):
            if index:
                self.stop_event.wait(self.config.fire.interval_s)
            outcome, tx_hash = self._broadcast(plan, result, primary=plan is primary)
            if outcome == "fatal":
                return False
            if outcome == "retry":
                self.rearm(plans, bump=True)
                return False
            sent.append((plan, tx_hash or ""))

        # A list, not a generator: every transaction gets confirmed and reported,
        # not just the ones before the first success.
        outcomes = [
            self._confirm(plan, tx_hash, result, primary=plan is primary)
            for plan, tx_hash in sent
        ]
        landed = any(outcomes)
        if not landed and result.status != "broadcast":
            self.rearm(plans)
        return landed

    # -- per-wallet loop -----------------------------------------------------
    def _wallet_loop(self, plans: list[WalletPlan]) -> None:
        plan = plans[0]
        wallet = plan.wallet
        result = self.results[wallet.label]
        watch = self.config.watch
        fire = self.config.fire
        max_attempts = self.config.safety.max_attempts_per_wallet

        try:
            base_nonce = self.client.pending_nonce(wallet.address)
            for offset, queued in enumerate(plans):
                queued.nonce = base_nonce + offset
            # A fee spike while arming is a reason to wait, not to drop the wallet.
            while not self.stop_event.is_set():
                try:
                    for queued in plans:
                        self.sign(queued)
                    break
                except GasTooHigh as exc:
                    self.reporter.event(
                        "gas_too_high", f"[{wallet.label}] {exc} — waiting to arm",
                        level=logging.WARNING, wallet=wallet.label,
                    )
                    self.stop_event.wait(watch.poll_interval_s)
            if plan.raw is None:
                return

            salvo = f", salvo of {len(plans)}" if len(plans) > 1 else ""
            self.reporter.event(
                "armed",
                f"[{wallet.label}] armed {wallet.short} — qty {wallet.quantity}, "
                f"nonce {plan.nonce}{salvo}, max cost "
                f"{sum(p.max_cost for p in plans) / 1e18:.6f} ETH",
                wallet=wallet.label, address=wallet.address, nonce=plan.nonce,
                quantity=wallet.quantity, transactions=len(plans),
                max_cost_wei=sum(p.max_cost for p in plans),
            )

            if fire.mode == "instant":
                moment = fire.at or watch.start_at
                self.reporter.event(
                    "instant",
                    f"[{wallet.label}] instant mode — firing at {moment.isoformat()} "
                    f"without checking the contract",
                    wallet=wallet.label, fire_at=moment.isoformat(),
                )
                remaining = (moment - _now()).total_seconds()
                if remaining > 0:
                    self.stop_event.wait(remaining)
                if self.stop_event.is_set():
                    return
                self._fire_until_done(plans, result, max_attempts)
                return

            while not self.stop_event.is_set():
                if watch.deadline_at and _now() >= watch.deadline_at:
                    result.status = "failed"
                    result.detail = "deadline passed before the mint opened"
                    self.reporter.event(
                        "deadline", f"[{wallet.label}] deadline reached — giving up",
                        level=logging.WARNING, wallet=wallet.label,
                    )
                    return

                try:
                    live, reason = self.is_live(plan)
                except GasTooHigh as exc:
                    self.reporter.event(
                        "gas_too_high", f"[{wallet.label}] {exc}",
                        level=logging.WARNING, wallet=wallet.label,
                    )
                    live, reason = False, str(exc)
                except ChainError as exc:
                    self.reporter.event(
                        "rpc_error", f"[{wallet.label}] {str(exc)[:160]}",
                        level=logging.WARNING, wallet=wallet.label, error=str(exc)[:200],
                    )
                    live, reason = False, "rpc error"

                if live:
                    self.reporter.event(
                        "live", f"[{wallet.label}] mint is LIVE ({reason}) — firing",
                        wallet=wallet.label, reason=reason,
                    )
                    self._fire_until_done(plans, result, max_attempts)
                    return

                # Not live yet: keep the signed transaction fresh against fee drift.
                if time.monotonic() - plan.signed_at >= self.config.gas.resign_interval_s:
                    try:
                        for queued in plans:
                            self.sign(queued)
                    except GasTooHigh as exc:
                        self.reporter.event(
                            "gas_too_high", f"[{wallet.label}] {exc}",
                            level=logging.WARNING, wallet=wallet.label,
                        )

                delay = watch.poll_interval_s + random.uniform(0, watch.jitter_s)
                self.stop_event.wait(delay)

        except Exception as exc:  # noqa: BLE001 - one wallet must not kill the run
            result.status = "failed"
            result.detail = f"{type(exc).__name__}: {exc}"[:200]
            self.reporter.event(
                "wallet_error", f"[{wallet.label}] {type(exc).__name__}: {str(exc)[:160]}",
                level=logging.ERROR, wallet=wallet.label, error=str(exc)[:300],
            )

    def _sweep_after_mint(self, wallet: Wallet, result: WalletResult) -> None:
        """Move what just landed to the post-mint destination, if one is set."""
        postmint = self.config.postmint
        if not postmint.enabled or result.status != "minted" or result.receipt is None:
            return
        if postmint.destination.lower() == wallet.address.lower():
            return

        # Imported here: the NFT manager builds on this module's Reporter, so a
        # module-level import would close the loop.
        from .nft import NftManager

        try:
            manager = NftManager(
                self.client, self.config.gas, self.reporter, self.config.safety.dry_run
            )
            batch = manager.rescue_from_receipt(
                wallet, self.config.contract.address, postmint.destination, result.receipt
            )
            result.moved = len(batch.confirmed)
            if batch.failed:
                self.reporter.event(
                    "post_mint_incomplete",
                    f"[{wallet.label}] {len(batch.failed)} token(s) did not move — "
                    f"they are still in the minting wallet",
                    level=logging.WARNING, wallet=wallet.label,
                )
        except Exception as exc:  # noqa: BLE001 - a failed sweep must not undo a mint
            self.reporter.event(
                "post_mint_failed",
                f"[{wallet.label}] post-mint transfer failed ({str(exc)[:140]}) — "
                f"the token is minted and still in the wallet",
                level=logging.ERROR, wallet=wallet.label, error=str(exc)[:200],
            )

    def _fire_until_done(
        self, plans: list[WalletPlan], result: WalletResult, max_attempts: int
    ) -> None:
        """Fire the salvo, retrying while attempts remain and nothing has landed."""
        while result.attempts < max_attempts and not self.stop_event.is_set():
            if self._fire(plans, result):
                self._sweep_after_mint(plans[0].wallet, result)
                if self.config.safety.stop_on_first_success:
                    self.stop_event.set()
                return
            if result.status == "failed":
                return
        if result.status not in ("minted", "skipped", "broadcast"):
            result.status = "failed"
            result.detail = result.detail or f"gave up after {result.attempts} attempts"

    # -- entry point ---------------------------------------------------------
    def wait_for_start(self) -> None:
        start = self.config.watch.start_at
        if not start:
            return
        remaining = (start - _now()).total_seconds()
        if remaining <= 0:
            return
        self.reporter.event(
            "waiting", f"holding until {start.isoformat()} ({remaining:.0f}s away)",
            start_at=start.isoformat(), seconds=remaining,
        )
        self.stop_event.wait(remaining)

    def build_salvo(self, wallet: Wallet) -> list[WalletPlan]:
        """One plan per transaction this wallet will fire, sharing its calldata."""
        primary = self.build_plan(wallet)
        extras = [
            WalletPlan(
                wallet=wallet,
                calldata=primary.calldata,
                value=primary.value,
                gas_limit=primary.gas_limit,
            )
            for _ in range(self.config.fire.transactions - 1)
        ]
        return [primary, *extras]

    def run(self, plans: list[WalletPlan] | None = None) -> dict[str, WalletResult]:
        if plans is None:
            salvos = [self.build_salvo(w) for w in self.wallets]
        else:
            # Callers (tests, the CLI) may hand over one plan per wallet directly.
            salvos = [[p] for p in plans]
        self.wait_for_start()

        threads = [
            threading.Thread(
                target=self._wallet_loop, args=(salvo,), name=f"mint-{salvo[0].wallet.label}"
            )
            for salvo in salvos
        ]
        for thread in threads:
            thread.start()
        try:
            while any(thread.is_alive() for thread in threads):
                for thread in threads:
                    thread.join(timeout=0.2)
        except KeyboardInterrupt:
            self.reporter.event("interrupted", "stopping on Ctrl-C…", level=logging.WARNING)
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=10)
        return self.results
