"""
Airdrop Desk — Streamlit airdrop tracker with live web/X research.

Run locally:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...     # or put it in .streamlit/secrets.toml
    streamlit run app.py

Deploy on Streamlit Community Cloud:
    push this repo to GitHub, "New app" -> pick app.py,
    then add ANTHROPIC_API_KEY under Settings -> Secrets.
"""

import json
import os
from datetime import datetime, timezone

import streamlit as st
from anthropic import Anthropic

import lp_scanner
import lp_sources

# ----------------------------- config ---------------------------------------
MODEL = "claude-sonnet-5"            # swap to claude-opus-5 for deeper research
DATA_FILE = "airdrops.json"
RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "airdrops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "chain": {"type": "string"},
                    "tier": {"type": "string", "enum": ["recommended", "worth", "extras", "watch"]},
                    "status": {"type": "string", "enum": [
                        "points_live", "rewards_live", "token_confirmed",
                        "no_token", "rumored", "ended",
                    ]},
                    "capital": {"type": "string"},
                    "effort": {"type": "string"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "probability": {"type": "integer"},
                    "probability_note": {"type": "string"},
                    "evidence_date": {"type": "string"},
                    "description": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                            "required": ["title", "url"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "id", "name", "chain", "tier", "status", "capital", "effort",
                    "risk", "confidence", "probability", "probability_note",
                    "evidence_date", "description", "steps", "sources",
                ],
                "additionalProperties": False,
            },
        },
        "removals": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "airdrops", "removals"],
    "additionalProperties": False,
}
TIERS = [
    ("recommended", "Recommended", "Strongest reward evidence, executable steps, capital-efficient."),
    ("worth", "Worth Farming", "Good fit if capital and risk suit you; reward link less certain."),
    ("extras", "Low-Commitment Extras", "Little or no dedicated capital; weaker or speculative signal."),
    ("watch", "Watchlist", "Rumored or unverified. Research before committing anything."),
]
STATUS_LABEL = {
    "points_live": "Points live", "rewards_live": "Rewards live",
    "token_confirmed": "Token confirmed", "no_token": "No token announced",
    "rumored": "Rumored", "ended": "Ended",
}

SEED = [
    {"id": "bulk", "name": "Bulk", "chain": "Solana", "tier": "recommended", "status": "points_live",
     "capital": "$25+", "effort": "10 min/wk", "risk": "medium", "confidence": "high",
     "probability": 65, "probability_note": "Live points, clear pre-mainnet mechanics, no token terms published.",
     "description": "Pre-deposit USDC before mainnet; weekly Saturday AURA distributions based on amount and time.",
     "steps": ["Deposit Solana USDC into Bulk pre-deposit", "Hold through weekly distributions", "Decide withdrawal before mainnet"],
     "sources": [], "updated": "seed"},
    {"id": "xstocks", "name": "xStocks", "chain": "Solana", "tier": "recommended", "status": "points_live",
     "capital": "$50+", "effort": "20 min/wk", "risk": "medium", "confidence": "high",
     "probability": 60, "probability_note": "Active points program and real product traction; token not formally confirmed.",
     "description": "Hold tokenized equities for xPoints, or deploy via a supported lending/LP venue for a higher tier.",
     "steps": ["Buy a tokenized equity", "Optionally deploy via a listed lender or LP venue", "Track xPoints portal"],
     "sources": [], "updated": "seed"},
    {"id": "loopscale", "name": "Loopscale", "chain": "Solana", "tier": "worth", "status": "points_live",
     "capital": "$50+", "effort": "30 min/wk", "risk": "medium", "confidence": "medium",
     "probability": 45, "probability_note": "Points live but no announced token destination.",
     "description": "Participation points via lending/borrowing/loops; no announced token destination yet.",
     "steps": ["Lend or loop supported assets", "Stack partner rewards on supported collateral"],
     "sources": [], "updated": "seed"},
    {"id": "hylo", "name": "Hylo", "chain": "Solana", "tier": "worth", "status": "points_live",
     "capital": "$25+", "effort": "15 min/wk", "risk": "medium", "confidence": "medium",
     "probability": 45, "probability_note": "Season 1 XP running; conversion to token unstated.",
     "description": "Hold one supported Hylo asset; capital and time build Season 1 XP. Pick by risk profile first.",
     "steps": ["Choose stable / staking / leveraged product", "Hold and let XP accrue"],
     "sources": [], "updated": "seed"},
    {"id": "titan", "name": "Titan", "chain": "Solana", "tier": "extras", "status": "no_token",
     "capital": "$0+", "effort": "10 min/wk", "risk": "low", "confidence": "low",
     "probability": 25, "probability_note": "No token or active campaign announced; purely speculative.",
     "description": "Use Titan for genuine swaps when its quote is competitive. No confirmed token or campaign.",
     "steps": ["Route real swaps through Titan when pricing wins"], "sources": [], "updated": "seed"},
]

