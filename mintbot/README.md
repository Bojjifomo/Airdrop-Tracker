# mintbot

An auto-mint bot for scheduled EVM NFT drops. You point it at a mint contract and
a set of your own wallets; it holds a pre-signed transaction per wallet, watches
the contract, and broadcasts the moment the phase actually opens.

Pre-filled for **Robinhood Chain** (chain id `4663`, RPC
`https://rpc.mainnet.chain.robinhood.com`), but nothing in it is chain-specific.

There are two ways to drive it: a control panel, or the CLI.

## Control panel

```
pip install -r requirements.txt -r requirements-mintbot.txt
streamlit run mintbot_ui.py
```

Six tabs, in order:

1. **Wallets** — paste a private key and it is encrypted into a keystore under
   `keys/` before anything touches disk; the plaintext is never written, never
   stored in session state, and never logged. One password covers every keystore
   you add, and you are asked for it once when the bot starts. You can also point
   an entry at an environment variable instead. Per-wallet quantity, merkle proof
   and an armed/disarmed toggle live here.
2. **Funding** — create wallets in bulk, send the same amount to every one of
   them from a funder, and sweep them all back into a single address afterwards.
3. **The drop** — paste the contract address, hit *Analyse*, and it pulls the
   verified ABI and offers the ranked mint entrypoints and phase flags as
   choices. Set price, quantity, gas ceiling, firing strategy and the spend cap,
   then save. This writes the same `mintbot.toml` the CLI reads.
4. **Eligibility** — ask the contract, before the drop, whether each wallet can
   actually mint.
5. **Preflight** — the full check, broadcasting nothing.
6. **Run** — dry run or live (live needs you to type `MINT`), then a live event
   feed while it watches and fires.

It handles private keys, so run it **on your own machine**. If it detects hosted
infrastructure — Streamlit Community Cloud, Spaces, Cloud Run, Heroku, Render,
Railway, Codespaces — key entry is disabled outright. `MINTBOT_ALLOW_KEYS=1` is
the deliberate override for a VPS you control yourself. Do not deploy this page
to Streamlit Community Cloud.

The panel is a separate app from the airdrop tracker in `app.py`; neither
touches the other's state.

## CLI

```
pip install -r requirements-mintbot.txt

cp mintbot.example.toml mintbot.toml
cp wallets.example.toml wallets.toml

python -m mintbot generate -n 5        # create wallets as encrypted keystores
python -m mintbot discover            # fetch the ABI, rank mint / phase functions
python -m mintbot wallets             # addresses and balances
python -m mintbot disperse --from main --amount 0.01 --live    # fund them
python -m mintbot eligibility         # can each wallet actually mint?
python -m mintbot preflight           # funds, gas, ABI, liveness — sends nothing
python -m mintbot run                 # dry run: signs everything, broadcasts nothing
python -m mintbot run --live          # arm for real
python -m mintbot nft --to 0xVault --live      # move what you minted
python -m mintbot consolidate --to 0xVault --live   # sweep the leftover gas back
```

Both read and write the same two files, so you can set up in the panel and run
from the terminal, or the other way round.

## How it decides the mint is open

The default `watch.mode = "simulate"` is the reliable signal. Every poll, the bot
`eth_call`s the *real* mint function from the *real* wallet with the *real*
value. While the phase is closed the call reverts; the instant it returns
successfully, the mint is genuinely open **for that wallet** — which means
allowlist gating, per-wallet caps, supply limits and price checks are all
covered without the bot knowing anything about how the contract implements them.

| mode | signal | when to use |
| --- | --- | --- |
| `simulate` | the mint call stops reverting | default; works on any contract |
| `getter` | a view function returns the expected value | when simulation is unavailable |
| `both` | the flag flips **and** the simulation passes | strictest; avoids a flag that flips early |

`discover` reads the verified ABI from Blockscout and prints a ranked list of
candidates for both, plus a config snippet you can paste in. Team-only
entrypoints (`ownerMint`, `devMint`, `reserve…`) are scored down so the public
one surfaces first.

## Firing strategy

`fire.mode = "probe"` is the default and the reliable one: wait until the
contract accepts the call, then send. `"instant"` skips the check entirely and
fires at `fire.at` — for a drop whose contract exposes no readable signal, where
a probe would never turn green in time.

`fire.transactions` sends that many back-to-back from each wallet on sequential
nonces. If the drop allows one mint per wallet, the extras revert and you pay
their gas; it buys you a second and third shot when a single transaction might
be dropped from a full block. It is capped at 10, and off by default.

`fire.rebroadcast` pushes the same signed bytes to every configured endpoint at
once. One transaction, one hash — this is propagation, not a duplicate mint.

## After the mint

`[postmint]` moves tokens to a vault the instant they land. The bot reads the
token ids straight out of the mint receipt's `Transfer` logs, so it moves exactly
what was minted, and a failed sweep is reported without ever undoing the mint.
The minting wallet holds a hot key; the vault should not.

## Funding a set of wallets

`disperse` sends the same amount from one wallet to every other, on sequential
nonces, refusing upfront if the funder cannot cover the total including gas.
`consolidate` sweeps the other way, taking gas out of the amount sent so a wallet
empties rather than failing for one wei, and skipping any wallet too poor to
cover the transfer instead of erroring. Both broadcast the whole batch first and
collect receipts after, so one slow confirmation does not hold up the rest.

## Eligibility

