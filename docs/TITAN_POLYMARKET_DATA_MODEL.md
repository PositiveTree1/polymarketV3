# Titan / Polymarket Data Model

This document explains, using the current Titan codebase and recent probes, what Titan loads from Polymarket, how Titan maps that data into its own structures, and which fields are the real source of truth.

The most important conclusion is simple:

- The frontend Polymarket URL uses `event.slug`
- In Titan, that value must live in `Market.event_slug`
- `Market.slug` is not the same thing


## 1. Core Polymarket hierarchy

Polymarket is effectively:

1. `Event`
2. `Market`
3. `Outcome token / asset`

Titan terminology matches this only partially:

- `Event` is not a first-class Titan object today
- `Market` is a Titan dataclass, not a raw Polymarket type
- `asset` is the token ID Titan uses to identify a specific side of a binary market
- `cid` is the market condition ID

Useful identity chain:

- `asset` -> Gamma `market`
- `market` -> parent `event`
- `event.slug` -> frontend URL path


## 2. What Titan loads from Polymarket

Titan currently loads data from two Polymarket APIs:

- Data API
- Gamma API

### Data API calls used by Titan

#### `/positions`

Used for wallet and position information.

Current uses:

- wallet scoring
- wallet open-position analysis
- startup understanding of holdings

Typical fields Titan reads:

- `conditionId`
- `asset`
- `outcome`
- `curPrice`
- `size`
- `initialValue`
- `currentValue`
- `cashPnl`
- `redeemable`

Important rule:

- `/positions` is not a full market object
- it does not replace Gamma market metadata

#### `/trades`

Used for whale/public trade ingestion and several recovery paths.

Current uses:

- main trade feed
- elite/VIP wallet polling
- watchlist polling
- public discovery feed
- sell detection
- bootstrap recovery for market lookup by `conditionId`

Typical fields Titan reads:

- `conditionId`
- `asset`
- `slug`
- `eventSlug`
- `title`
- `outcome`
- `price`
- `size`
- `timestamp`
- `proxyWallet`

Important rule:

- trade payloads are often the first place Titan sees `eventSlug`
- that makes trade metadata a strong fallback source for URL identity

### Gamma API calls used by Titan

#### `/markets`

This is Titan's main market metadata source.

Current lookup strategies:

1. by `clob_token_ids` using `asset`
2. by `slug`
3. by `conditionId` only indirectly through Data API `/trades` bootstrap

Typical fields Titan reads:

- `conditionId`
- `slug`
- `eventSlug`
- `event.slug`
- `question`
- `liquidity`
- `volume`
- `outcomePrices`
- `outcomes`
- `clobTokenIds`
- `endDate`
- `active`
- `closed`

Important observed rule:

- direct Gamma lookup by `conditionId` is unreliable in this codebase context
- lookup by `asset` via `clob_token_ids` is the most reliable path

#### `/events`

Titan does not currently use `/events` directly in production code.

But based on the information gathered, this is the canonical event-level source:

- `GET /events`
- `GET /events?slug=...`
- `GET /events/slug/{slug}`

Important rule:

- the frontend URL uses `event.slug`
- if Titan ever adds explicit event loading, `/events` should be treated as the event-level source of truth


## 3. Titan structures

### `WhaleObservation`

Defined in `ScriptsTitan/titan_market.py`.

This is Titan's normalized view of a Polymarket trade from the whale/public feed.

Key fields:

- `wallet`
- `name`
- `cid`
- `asset`
- `slug`
- `event_slug`
- `title`
- `outcome`
- `price`
- `size`
- `cash`
- `ts`
- `source`

Source mapping:

- built from Data API `/trades`
- populated in `_normalise_trade()`

Important rule:

- `WhaleObservation.event_slug` comes from trade `eventSlug`
- this is often the earliest URL-quality identifier Titan sees

### `Market`

Defined in `ScriptsTitan/titan_market.py`.

This is a Titan dataclass, not a raw Polymarket schema.

Key fields:

- `yes_price`
- `no_price`
- `outcome_labels`
- `outcome_prices`
- `token_index`
- `index_to_price`
- `asset_to_price`
- `asset_to_index`
- `liq`
- `volume`
- `title`
- `end_date`
- `hrs_left`
- `slug`
- `event_slug`
- `mkt_type`
- `is_sports`
- `ts`

Important rules:

- `Market.slug` is market identity
- `Market.event_slug` is event identity
- `Market.polymarket_url()` prefers `event_slug`, then `slug`

### `Signal`

Defined in `ScriptsTitan/titan_signals.py`.

Signal carries both:

- a `Market` object in `sig.mkt`
- fallback signal-side identity fields `_slug` and `_event_slug`

Important rule:

- `Signal.event_slug` returns `sig.mkt.event_slug or sig._event_slug`
- this is why signal payloads could appear correct even when persisted markets were blank

### `TradeRecord`

Defined in `ScriptsTitan/titan_trade.py`.

This is Titan's own local trade history object, not a Polymarket feed trade.

Key identity fields:

- `cid`
- `asset`
- `title`
- `slug`
- `event_slug`
- `market_url`

Important rule:

- `TradeRecord` stores the final Titan-side identity used for audit and replay