# ----------------------------- persistence ----------------------------------
def load_state():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"airdrops": SEED, "strategy": [], "log": []}

def save_state():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(
                {"airdrops": st.session_state.airdrops,
                 "strategy": st.session_state.strategy,
                 "log": st.session_state.log[-30:]},
                f, indent=2,
            )
    except Exception:
        pass  # ephemeral FS on Streamlit Cloud; use the sidebar backup instead

# ----------------------------- research -------------------------------------
def get_client():
    key = st.secrets.get("ANTHROPIC_API_KEY", None) if hasattr(st, "secrets") else None
    key = key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return Anthropic(api_key=key)

def research_prompt(query, existing):
    tracked = "; ".join(f"{a['id']}|{a['name']}|{a['tier']}|{a['status']}" for a in existing) or "none"
    today = datetime.now()
    return f"""You are the research engine of a crypto airdrop tracker dashboard. Today is {today:%A %d %B %Y}.

USER REQUEST: "{query}"

CURRENTLY TRACKED (id|name|tier|status): {tracked}

You have a budget of at most 8 web searches — spend them on genuine discovery, not busywork:
1. Open with 1-2 broad, date-anchored queries (e.g. "solana airdrop {today:%B %Y}", "new solana points program {today.year}") aimed at finding something NOT already in CURRENTLY TRACKED — a query with no date terms tends to surface stale evergreen pages.
2. Spend up to 4 more verifying the single strongest NEW candidate that turns up: project name + "airdrop"/"snapshot"/"TGE", or the team's own X/Twitter account. Don't fan out across many candidates in parallel — pick the strongest lead and confirm it properly.
3. Only spend a search re-checking a CURRENTLY TRACKED project if the request specifically asks to re-score it — your job when asked for new/hot picks is to find something new, not re-confirm what's already on the board.
4. QUALITY BAR: CURRENTLY TRACKED is the bar a new candidate has to clear — a real protocol with a live points/rewards program, an identifiable team, and a primary source behind it, not a vague rumor. A candidate weaker than what's already tracked isn't worth adding just to have something "new" to show. If nothing you find clears that bar, say so plainly in "summary" instead of padding the list or silently re-listing what's already tracked.

Search the open web AND recent X/Twitter posts for announcements, points programs, snapshot rumors and TGE news.

SOURCE QUALITY: this space is full of programmatic-SEO "Top 10 airdrops to farm in 2026" listicles that repeat rumors from each other with no real signal behind them — treat those as noise, not evidence. A finding only counts as verified if it traces back to a PRIMARY source: the project's own domain/docs, the project's own X/Twitter account, or a named reputable crypto data source (DeFiLlama, CoinGecko, a chain foundation's own blog, an established crypto news outlet). If the only thing you found on a candidate is an aggregator roundup with no primary source behind it, don't add or upgrade it — mention it's unverified in "summary" instead of adding it as a tracked airdrop. Every URL you put in "sources" must be a primary source, never a listicle or roundup page. Treat unverified X rumors as tier "watch" with confidence "low".

For EACH airdrop estimate "probability": an integer 0-100 = chance it actually pays out a token/reward to a normal farmer within ~12 months, grounded in evidence. Anchor: token confirmed + live snapshot 75-90; live points, credible team, no token terms 45-65; points with no announced destination 30-45; rumor-only 10-25; ended/dead <10. Put the one-line reasoning in "probability_note" citing the concrete signal.

EVIDENCE RECENCY: put the date of the most recent primary-source evidence behind your score in "evidence_date", as "YYYY-MM" (or "YYYY-MM-DD" when you have the exact day) — e.g. the date of the announcement, docs update, or post you're relying on. Use "unknown" only if the source genuinely carries no date. Recency is part of the judgement, not decoration: evidence more than ~3 months old is a weaker basis for a live score, so cap "confidence" at "medium" when your newest evidence is that old, and say so in "probability_note" (e.g. "points live per docs, but nothing newer than Nov 2025"). A stale program may have quietly ended — treat old evidence as uncertainty, not as confirmation.

Be terse. Max 5 airdrops, sources max 2 each. "capital" like "$25+", "effort" like "15 min/wk". "tier" is one of recommended|worth|extras|watch. "status" is one of points_live|rewards_live|token_confirmed|no_token|rumored|ended.

Rules: reuse existing ids when updating a tracked project. Only include airdrops relevant to the request. Keep every string short."""