`eligibility` asks the contract three separate questions per wallet, before the
drop: the allowlist flag (it finds the right view function itself), how much the
address has already minted, and whether a live simulation of the mint succeeds.
If the contract uses a merkle allowlist it also verifies your configured proof
against the on-chain root, and reports *which* leaf encoding matched —
`keccak(address)` or `keccak(keccak(address))` — since projects differ and a
proof only verifies against the one they used.

## Latency

Each wallet signs its transaction ahead of time and re-signs every
`gas.resign_interval_s` so the fees never go stale. When the probe succeeds, the
only remaining work is one `eth_sendRawTransaction` — no estimation, no nonce
lookup, no signing in the hot path.

Two knobs matter more than the code: run it on a box near the RPC provider, and
use a dedicated endpoint. `watch.poll_interval_ms` below ~250ms will get a public
endpoint to rate-limit you, which is slower than polling politely. Put the
dedicated provider in `rpc_url` and leave the public one in `fallback_rpc_urls` —
the client rotates endpoints on any transport failure, so a throttled provider
degrades instead of taking the run down.

## Safety rails

- **`dry_run = true` by default.** Signing, probing and firing all run; only the
  broadcast is skipped. `--live` is the only way to spend anything.
- **`gas.max_fee_gwei` is a hard ceiling.** If the base fee climbs above it the
  bot holds and logs, rather than paying whatever the drop demands.
- **`safety.max_total_spend_eth`** is checked in preflight against the worst case
  across every wallet — mint value plus `gas_limit × max_fee`.
- **`mint.max_per_wallet`** rejects a wallets file that asks for more than the
  drop allows.
- **Address confirmation.** Give a wallet an `address` and the bot refuses to run
  if the key derives a different one — it catches a mispasted key before the drop,
  not after.
- **`safety.max_attempts_per_wallet`** bounds the retry loop. An on-chain revert
  (someone else took the supply) re-arms with a fresh nonce; an underpriced
  rejection re-signs with bumped fees; insufficient funds stops that wallet.
- Every action lands in `mintbot.jsonl` as one JSON object per line.

## Keys

Private keys are never written into any file this repo tracks. A wallet entry
names either an environment variable or an encrypted keystore:

```toml
[[wallet]]
label = "main"
key_env = "MINT_KEY_MAIN"          # export MINT_KEY_MAIN=0x…
address = "0xYourAddress"          # optional guard, strongly recommended
quantity = 1

[[wallet]]
label = "alt1"
keystore = "~/keys/alt1.json"      # encrypted keystore JSON
password_env = "ALT1_PASSWORD"     # omit to be prompted instead
```

Prefer the keystore form — the control panel creates one for you. Keystores are
written `0600` inside a `0700` directory. `keys/`, `mintbot.toml`,
`wallets.toml`, `mintbot.jsonl` and `mintbot.run.log` are all gitignored, and
`Wallet.__repr__` redacts the key so it cannot leak through a traceback or a log
line.

Back up `keys/` somewhere safe. Losing those files loses the wallets.

Use wallets whose balance you would be willing to lose. A bot that holds a
signed transaction is only as safe as the machine it runs on.

## Allowlist phases

If the mint takes a merkle proof, set the arguments and give each wallet its own
proof:

```toml
# mintbot.toml
[mint]
function = "allowlistMint"
args = ["{quantity}", "{proof}"]

# wallets.toml
[[wallet]]
label = "main"
key_env = "MINT_KEY_MAIN"
proof = ["0x…", "0x…"]
```

`{quantity}`, `{address}` and `{proof}` are filled in per wallet.

## Troubleshooting

| symptom | cause |
| --- | --- |
| `no contract code at 0x…` | wrong address, or right address on the wrong chain |
| `function 'mint' is not in the ABI` | run `discover`, then use the name it prints |
| `is not payable, but [mint].price is …` | the price belongs on a different function, or the mint is free |
| `cannot estimate gas yet` | normal before the drop — set `gas.gas_limit` explicitly |
| `base fee is … above your ceiling` | raise `gas.max_fee_gwei`, or accept the miss |
| liveness probe stays closed after the announced time | the phase flag may lag the announcement; `simulate` fires on the contract, not the tweet |

## What it does not do

It does not find the contract address for you, know the mint price, or bypass
anything. If a drop is per-wallet limited or allowlist gated, the bot mints
exactly what the contract lets each of your wallets mint. Some projects treat
multi-wallet minting as a rules violation and revoke; that is between you and
the project, so read their terms before pointing many wallets at one drop.

Not built, and not planned here: bulk social-account automation — driving many
X, Discord or Gmail accounts to farm allowlist spots. That is sybil behaviour
against those platforms rather than a chain interaction, and it is the one part
of a general-purpose minting toolkit this repo leaves out. The eligibility
checker covers the legitimate half of the same problem: knowing which of your
wallets actually qualified.

Not built yet, but a reasonable next step: Solana drops (a different stack
entirely — Candy Machine, Jito bundles), per-launchpad site modules, and OpenSea
listing via Seaport.

## Tests

```
pip install pytest
python -m pytest tests/ -q
```

No test touches an external network. `tests/test_integration.py` runs the full
watch/arm/fire loop against a stub JSON-RPC node on `127.0.0.1`, including real
signing and signature recovery from the broadcast bytes, and
`tests/test_ui_app.py` drives the control panel itself — adding a wallet through
the form and running preflight against that same stub node.
