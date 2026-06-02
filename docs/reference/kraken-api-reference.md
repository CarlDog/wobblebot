# Kraken API Reference for WobbleBot

> **Historical design notes — partially superseded by ADR-005.**
>
> This document captures the pre-ADR-005 research and deliberation that
> led to the current domain model. Sections that read like
> recommendations ("Mismatch identified", "Recommendation: status
> mapping layer", "Our Order model currently uses…", "Current Model")
> describe the *old* model state — they were resolved by adopting
> Kraken's canonical vocabulary directly in `src/wobblebot/domain/`.
>
> **No mapping layer exists or is needed.** The Order status `Literal`
> is exactly Kraken's: `pending | open | closed | canceled | expired`
> (American "canceled"). `Trade.id` and `Trade.order_id` are plain
> Kraken txid strings, not UUIDs. `Order` uses the dual-ID strategy
> recommended in section "Architectural Recommendations → 1".
>
> The Kraken API descriptions themselves (request/response shapes,
> field semantics, precision rules) remain accurate and useful as a
> reference when implementing the Kraken adapter in Phase 2.

> **Live verification:** 2026-05-14. Public endpoints
> (`SystemStatus`, `AssetPairs`, `Ticker`, `Assets`) re-fetched against
> `api.kraken.com` and cross-checked against the shapes below. Private
> endpoints (`Balance`, `OpenOrders`, `TradesHistory`, `AddOrder`,
> `Withdraw`) are not live-verified — claims about them come from
> Kraken's documentation and have not changed in the intervening
> period to public knowledge. The integration test
> `tests/integration/test_kraken_api_health.py` re-checks public
> shapes on demand.

**Purpose:** Domain model design decisions based on Kraken REST API v0 data structures and field naming conventions.

**Sources:**
- [Kraken REST API Docs](https://docs.kraken.com/rest/)
- [krakenex Python client](https://github.com/veox/python3-krakenex) (LGPLv3, used for reference only)
- [Kraken Support - API Articles](https://support.kraken.com/hc/en-us/sections/200973757-trading-via-the-api)

---

## Key Design Principles from Kraken API

### 1. **ID Fields: String, Not UUID**
Kraken uses **string IDs** for all resources:
- Orders: `txid` (transaction ID) — string like `"OG5V2Y-RYKVL-DT3V3B"`
- Trades: `txid` — string
- User Reference: `userref` — optional user-defined integer reference
- Positions: Position ID strings

**Decision:** Domain models should use **`str` for IDs**, not `UUID`. Our internal `id: UUID` can coexist with `exchange_id: str`.

### 2. **Symbol Format: Pair Strings**
Kraken symbols are concatenated strings:
- REST API key form: `"XXBTZUSD"`, `"XETHZUSD"`, `"XDGUSD"` (no separator)
- WebSocket name (`wsname`): `"XBT/USD"`, `"ETH/USD"`, `"XDG/USD"` (slash separator)
- Altname: `"XBTUSD"`, `"ETHUSD"`, `"XDGUSD"` (short form, no separator)

Assets have prefix notation:
- `X` prefix for crypto: `XXBT` (BTC), `XETH` (ETH), `XXDG` (DOGE)
- `Z` prefix for fiat: `ZUSD` (USD), `ZEUR` (EUR)
- Newer/added assets often drop the prefix: `ADA`, `SOL`, `MATIC`, `DASH`, `GNO`

**Asset-code aliasing gotchas.** Some popular asset codes don't match
their colloquial ticker symbol:

| Colloquial | Kraken asset code | Pair key | Altname / wsname base |
|------------|-------------------|----------|------------------------|
| BTC        | `XXBT`            | `XXBTZUSD` | `XBT` |
| DOGE       | `XXDG`            | `XDGUSD`   | `XDG` |
| (others)   | mostly identity   | varies     | identity              |

The DOGE/XDG asymmetry matters: our config (`config/settings.example.yml`)
lists `DOGE` as a grid coin. The `Symbol → Kraken` translator needs a
small alias table or it'll request a non-existent `DOGEUSD` pair.
Build the alias table from `/0/public/AssetPairs` at startup rather
than hard-coding it — Kraken adds/removes pairs over time, and a
runtime lookup catches drift without code changes.

**Decision:** Our `Symbol` value object uses `base/quote` (e.g. `BTC/USD`). Kraken-format translation lives in the adapter, not the domain — `KrakenAdapter` has a small conventional dict (`BTC↔XBT`, `DOGE↔XDG`) plus a startup-populated `Assets`-endpoint cache that maps Kraken's legacy response codes (XXBT, ZUSD) to altnames (XBT, USD). A `Symbol.to_kraken_format()` method previously lived on the domain object and was removed in Stage 2.1 slice 2.5 — it violated hex-layer rules and was broken anyway (`Symbol("BTC","USD").to_kraken_format()` returned `"BTCUSD"`, which Kraken rejects; the correct altname is `"XBTUSD"`).

### 3. **Amounts and Prices: Decimal Strings**
Kraken returns all monetary values as **decimal strings**, not floats:
- Prices: `"79035.20000"`
- Volumes: `"0.12345678"`
- Fees: `"5.10"`
- Costs: `"6000.60"`

Precision is communicated by two separate field families. Don't conflate them:

**Per-asset (from `/0/public/Assets`):**
- `decimals` — internal precision the exchange tracks (e.g. `XXBT: 10`, `ZUSD: 4`).
- `display_decimals` — precision used in the Kraken web UI (e.g. `XXBT: 5`, `ZUSD: 2`).

**Per-pair (from `/0/public/AssetPairs`):**
- `pair_decimals` — price quote precision for this pair (e.g. `XXBTZUSD: 1`).
- `lot_decimals` — volume precision (e.g. `XXBTZUSD: 8`).
- `cost_decimals` — cost-side precision = price × volume (e.g. `XXBTZUSD: 5`).

For trading, **`pair_decimals` and `lot_decimals` are load-bearing** —
submitting a price or volume with extra precision is rejected. Read
them once at startup along with the alias table.

**Decision:** Use Python `Decimal` type internally. Our `Price` and `Amount` value objects correctly use `Decimal`. Good. Phase 2.3 will add per-pair precision quantization in `KrakenAdapter.place_order` using `lot_decimals` / `pair_decimals` from a startup `AssetPairs` snapshot.

---

## Public Market Data

### **Response: Ticker** (`/0/public/Ticker?pair=...`)

```json
{
    "error": [],
    "result": {
        "XXBTZUSD": {
            "a": ["79035.20000", "1", "1.000"],
            "b": ["79035.10000", "1", "1.000"],
            "c": ["79033.80000", "0.00156715"],
            "v": ["98.21298882", "1602.79390704"],
            "p": ["79448.11928", "79717.97007"],
            "t": [5634, 51349],
            "l": ["78995.20000", "78720.90000"],
            "h": ["79664.90000", "81277.00000"],
            "o": "79292.10000"
        }
    }
}
```

**Field semantics:**

| Key | Shape | Meaning |
|-----|-------|---------|
| `a` | `[price, whole_lot_volume, lot_volume]` | Best ask |
| `b` | `[price, whole_lot_volume, lot_volume]` | Best bid |
| `c` | `[price, lot_volume]` | **Last trade closed** — `c[0]` is the canonical "current price" |
| `v` | `[today, last_24h]` | Volume |
| `p` | `[today, last_24h]` | Volume-weighted average price |
| `t` | `[today, last_24h]` | Number of trades |
| `l` | `[today, last_24h]` | Low price |
| `h` | `[today, last_24h]` | High price |
| `o` | `string` | Today's opening price |

**For our adapter's `get_current_price`: use `result[<pair_key>]["c"][0]`.**
The bid/ask spread (`a[0]` and `b[0]`) is more useful for execution
decisions, but Stage 2.1 only needs a mid-price-ish single number;
last-trade is the standard pick.

### **Response: AssetPairs** (`/0/public/AssetPairs`)

Returns metadata for every tradable pair. The fields we care about:

| Field | Type | Used for |
|-------|------|----------|
| `altname` | string | Short pair name (e.g. `XBTUSD`) — useful for log messages |
| `wsname` | string | WebSocket-format name (e.g. `XBT/USD`) — Phase 2+ if WS lands |
| `base` | string | Base asset code (e.g. `XXBT`) — drives the alias table |
| `quote` | string | Quote asset code (e.g. `ZUSD`) |
| `pair_decimals` | int | Price precision for order placement |
| `lot_decimals` | int | Volume precision for order placement |
| `cost_decimals` | int | Cost-side precision (less load-bearing) |
| `ordermin` | string | Minimum order volume |
| `costmin` | string | Minimum order cost |
| `tick_size` | string | Smallest price increment |
| `status` | string | `"online"`, `"cancel_only"`, `"post_only"`, etc. — gate before placing |

The response key (e.g. `"XXBTZUSD"`) is the canonical pair identifier
used in `Ticker`, `OpenOrders`, `AddOrder`, and elsewhere. `altname`
and `wsname` are alternative representations of the same pair —
useful for display, not for API requests.

---

## Order Structure (AddOrder / OpenOrders / QueryOrders)

### **Request: AddOrder**
```python
{
    "pair": "XXBTZUSD",           # Required: asset pair
    "type": "buy" | "sell",       # Required: order side
    "ordertype": "limit" | "market" | "stop-loss" | "take-profit" | ...,
    "price": "50000.00",          # Required for limit orders
    "volume": "0.1",              # Required: order quantity
    "userref": 123456,            # Optional: user reference ID (int32)
    "oflags": "post,fciq",        # Optional: order flags
    "starttm": "0",               # Optional: scheduled start time
    "expiretm": "0",              # Optional: expiration time
    "validate": false             # Optional: validate only, don't submit
}
```

### **Response: AddOrder**
```json
{
    "error": [],
    "result": {
        "descr": {
            "order": "buy 0.1 XXBTZUSD @ limit 50000.00"
        },
        "txid": ["OG5V2Y-RYKVL-DT3V3B"]  # Array of order IDs (usually single)
    }
}
```

### **Response: OpenOrders / QueryOrders**
```json
{
    "error": [],
    "result": {
        "open": {
            "OG5V2Y-RYKVL-DT3V3B": {
                "refid": null,
                "userref": 0,
                "status": "open",
                "opentm": 1688712345.6789,    # Unix timestamp (float)
                "starttm": 0,
                "expiretm": 0,
                "descr": {
                    "pair": "XXBTZUSD",
                    "type": "buy",
                    "ordertype": "limit",
                    "price": "50000.00",
                    "price2": "0",
                    "leverage": "none",
                    "order": "buy 0.1 XXBTZUSD @ limit 50000.00",
                    "close": ""
                },
                "vol": "0.10000000",           # Original order volume
                "vol_exec": "0.00000000",      # Executed volume
                "cost": "0.00000",             # Total cost (quote currency)
                "fee": "0.00000",              # Total fee
                "price": "0.00000",            # Average fill price
                "stopprice": "0.00000",
                "limitprice": "0.00000",
                "misc": "",
                "oflags": "fciq"
            }
        },
        "count": 1
    }
}
```

### **Order Status Values**
From Kraken API observations:
- `"pending"` — Order submitted, not yet on exchange (rare, internal state)
- `"open"` — Order on order book, not filled
- `"closed"` — Order fully filled
- `"canceled"` — Order cancelled (note: Kraken uses "canceled", not "cancelled")
- `"expired"` — Order expired (time-based)

**Resolved by ADR-005:** the domain model adopts Kraken's exact values:
```python
status: Literal["pending", "open", "closed", "canceled", "expired"]
```
No mapping layer in the adapter — Kraken's strings flow through unchanged.

---

## Trade Structure (TradesHistory)

### **Response: TradesHistory**
```json
{
    "error": [],
    "result": {
        "trades": {
            "TJKLMN-OPQRS-TUVWXY": {
                "ordertxid": "OG5V2Y-RYKVL-DT3V3B",  # Parent order ID
                "postxid": "TKH2SE-M7IF5-CFI7LT",    # Position ID (if margin)
                "pair": "XXBTZUSD",
                "time": 1688712400.1234,              # Unix timestamp (float)
                "type": "buy",
                "ordertype": "limit",
                "price": "50010.50000",
                "cost": "5001.05000",                 # Total cost (price * vol)
                "fee": "13.00273",                    # Fee charged
                "vol": "0.10000000",                  # Trade volume
                "margin": "0.00000",
                "misc": ""
            }
        },
        "count": 1
    }
}
```

**Key Fields:**
- `ordertxid`: String reference to parent order
- `pair`: Symbol string
- `time`: Unix timestamp as float (seconds since epoch)
- `price`, `cost`, `fee`, `vol`: Decimal strings
- No separate `trade_id` field — **trades are keyed by their own txid in the response object**

**Decision:** Our `Trade` model uses `trade_id`, `order_id` (both should be strings), and `fee: Amount`.

**Mismatch:** Kraken returns `fee` as decimal string (just the number), not structured `{amount, asset}`. Fee asset is implicit (quote currency for most trades).

**Recommendation:**
```python
class Trade(BaseModel):
    id: str                # Kraken txid (not UUID!)
    order_id: str          # Kraken ordertxid (not UUID!)
    symbol: Symbol
    side: OrderSide
    price: Price
    amount: Amount         # Kraken "vol"
    fee: Decimal           # Kraken "fee" (decimal string) - fee currency is quote
    executed_at: Timestamp # Kraken "time"
    cost: Decimal          # Kraken "cost" (price * vol)
```

---

## Balance Structure (Balance endpoint)

### **Response: Balance**
```json
{
    "error": [],
    "result": {
        "XXBT": "1.50000000",
        "ZUSD": "50000.00",
        "XETH": "10.00000000"
    }
}
```

**Simple Structure:**
- Keys: Asset codes (with X/Z prefixes)
- Values: Decimal strings (total balance)
- **No distinction between `available` and `locked`** in Balance endpoint
- To get locked funds, must query `OpenOrders` and calculate

**TradeBalance endpoint** provides more detail:
```json
{
    "result": {
        "eb": "50000.0000",   # Equivalent balance (USD)
        "tb": "51000.0000",   # Trade balance
        "m": "0.0000",        # Margin amount
        "n": "0.0000",        # Net
        "c": "0.0000",        # Cost basis
        "v": "0.0000",        # Floating valuation
        "e": "51000.0000",    # Equity
        "mf": "51000.0000"    # Free margin
    }
}
```

**Decision:** Our `Balance` model with `total`, `available`, `locked` fields is **NOT** directly provided by Kraken. We must **calculate** locked amounts by:
1. Get `Balance` endpoint → `total`
2. Get `OpenOrders` → sum up order volumes → `locked`
3. Compute `available = total - locked`

This is acceptable — it's an adapter responsibility, not a domain model issue.

---

## Withdrawal Structure (Withdraw endpoint)

### **Request: Withdraw**
```python
{
    "asset": "XXBT",
    "key": "btc_withdrawal_key",  # Pre-configured withdrawal address name
    "amount": "0.5"
}
```

### **Response: Withdraw**
```json
{
    "error": [],
    "result": {
        "refid": "AGBJQ7N-L3RJUZ-VHMN3I"  # Withdrawal reference ID
    }
}
```

### **Withdrawal Status (WithdrawStatus)**
```json
{
    "result": [
        {
            "method": "Bitcoin",
            "aclass": "currency",
            "asset": "XXBT",
            "refid": "AGBJQ7N-L3RJUZ-VHMN3I",
            "txid": "...",                    # Blockchain txid (once processed)
            "info": "bc1q...",                # Withdrawal address
            "amount": "0.50000000",
            "fee": "0.00010000",
            "time": 1688712500,
            "status": "Success" | "Pending" | "Settled" | "Failure" | "Cancelled"
        }
    ]
}
```

**Key Insight:** Withdrawal uses **pre-configured keys** (address book), not arbitrary addresses. This is a Kraken security feature.

**Decision:** Our `HarvesterPort.execute_transfer()` should accept:
```python
async def execute_transfer(
    self,
    direction: Literal["deposit", "withdrawal"],
    asset: str,
    amount: Decimal,
    withdrawal_key: str | None = None,  # Required for withdrawals
) -> TransferResult:
    ...
```

---

## Dead Man's Switch (CancelAllOrdersAfter endpoint)

Server-side safety timer used by the engine's dead man's switch (ADR-021,
`ExchangePort.set_dead_mans_switch`). Each call (re)starts a countdown **on Kraken's
servers**; if no further call arrives within `timeout` seconds, Kraken cancels every
open order on the account itself — so it fires even when our host has lost power or
network. `timeout: 0` disables it. Permission: "Create & modify orders" OR "Cancel &
close orders" (NOT Withdraw). Kraken's recommended pattern is a 60s timeout pinged every
15–30s; `cli/live` pings every tick (≈5s).

### **Request: CancelAllOrdersAfter** (`POST /0/private/CancelAllOrdersAfter`)
```python
{
    "timeout": "60"  # seconds until auto-cancel if not reset; "0" disables
}
```

### **Response: CancelAllOrdersAfter**
```json
{
    "error": [],
    "result": {
        "currentTime": "2026-06-01T00:00:00Z",  # server time the request was handled
        "triggerTime": "2026-06-01T00:01:00Z"   # when all orders cancel if not reset
    }
}
```

**Key Insight:** the timer is **account-wide** — it cancels manually-placed orders on the
same account too, not just the bot's. The empty `error` array is the success signal (the
adapter ignores the `result` body and relies on the standard envelope check).

---

## Field Naming Conventions Summary

Reflects the post-ADR-005 domain model. The ⚠️ rows in the original
version of this table were all subsequently aligned with Kraken's
vocabulary.

| Concept | Kraken API | Domain Model |
|---------|-----------|--------------|
| **Order ID** | `txid` (string) | `id: UUID`, `exchange_id: str \| None` (dual ID) |
| **Order status** | `pending`/`open`/`closed`/`canceled`/`expired` | Same Literal, verbatim |
| **Order volume** | `vol` (string), `vol_exec` (string) | `amount: Amount`, `filled_amount: Decimal` |
| **Order price** | `price` (string, limit price) | `price: Price` |
| **Order side** | `type: "buy"` or `"sell"` | `side: OrderSide` with `"buy"` or `"sell"` |
| **Trade ID** | `txid` (string, response key) | `id: str` (Kraken txid) |
| **Trade order ref** | `ordertxid` (string) | `order_id: str` (Kraken parent txid) |
| **Trade fee** | `fee` (decimal string, quote currency) | `fee: Decimal` |
| **Trade volume** | `vol` (decimal string) | `amount: Amount` |
| **Balance** | `{"XXBT": "1.5"}` (flat dict) | `Balance(asset, total, available, locked)` — adapter calculates `locked` from open-order set |
| **Timestamp** | Unix float (seconds.microseconds) | `Timestamp(dt: datetime)` — adapter converts via `to_unix_seconds()` |
| **Symbol** | `"XXBTZUSD"` (concatenated) | `Symbol(base, quote)`; Kraken-format translation in `KrakenAdapter` (`_symbol_to_kraken_altname` + dynamic Assets cache) |

---

## Architectural Recommendations

### **1. ID Strategy Decision**

**Option A: UUID for Internal, String for Exchange (RECOMMENDED)**
```python
class Order(BaseModel):
    id: UUID = Field(default_factory=uuid4)  # Internal tracking
    exchange_id: str | None                   # Kraken txid
    ...
```

**Pros:**
- Clean separation of concerns
- Database-friendly (UUID primary keys)
- Exchange-agnostic (can support multiple exchanges)

**Cons:**
- Two ID fields per entity

**Option B: String IDs Throughout**
```python
class Order(BaseModel):
    id: str                                  # Kraken txid (or generated)
    ...
```

**Pros:**
- Simpler model (single ID field)
- Direct Kraken compatibility

**Cons:**
- ID format varies by exchange
- Less database-friendly
- Harder to generate IDs before submission

**Verdict:** **Option A** — Keep `id: UUID` for internal tracking, `exchange_id: str` for Kraken txid.

### **2. Order Status Alignment**

**Kraken API status vocabulary:**
```python
status: "pending" | "open" | "closed" | "canceled" | "expired"
```

**Resolved by ADR-005:** the domain model adopts these values verbatim
(`src/wobblebot/domain/models.py:48`):
```python
status: Literal["pending", "open", "closed", "canceled", "expired"]
```

No mapping table. No translation in the adapter. Kraken's terminology
*is* the domain terminology. (The alternative — keeping a mapping
layer for "future flexibility" — was rejected as premature abstraction
since spot trading on Kraken is the Phase 1-5 target.)

### **3. Trade Fee Structure**

**Current Model:**
```python
fee: Amount  # {value: Decimal, asset: str}
```

**Kraken API:**
```python
"fee": "13.00273"  # Decimal string, asset implicit (quote currency)
```

**Recommendation:** Simplify to match Kraken:
```python
fee: Decimal  # Fee amount in quote currency
```

Asset is deterministic: always the quote currency of the trading pair.

### **4. Position Model (Not Used Yet)**

Kraken positions are **margin-specific**. For Phase 1-2 (spot trading only), we don't need `Position` model.

**Recommendation:** Defer Position model to Phase 3+ when margin trading is considered. For now, track P&L via completed trades.

---

## Timestamp Handling

Kraken uses **Unix timestamps as floats**:
- Precision: Seconds with microsecond decimal part
- Example: `1688712345.123456`

**Conversion:**
```python
# Kraken timestamp → Python datetime
dt = datetime.fromtimestamp(kraken_time, tz=timezone.utc)

# Python datetime → Kraken timestamp
kraken_time = dt.timestamp()  # Returns float

# Our Timestamp.to_unix_ms() returns int milliseconds
# Kraken expects float seconds
kraken_time = timestamp.dt.timestamp()  # Float seconds
```

**Recommendation:** Add `Timestamp.to_unix_seconds()` method:
```python
class Timestamp(BaseModel):
    ...
    def to_unix_seconds(self) -> float:
        """Convert to Unix timestamp in seconds (Kraken format)."""
        return self.dt.timestamp()
```

---

## Next Steps: Domain Model Refactoring

Based on Kraken API realities, here are the recommended changes:

### **Phase 1.2 Immediate Changes (Before Tests)**

1. **Order Status Values:**
   ```python
   status: Literal["pending", "open", "closed", "canceled", "expired"]
   ```

2. **Trade Model:**
   ```python
   class Trade(BaseModel):
       id: str  # Kraken txid
       order_id: str  # Kraken ordertxid
       symbol: Symbol
       side: OrderSide
       price: Price
       amount: Amount  # Kraken "vol"
       fee: Decimal  # Kraken "fee" (quote currency)
       cost: Decimal  # Kraken "cost" (price * vol)
       executed_at: Timestamp

       class Config:
           frozen = True
   ```

3. **Order Model:**
   ```python
   class Order(BaseModel):
       id: UUID = Field(default_factory=uuid4)
       exchange_id: str | None = None  # Kraken txid
       symbol: Symbol
       side: OrderSide
       price: Price
       amount: Amount
       status: Literal["pending", "open", "closed", "canceled", "expired"] = "pending"
       created_at: Timestamp
       updated_at: Timestamp | None = None  # Make optional, set on updates
       filled_amount: Decimal = Field(default=Decimal("0"), ge=0)
   ```

4. **Timestamp Extensions:**
   ```python
   def to_unix_seconds(self) -> float:
       """For Kraken API compatibility."""
       return self.dt.timestamp()
   ```

---

## Summary — what shipped

All six items below were adopted into the Phase 1.2 domain model and
codified in ADR-005:

1. **IDs:** dual-ID for `Order` (`id: UUID` internal + `exchange_id: str | None` Kraken txid); plain `id: str` for `Trade` and `Trade.order_id` (Kraken txids).
2. **Status values:** Kraken's canonical Literal verbatim — `pending | open | closed | canceled | expired`. No mapping layer.
3. **Amounts:** `Decimal` for most fields; `Amount` value object when asset context is needed.
4. **Trade fees:** `Decimal` in quote currency (simplified from `Amount`).
5. **Timestamps:** `Timestamp.to_unix_seconds()` provides the Kraken-compatible float-seconds format.
6. **Position model:** deferred to Phase 3+ (margin trading); spot trading does not need it.