def run_research(client, query):
    msg = client.messages.create(
        model=MODEL,
        max_tokens=6000,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": RESEARCH_SCHEMA}},
        messages=[{"role": "user", "content": research_prompt(query, st.session_state.airdrops)}],
    )
    text = next(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return json.loads(text)

def apply_result(result):
    stamp = datetime.now()
    now = stamp.strftime("%d %b %H:%M")
    ads = st.session_state.airdrops
    for rid in result.get("removals", []):
        ads = [a for a in ads if a["id"] != rid]
    for item in result.get("airdrops", []):
        item["updated"] = now
        item["updated_ts"] = stamp.isoformat(timespec="seconds")
        idx = next((i for i, a in enumerate(ads) if a["id"] == item["id"]), None)
        if idx is not None:
            ads[idx] = {**ads[idx], **item}
        else:
            ads.append(item)
    st.session_state.airdrops = ads
    touched = ", ".join(a["name"] for a in result.get("airdrops", []))
    summary = result.get("summary", "Done.") + (f" — updated: {touched}" if touched else "")
    st.session_state.log.append({"who": "desk", "text": summary, "at": now})

def ask(query):
    st.session_state.pending = query.strip()

# ----------------------------- state init -----------------------------------
st.set_page_config(page_title="Airdrop Desk", page_icon="🪂", layout="wide")
if "airdrops" not in st.session_state:
    s = load_state()
    st.session_state.airdrops = s["airdrops"]
    st.session_state.strategy = s["strategy"]
    st.session_state.log = s["log"]
    st.session_state.pending = None

client = get_client()

# process a pending research request (set by any button / input)
if st.session_state.get("pending"):
    q = st.session_state.pending
    st.session_state.pending = None
    now = datetime.now().strftime("%d %b %H:%M")
    st.session_state.log.append({"who": "you", "text": q, "at": now})
    if client is None:
        st.session_state.log.append({"who": "desk", "text": "No API key set. Add ANTHROPIC_API_KEY in secrets.", "at": now})
    else:
        with st.spinner("Searching web + X, scoring chances…"):
            try:
                apply_result(run_research(client, q))
            except Exception as ex:
                st.session_state.log.append({"who": "desk", "text": f"Research failed: {ex}", "at": now})
    save_state()

# ----------------------------- styles ---------------------------------------
st.markdown("""<style>
.block-container {max-width: 1100px;}
.prob-bar {height:6px;border-radius:6px;background:#e6e3da;overflow:hidden;margin:2px 0 4px;}
.prob-fill {height:100%;border-radius:6px;}
.chip {font-family:monospace;font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid #ccc;}
.meta {font-family:monospace;font-size:12px;color:#555;}
small.note {color:#888;}
</style>""", unsafe_allow_html=True)

def prob_color(p):
    return "#2f9e7f" if p >= 65 else "#e0a723" if p >= 40 else "#cc5f43"

def days_since_check(a):
    """Days since this entry was last re-scored, or None if never/unparseable."""
    ts = a.get("updated_ts")
    if not ts:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).days
    except ValueError:
        return None

# ----------------------------- header ---------------------------------------
st.title("🪂 Airdrop Desk")
c1, c2 = st.columns([3, 1])
c1.caption(f"{len(st.session_state.airdrops)} tracked · {len(st.session_state.strategy)} in strategy · live web + X research")
if client is None:
    c2.error("No API key")
else:
    c2.success("Research online")

# ----------------------------- research console -----------------------------
with st.container(border=True):
    if st.session_state.log:
        for l in st.session_state.log[-6:]:
            who = "🟡 you" if l["who"] == "you" else "🟢 desk"
            st.markdown(f"**{who}** <small class='note'>{l['at']}</small><br>{l['text']}", unsafe_allow_html=True)
    else:
        st.caption("Ask the desk anything. It searches the web and X, scores payout chances, and updates the board.")

    q = st.text_input("Ask the desk", placeholder='e.g. "new Solana airdrops this week" or "peluang airdrop Hylo?"',
                      label_visibility="collapsed", key="query_box")
    b1, b2, b3, b4 = st.columns(4)
    if b1.button("Research", use_container_width=True, type="primary") and q.strip():
        ask(q); st.rerun()
    if b2.button("New on Solana", use_container_width=True):
        ask("Find the newest Solana airdrop opportunities announced this week, including anything trending on X."); st.rerun()
    if b3.button("Score chances", use_container_width=True):
        ask("Re-score the payout probability of the airdrops I'm tracking on the latest evidence — flag any whose chances rose or dropped and why."); st.rerun()
    if b4.button("X rumors", use_container_width=True):
        ask("Any airdrop rumors or snapshot speculation trending on crypto X right now? Add credible ones to the watchlist."); st.rerun()

