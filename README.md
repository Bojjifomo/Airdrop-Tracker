# Airdrop Desk

Streamlit dashboard with two halves:

- **Board / Strategy** — airdrop tracker backed by live web + X research through the Claude API.
- **LP Trigger** — a scanner that watches Robinhood Chain pools and fires on coins worth LPing.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...     # or put it in .streamlit/secrets.toml
streamlit run app.py
```

## LP Trigger

A coin fires the trigger when its pool clears **both** gates:

| gate | threshold |
| --- | --- |
| market cap | > $500,000 |
| fees, 24h | > 0.5 ETH |

Fees are the pool's own 24h fees — `24h volume × pool fee tier`, converted to ETH.
That is the size of the pie an LP position takes a share of, not your share of it;
your cut scales with your share of the pool's liquidity. The tab also shows the
annualised pool fee APR against reserves, which is the number that actually
decides whether a position is worth opening.

### Running it every 3 minutes

The scanner is a standalone script, so the trigger keeps running whether or not
the dashboard is open. Add to `crontab -e`:

```cron
*/3 * * * * cd /path/to/Airdrop-Tracker && /usr/bin/python3 lp_scanner.py --quiet >> lp_scanner.log 2>&1
```

Or without cron:

```bash
python lp_scanner.py --loop 180
```

Either way it writes `lp_hits.json`, which the **LP Trigger** tab reads and
renders. The tab warns if the last scan is more than 10 minutes old, so a
stopped cron job is visible rather than silently serving stale hits.

### Tuning

```bash
python lp_scanner.py --min-mcap 1000000 --min-fees-eth 1.0   # stricter
python lp_scanner.py --window h6                             # 6h fees instead of 24h
python lp_scanner.py --network <gecko-network-id>            # skip auto-discovery
```

| env var | effect |
| --- | --- |
| `RH_NETWORK` | GeckoTerminal network id for Robinhood Chain; skips name lookup |
| `ETH_USD` | pin the ETH price instead of deriving it from pool data |
| `LP_FEE_TIERS` | JSON map of dex-id substring → fee fraction, e.g. `{"froth": 0.01}` |
| `GT_API` | alternate GeckoTerminal API base URL |

### Data notes

- Market cap uses GeckoTerminal's `market_cap_usd`, falling back to FDV when the
  token has no CoinGecko-backed cap. New launchpad tokens almost always land on
  the FDV path; each hit shows which one it used. FDV equals market cap only
  when the whole supply is circulating.
- Fee tiers come from the API's `pool_fee_percentage` when present. When it is
  missing, a per-DEX default is used (pools.trade/Uniswap v4 0.25%, froth.meme
  1%, otherwise 0.3%) and the hit is labelled `default:` or `fallback` so an
  estimated fee is never mistaken for a reported one.
- The ETH price is derived as the median of `price_usd ÷ price_native` across
  every pool in the feed, so one mispriced token cannot move it.

### Tests

```bash
pytest test_lp_scanner.py
```

The API is stubbed, so the suite runs offline with no key.
