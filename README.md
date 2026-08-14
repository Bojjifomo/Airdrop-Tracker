# Airdrop Desk

Streamlit dashboard with two halves:

- **Board / Strategy** — airdrop tracker backed by live web + X research through the Claude API.
- **LP Trigger** — a scanner that watches Robinhood Chain coins and fires on the ones worth LPing.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # or put it in .streamlit/secrets.toml
streamlit run app.py
```

## LP Trigger

A coin fires the trigger when it clears **both** gates:

| gate | threshold |
| --- | --- |
| market cap | > $500,000 |
| fees, 24h | > 0.5 ETH |

Fees are the pool's own fees over the window — `volume × fee tier`, converted to
ETH. That is the size of the pie an LP position takes a share of, not your share
of it; your cut scales with your share of the pool's liquidity. The tab also
shows the annualised pool fee APR against reserves, which is the number that
actually decides whether a position is worth opening.

Coins flagged as honeypots are dropped before the gates are applied. A honeypot
can post a convincing cap and volume while being unsellable, which is exactly
the trap to fail closed on when the plan is to park liquidity in it. Override
with `--allow-honeypot` if you want to see them.

### Sources

```bash
python lp_scanner.py --source gmgn --eth-usd 3200      # default
python lp_scanner.py --source geckoterminal            # measured fee tiers
python lp_scanner.py --source custom --eth-usd 3200    # FOMO or anything else
```

| source | fee tier | ETH price | notes |
| --- | --- | --- | --- |
| `gmgn` | **assumed** (`--fee-rate`, default 0.25%) | must be supplied | Widest Robinhood Chain launchpad coverage. Also returns honeypot flags, buy/sell taxes and holder counts. Cloudflare-protected. |
| `geckoterminal` | **reported per pool** | derived automatically | Fees are measured rather than guessed, at the cost of narrower launchpad coverage. |
| `custom` | assumed | must be supplied | Configurable JSON adapter — see below. |

**The fee tier is the weak point on GMGN.** Its ranking API returns market cap,
volume and liquidity but no pool fee tier, so fees are `volume × --fee-rate`.
Guess the tier wrong and every fee figure moves by that multiple: a coin in a 1%
pool scored at 0.25% reads four times too low. Set `--fee-rate` to match the
launchpad you're targeting (pools.trade / Uniswap v4 is 0.25%, froth.meme is 1%),
or cross-check a candidate on `--source geckoterminal`, which reports the real
tier. Every hit is labelled `assumed:` or `api` so the two are never confused.

GMGN sits behind Cloudflare, which rejects the default Python TLS fingerprint.
Install `curl_cffi` and the scanner will present a real Chrome handshake:

```bash
pip install curl_cffi
```

Without it you'll get a clear bot-challenge error rather than a silent empty
result. `GMGN_API` can also be pointed at a scraping proxy you control.

### FOMO and other venues

FOMO publishes no API documentation, so rather than guess at endpoints the
`custom` source lets you point at the request the site actually makes. Open it,
copy the token-list request out of the browser network tab, then:

```bash
export LP_SOURCE_URL='https://<the endpoint you copied>'
export LP_SOURCE_ITEMS='data.tokens'        # dotted path to the list
export LP_SOURCE_MAP='{"symbol":"ticker","mcap_usd":"stats.marketCap","volume_usd":"stats.volume24h","reserve_usd":"stats.liquidityUsd","token_address":"contract"}'
python lp_scanner.py --source custom --eth-usd 3200
```

Field values in `LP_SOURCE_MAP` are dotted paths, so nested JSON works. If the
path is wrong the scanner names the keys it actually found instead of returning
an empty board.

### Running it every 3 minutes

The scanner is a standalone script, so the trigger keeps running whether or not
the dashboard is open. Add to `crontab -e`:

```cron
*/3 * * * * cd /path/to/Airdrop-Tracker && ETH_USD=3200 /usr/bin/python3 lp_scanner.py --source gmgn --quiet >> lp_scanner.log 2>&1
```

Or without cron:

```bash
python lp_scanner.py --source gmgn --loop 180
```

Either way it writes `lp_hits.json`, which the **LP Trigger** tab reads and
renders. The tab warns if the last scan is more than 10 minutes old, so a
stopped job is visible rather than silently serving stale hits.

### Tuning

```bash
python lp_scanner.py --min-mcap 1000000 --min-fees-eth 1.0   # stricter
python lp_scanner.py --window h6                             # 6h fees instead of 24h
python lp_scanner.py --fee-rate 0.01                         # assume a 1% pool
python lp_scanner.py --network <chain-slug>                  # skip auto-discovery
```

| env var | effect |
| --- | --- |
| `LP_SOURCE` | default source when `--source` is omitted |
| `RH_NETWORK` | chain id/slug for the source; skips GeckoTerminal's name lookup |
| `ETH_USD` | ETH price for sources that cannot derive one |
| `ETH_PRICE_URL` / `ETH_PRICE_PATH` | fetch the ETH price from any feed you can reach; path is dotted, e.g. `data.usd` |
| `LP_FEE_TIERS` | JSON map of dex-id substring → fee fraction, e.g. `{"froth": 0.01}` |
| `GMGN_API` / `GT_API` | alternate API base URLs |
| `LP_SOURCE_URL` / `_ITEMS` / `_MAP` / `_PARAMS` | configure the `custom` source |

ETH price precedence: `--eth-usd` → `ETH_USD` → source-derived → `ETH_PRICE_URL`.
If none resolve, the scan fails with an explanation instead of inventing a rate.

### Data notes

- On GeckoTerminal, market cap uses `market_cap_usd` and falls back to FDV when
  the token has no CoinGecko-backed cap. New launchpad tokens almost always land
  on the FDV path; each hit shows which one it used. FDV equals market cap only
  when the whole supply is circulating.
- GeckoTerminal fee tiers come from `pool_fee_percentage` when present, else a
  per-DEX default (pools.trade/Uniswap v4 0.25%, froth.meme 1%, otherwise 0.3%),
  labelled `default:` or `fallback` so an estimate is never read as a reported
  figure.
- The GeckoTerminal ETH price is the median of `price_usd ÷ price_native` across
  the whole feed, so one mispriced token cannot move it.

### Tests

```bash
pytest test_lp_scanner.py
```

Every API is stubbed, so the suite runs offline with no key.