# ----------------------------- board ----------------------------------------
def render_card(a):
    with st.container(border=True):
        top = st.columns([3, 2])
        top[0].markdown(f"**{a['name']}**  \n<span class='meta'>{a.get('chain','')}</span>", unsafe_allow_html=True)
        top[1].markdown(f"<div style='text-align:right'><span class='chip'>{STATUS_LABEL.get(a['status'], a['status'])}</span></div>", unsafe_allow_html=True)
        st.write(a.get("description", ""))

        p = a.get("probability")
        if isinstance(p, (int, float)):
            col = prob_color(p)
            st.markdown(
                f"<div class='meta'>PAYOUT CHANCE &nbsp; <b style='color:{col}'>{p}%</b></div>"
                f"<div class='prob-bar'><div class='prob-fill' style='width:{p}%;background:{col}'></div></div>"
                f"<small class='note'>{a.get('probability_note','')}</small>",
                unsafe_allow_html=True,
            )
        st.markdown(
            f"<div class='meta'>{a.get('capital','')} · {a.get('effort','')} · "
            f"risk {a.get('risk','')} · conf {a.get('confidence','')}</div>",
            unsafe_allow_html=True,
        )

        ev = a.get("evidence_date")
        age = days_since_check(a)
        bits = []
        if ev and ev != "unknown":
            bits.append(f"evidence {ev}")
        if age is None:
            bits.append("never re-scored")
        elif age >= 7:
            bits.append(f"checked {age}d ago")
        if bits:
            stale = age is None or age >= 7
            colour = "#cc5f43" if stale else "#888"
            st.markdown(
                f"<div class='meta' style='color:{colour}'>{' · '.join(bits)}</div>",
                unsafe_allow_html=True,
            )

        with st.expander("Steps & sources"):
            for s in a.get("steps", []):
                st.markdown(f"- {s}")
            for src in a.get("sources", []):
                st.markdown(f"[{src.get('title', src.get('url'))}]({src.get('url')})")
            st.caption(f"updated {a.get('updated','')}")

        act = st.columns(3)
        if act[0].button("Score chance", key=f"sc_{a['id']}", use_container_width=True):
            ask(f"Re-score the payout chance for the {a['name']} airdrop ({a.get('chain','')}): latest status, snapshot/TGE news, funding and team signals, and anything on X. Give an updated probability.")
            st.rerun()
        in_strat = a["id"] in st.session_state.strategy
        if act[1].button("In strategy ✓" if in_strat else "Add to strategy", key=f"add_{a['id']}", use_container_width=True):
            if in_strat:
                st.session_state.strategy.remove(a["id"])
            else:
                st.session_state.strategy.append(a["id"])
            save_state(); st.rerun()
        if act[2].button("Remove", key=f"rm_{a['id']}", use_container_width=True):
            st.session_state.airdrops = [x for x in st.session_state.airdrops if x["id"] != a["id"]]
            if a["id"] in st.session_state.strategy:
                st.session_state.strategy.remove(a["id"])
            save_state(); st.rerun()

# ----------------------------- LP trigger -----------------------------------
def fmt_usd(v):
    if not isinstance(v, (int, float)):
        return "—"
    for cutoff, suffix, div in ((1e9, "B", 1e9), (1e6, "M", 1e6), (1e3, "k", 1e3)):
        if abs(v) >= cutoff:
            return f"${v / div:,.2f}{suffix}"
    return f"${v:,.0f}"


def scan_age_minutes(state):
    ts = state.get("generated_at")
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 60


