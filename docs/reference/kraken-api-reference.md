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
- REST API: `"XXBTZUSD"`, `"XETHZUSD"`, `"ADAUSD"` (no separator)
- WebSocket API: `"XBT/USD"`, `"ETH/USD"` (slash separator)
- Altnames: `"BTCUSD"`, `"ETHUSD"` (short form)

Assets have prefix notation:
- `X` prefix for crypto: `XXBT` (BTC), `XETH` (ETH)
- `Z` prefix for fiat: `ZUSD` (USD), `ZEUR` (EUR)
- Exceptions: `"DASH"`, `"GNO"` (4-char assets, no prefix)

**Decision:** Our `Symbol` value object correctly uses `base/quote` with `to_kraken_format()` method. Keep current design.

### 3. **Amounts and Prices: Decimal Strings**
Kraken returns all monetary values as **decimal strings**, not floats:
- Prices: `"50000.50000"`
- Volumes: `"0.12345678"`
- Fees: `"5.10"`
- Costs: `"6000.60"`

Precision varies by asset:
- BTC: 8 decimal places
- USD: 2-4 decimal places
- Get from `/0/public/Assets` and `/0/public/AssetPairs` endpoints

**Decision:** Use Python `Decimal` type internally. Our `Price` and `Amount` value objects correctly use `Decimal`. Good.

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
| **Symbol** | `"XXBTZUSD"` (concatenated) | `Symbol(base, quote)` with `to_kraken_format()` |

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