### `Position`

Defined in `ScriptsTitan/titan_position.py`.

`Position` resolves `slug` and `event_slug` from:

1. buy trade
2. sell trade
3. audit snapshots

Important rule:

- position identity is trade-driven first, not market-driven first


## 4. MarketCache

`MarketCache` lives in `ScriptsTitan/titan_markets.py`.

This is now the centralized Titan source of truth for `Market`.

Responsibilities:

- load persisted markets from DB at startup
- return cached markets when available
- call Polymarket when necessary
- persist updated markets when they are truly used
- merge identity hints from signals, trades, and positions

Current design rule:

- Titan does not persist every market it reads from Polymarket
- a market is stored only if it is actually used by a signal, a position, or a trade flow

Current identity merge rule:

- if market fetch returns blank `event_slug`
- and Titan already knows `event_slug` from trade or position metadata
- `MarketCache` injects that hint into the canonical cached `Market`


## 5. Startup flow

Current startup behavior, based on probes:

1. `load_state()`
2. restore local DB / JSON state
3. main loop starts
4. first Polymarket calls are wallet `/positions`
5. then wallet `/trades`
6. market lookups happen later only when needed

Important rule:

- the first external call on startup is usually not a market lookup
- it is usually a Data API wallet positions request


## 6. How Titan gets trades

There are two different meanings of "trade" in Titan.

### A. External Polymarket feed trades

These come from Data API `/trades`.

Main sources:

- elite/VIP polling
- watchlist polling
- public feed polling
- HFT spike polling

These become `WhaleObservation`.

### B. Titan's own executed trades

These are local `TradeRecord` rows in `trade_history`.

Important rule:

- do not confuse feed trades with Titan's own buy/sell history


## 7. URL identity: `slug` vs `event.slug`

This is the most important section.

### What the frontend uses

Polymarket frontend URLs use:

- `https://polymarket.com/event/{event.slug}`

Not:

- market slug
- condition ID
- asset

### What that means in Titan

Titan should treat:

- `Market.event_slug` as the canonical frontend URL key

and treat:

- `Market.slug` as useful market metadata, but not the primary URL identity

### Correct priority order for Titan URL construction

When building a Polymarket link, Titan should prefer:

1. trade `eventSlug`
2. Gamma market `event.slug`
3. Gamma market `eventSlug`
4. explicit Gamma `/events` lookup if added later
5. market `slug` only as a last fallback

### Why this matters

A single event can contain multiple markets.

So:

- market slug identifies a market
- event slug identifies the page URL

Even when both look similar, they are not guaranteed to be the same thing.


## 8. Current proven lookup behavior

Based on recent probes:

### Asset -> market -> event slug works

Using:

- `GET /markets?clob_token_ids=<asset>`

Titan was able to recover:

- `market.slug`
- `event.slug`

Example observed:

- market slug: `will-the-democratic-party-control-the-house-after-the-2026-midterm-elections`
- event slug: `which-party-will-win-the-house-in-2026`

Example observed:

- market slug: `will-mercedes-be-the-2026-f1-constructors-champion`
- event slug: `f1-constructors-champion`

Important rule:

- `asset` is a strong starting point for recovering the real frontend URL slug

### Condition ID lookup is weaker

Observed behavior in this codebase:

- direct Gamma lookup by `conditionId` is unreliable
- Titan should prefer `asset` and `slug` over `conditionId` for market recovery


## 9. Current production rules inside Titan

### Market loading

Current effective recovery order:

1. cache / DB
2. Gamma `/markets` by `clob_token_ids` using `asset`
3. Gamma `/markets` by `slug`
4. Data API `/trades` bootstrap by `conditionId`

### Event slug recovery

Current effective recovery order:

1. trade `eventSlug`
2. Gamma market `event.slug`
3. Gamma market `eventSlug`
4. cached signal/position identity hints
5. fallback to `slug` only if nothing else exists

### Persistence

Markets are stored only when they are materially used by:

- a created signal
- an open position
- a trade / trader path


## 10. Known gaps

These are still true today:

- Titan does not have a first-class `Event` dataclass
- Titan does not call Gamma `/events` directly in production code
- some old persisted `markets` rows may still have blank `event_slug`
- market rows can exist without recent matching `trade_history`

Practical implication:

- `MarketCache` must keep enriching markets from trade/position identity hints
- adding explicit `/events` fallback would make event identity even cleaner


## 11. Practical guidance

If you need a clickable Polymarket URL in Titan:

1. prefer `market.event_slug`
2. if blank, use trade or signal `event_slug`
3. if still blank, recover via `asset -> Gamma /markets -> event.slug`
4. only use `market.slug` as a last fallback

Correct final form:

```text
https://polymarket.com/event/<event_slug>
```

Not:

```text
https://polymarket.com/event/<market_slug>
```

unless there is no event slug available at all.


## 12. Summary

The clean mental model is:

- Data API `/trades` gives Titan trade identity early, including `eventSlug`
- Gamma `/markets` gives Titan market structure and often nested event data
- `MarketCache` is Titan's central source of truth for `Market`
- `event.slug` is the canonical frontend URL identity
- `asset` is the strongest technical key for recovering a market and its parent event