def render_lp_hit(h):
    with st.container(border=True):
        top = st.columns([3, 2])
        title = h.get("symbol") or h.get("pool_name") or "unknown"
        badge = " 🆕" if h.get("is_new") else ""
        top[0].markdown(
            f"**{title}**{badge}  \n<span class='meta'>{h.get('token_name') or ''}</span>",
            unsafe_allow_html=True,
        )
        top[1].markdown(
            f"<div style='text-align:right'><span class='chip'>{h.get('dex') or 'dex ?'}</span></div>",
            unsafe_allow_html=True,
        )

        m = st.columns(3)
        m[0].metric("Market cap", fmt_usd(h.get("mcap_usd")))
        m[1].metric("Fees 24h", f"{h.get('fees_eth', 0):.2f} ETH")
        apr = h.get("fee_apr")
        m[2].metric("Pool fee APR", f"{apr:,.0f}%" if isinstance(apr, (int, float)) else "—")

        st.markdown(
            f"<div class='meta'>vol {fmt_usd(h.get('volume_usd'))} · "
            f"TVL {fmt_usd(h.get('reserve_usd'))} · "
            f"fee tier {h.get('fee_rate', 0) * 100:.2f}% ({h.get('fee_source', '?')}) · "
            f"cap via {h.get('mcap_source', '?')}</div>",
            unsafe_allow_html=True,
        )

        safety = []
        for label, key in (("buy tax", "buy_tax"), ("sell tax", "sell_tax")):
            tax = h.get(key)
            if isinstance(tax, (int, float)) and tax > 0:
                safety.append(f"{label} {tax:.1f}%")
        if isinstance(h.get("holder_count"), (int, float)):
            safety.append(f"{int(h['holder_count']):,} holders")
        if safety:
            st.markdown(f"<div class='meta'>{' · '.join(safety)}</div>", unsafe_allow_html=True)

        st.markdown(
            f"<small class='note'>passed {h.get('runs_passed', 1)} scan(s) · "
            f"first seen {h.get('first_seen', '—')}</small>",
            unsafe_allow_html=True,
        )
        if h.get("url"):
            st.markdown(f"[Open chart]({h['url']})")


def render_lp_tab():
    st.subheader("LP Trigger — Robinhood Chain")
    st.caption(
        "Fires on coins whose pool clears both gates. Fees are pool-wide fees over the "
        "window (volume × fee tier), not your share — your cut scales with your share "
        "of the pool's liquidity."
    )

    state = lp_scanner.load_state()
    if not state.get("generated_at"):
        st.info("No scan has run yet. Start the 3-minute trigger, or scan once now.")
    else:
        f = state.get("filters", {})
        age = scan_age_minutes(state)
        head = st.columns(4)
        head[0].metric("Hits", len(state.get("hits", [])))
        head[1].metric("Coins scanned", state.get("scanned", 0))
        head[2].metric("ETH price", fmt_usd(state.get("eth_usd")))
        head[3].metric("Last scan", f"{age:.0f} min ago" if age is not None else "—")
        st.markdown(
            f"<div class='meta'>source {state.get('source', '?')}/{state.get('network', '?')} · "
            f"filter: mcap &gt; {fmt_usd(f.get('min_mcap_usd'))} · "
            f"fees &gt; {f.get('min_fees_eth')} ETH / {f.get('fee_window')} · "
            f"ETH price {state.get('eth_price_source', '?')}</div>",
            unsafe_allow_html=True,
        )
        if age is not None and age > 10:
            st.warning(
                f"Last scan was {age:.0f} minutes ago — the 3-minute job looks stopped. "
                "Check the cron entry or the `--loop` process."
            )
        if any(str(h.get("fee_source", "")).startswith("assumed") for h in state.get("hits", [])):
            st.info(
                "This source does not report pool fee tiers, so fees are volume × an "
                "assumed rate. Guess the tier wrong and every fee figure moves by that "
                "multiple — set `--fee-rate` to match the launchpad you're targeting, "
                "or use the geckoterminal source, which reports the real tier per pool.",
                icon="⚠️",
            )

    with st.form("lp_scan"):
        c = st.columns([2, 2, 1])
        source = c[0].selectbox("Source", sorted(lp_sources.SOURCES),
                                index=sorted(lp_sources.SOURCES).index(
                                    state.get("source") or lp_scanner.DEFAULT_SOURCE))
        eth = c[1].number_input("ETH price (0 = derive/env)", min_value=0.0,
                                value=float(state.get("eth_usd") or 0.0), step=50.0)
        c[2].markdown("<br>", unsafe_allow_html=True)
        if c[2].form_submit_button("Scan now", type="primary", use_container_width=True):
            with st.spinner(f"Scanning Robinhood Chain via {source}…"):
                try:
                    lp_scanner.save_state(
                        lp_scanner.scan(source=source, eth_usd=eth or None))
                    st.rerun()
                except lp_scanner.ScanError as ex:
                    st.error(f"Scan failed: {ex}")

    hits = state.get("hits", [])
    if hits:
        st.divider()
        cols = st.columns(2)
        for i, h in enumerate(hits):
            with cols[i % 2]:
                render_lp_hit(h)
    elif state.get("generated_at"):
        st.caption("No coin cleared the filter in the last scan.")

    history = state.get("history", [])
    if history:
        with st.expander(f"Scan history (last {len(history)} runs)"):
            for run in reversed(history[-20:]):
                names = ", ".join(n for n in run.get("hits", []) if n) or "—"
                st.markdown(
                    f"<div class='meta'>{run.get('at', '')} · "
                    f"{run.get('hit_count', 0)} hit(s) of {run.get('scanned', 0)} · {names}</div>",
                    unsafe_allow_html=True,
                )

    with st.expander("Run the trigger every 3 minutes"):
        picked = state.get("source") or lp_scanner.DEFAULT_SOURCE
        st.markdown("Add this to `crontab -e` (adjust the paths):")
        st.code(
            f"*/3 * * * * cd {os.getcwd()} && "
            f"/usr/bin/python3 lp_scanner.py --source {picked} --quiet "
            ">> lp_scanner.log 2>&1",
            language="bash",
        )
        st.markdown("Or keep it in the foreground without cron:")
        st.code(f"python lp_scanner.py --source {picked} --loop 180", language="bash")
        st.markdown(
            "`gmgn` and `custom` cannot derive the ETH price themselves — set "
            "`ETH_USD`, or `ETH_PRICE_URL` + `ETH_PRICE_PATH` for a live feed."
        )
        st.caption(
            "The scanner writes lp_hits.json; this tab only reads it, so the trigger "
            "keeps running whether or not the dashboard is open."
        )


tab_board, tab_strategy, tab_lp = st.tabs(
    ["Board", f"Strategy ({len(st.session_state.strategy)})", "LP Trigger"]
)

with tab_board:
    for tid, label, note in TIERS:
        items = [a for a in st.session_state.airdrops if a.get("tier") == tid]
        if not items and tid == "watch":
            continue
        st.subheader(label)
        st.caption(note)
        if not items:
            st.caption("Nothing here yet — ask the desk to research this tier.")
        else:
            cols = st.columns(2)
            for i, a in enumerate(items):
                with cols[i % 2]:
                    render_card(a)

with tab_strategy:
    picks = [a for a in st.session_state.airdrops if a["id"] in st.session_state.strategy]
    if not picks:
        st.caption("No plays selected. Add airdrops from the board.")
    else:
        cap = sum(int("".join(filter(str.isdigit, a.get("capital", "0"))) or 0) for a in picks)
        eff = sum(int("".join(filter(str.isdigit, a.get("effort", "0"))) or 0) for a in picks)
        probs = [a["probability"] for a in picks if isinstance(a.get("probability"), (int, float))]
        avg = round(sum(probs) / len(probs)) if probs else None
        m = st.columns(4)
        m[0].metric("Plays", len(picks))
        m[1].metric("Min. capital", f"~${cap}+")
        m[2].metric("Per week", f"~{eff} min")
        if avg is not None:
            m[3].metric("Avg payout chance", f"{avg}%")
        cols = st.columns(2)
        for i, a in enumerate(picks):
            with cols[i % 2]:
                render_card(a)

with tab_lp:
    render_lp_tab()

# ----------------------------- sidebar: backup ------------------------------
with st.sidebar:
    st.header("Backup")
    st.caption("Streamlit Cloud storage is temporary — download a backup so you can restore after a redeploy.")
    st.download_button(
        "Download board (JSON)",
        data=json.dumps({"airdrops": st.session_state.airdrops, "strategy": st.session_state.strategy}, indent=2),
        file_name="airdrops_backup.json", mime="application/json", use_container_width=True,
    )
    up = st.file_uploader("Restore from backup", type="json")
    if up is not None:
        try:
            data = json.load(up)
            st.session_state.airdrops = data.get("airdrops", st.session_state.airdrops)
            st.session_state.strategy = data.get("strategy", st.session_state.strategy)
            save_state()
            st.success("Restored.")
        except Exception as ex:
            st.error(f"Bad file: {ex}")
    st.divider()
    st.caption("Model estimates from public evidence, not calibrated odds. Not financial advice — deploy only capital you can afford to lose.")
