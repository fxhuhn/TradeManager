# TradeManager: Low-Level Technical Specification

This document details the database schemas, CSV interface layouts, internal data structures, state machine transitions, and error management rules.

---

## 1. Database Schema

All database models reside in the local SQLite database at `data/trading.db`, operating in **WAL mode** with foreign key constraint checks enabled (`PRAGMA foreign_keys = ON`).

### 1.1 Schema Version Table (`schema_version`)
Stores schema migrations history.

| Variable Name | Data Type | Validation Rules | Description |
| :--- | :--- | :--- | :--- |
| `version` | `INTEGER` | `PRIMARY KEY` | Incremental integer representing migration level. |
| `applied_at` | `TIMESTAMP` | `DEFAULT CURRENT_TIMESTAMP` | Date and time when the schema migration was applied. |

### 1.2 Orders Table (`orders`)
Records intended and submitted orders.

| Variable Name | Data Type | Validation Rules | Description |
| :--- | :--- | :--- | :--- |
| `order_id` | `INTEGER` | `PRIMARY KEY` | TWS-assigned order ID (negative integers represent local temp IDs). |
| `perm_id` | `INTEGER` | `NULLABLE`, `UNIQUE` (partial index) | Permanent unique TWS ID once the order executes or gets confirmed. |
| `parent_id` | `INTEGER` | `NULLABLE`, `FOREIGN KEY` | Reference to parent `order_id` (`ENTRY` order) with cascade updates. |
| `trade_group_id` | `TEXT` | `NOT NULL` | Group key connecting bracket orders (e.g. entry, target, stop). |
| `account_id` | `TEXT` | `NOT NULL` | The target Interactive Brokers brokerage account. |
| `bracket_role` | `TEXT` | `CHECK IN ('ENTRY', 'SL', 'TP', 'EXIT')` | Structural role of the order in the trade group bracket. |
| `symbol` | `TEXT` | `NOT NULL` | Ticker symbol of the equity asset (e.g. AAPL). |
| `sec_type` | `TEXT` | `CHECK = 'STK'` | Asset class; exclusively equities ('STK') are supported. |
| `exchange` | `TEXT` | `CHECK = 'SMART'` | Router routing target (exclusively SMART router is supported). |
| `action` | `TEXT` | `CHECK IN ('BUY', 'SELL')` | The trade direction side. |
| `quantity` | `INTEGER` | `NOT NULL`, `CHECK (quantity > 0)` | Scaled quantity of shares to trade. |
| `order_type` | `TEXT` | `NOT NULL` | Order type class (e.g., LMT, STP, MKT, MOC). |
| `target_price` | `REAL`/`TEXT` | `NULLABLE` | Limit/Stop price. Mandatory if type is LMT or STP (stored as stringified `Decimal`). |
| `tif` | `TEXT` | `DEFAULT 'GTC'` | Time-In-Force (e.g., DAY, GTC). |
| `strategy_name` | `TEXT` | `NULLABLE` | Name of the generating trading logic/strategy. |
| `status` | `TEXT` | `CHECK IN ('Created', ...)` | Order lifecycle state. |
| `retry_count` | `INTEGER` | `DEFAULT 0` | Current counter of transmission retries. |
| `transmitted_at` | `TIMESTAMP` | `NULLABLE` | Timestamp when successfully sent to TWS. |

* **Constraints & Indexes**:
  * `UNIQUE (account_id, trade_group_id, bracket_role, order_type)`: Prevents duplicate roles under same order types within a trade group.
  * `FOREIGN KEY (parent_id) REFERENCES orders (order_id) ON UPDATE CASCADE ON DELETE SET NULL`
  * `idx_orders_perm_id`: Unique partial index on `perm_id` WHERE `perm_id IS NOT NULL AND perm_id != 0`.
  * `idx_orders_trade_group`: Index on `trade_group_id`.
  * `idx_orders_status`: Index on `status`.

### 1.3 Executions Table (`executions`)
Tracks transaction details reported from TWS callbacks.

| Variable Name | Data Type | Validation Rules | Description |
| :--- | :--- | :--- | :--- |
| `exec_id` | `TEXT` | `PRIMARY KEY` | TWS execution ID (uniquely identifies a partial fill). |
| `order_id` | `INTEGER` | `NOT NULL`, `FOREIGN KEY` | Reference to corresponding order row in database. |
| `price` | `REAL`/`TEXT` | `NOT NULL` | Trade fill execution price (stored as stringified `Decimal`). |
| `qty` | `REAL`/`TEXT` | `NOT NULL` | Number of shares filled in this transaction (stored as stringified `Decimal`). |
| `commission` | `REAL`/`TEXT` | `NULLABLE` | Trade commission fee (stored as stringified `Decimal`, populated by commission callback). |
| `currency` | `TEXT` | `NULLABLE` | Execution currency (e.g., USD). |
| `executed_at` | `TIMESTAMP` | `NULLABLE` | Timestamp when the execution event occurred. |

* **Indexes**:
  * `idx_executions_order_id`: Index on `order_id`.

### 1.4 Trades Settlement Table (`trades_settlement`)
Aggregates and persists final trade results.

| Variable Name | Data Type | Validation Rules | Description |
| :--- | :--- | :--- | :--- |
| `account_id` | `TEXT` | `NOT NULL` | Brokerage account ID. |
| `trade_group_id` | `TEXT` | `NOT NULL` | Group key connecting the entry and exit legs. |
| `avg_entry_price` | `REAL`/`TEXT` | `NOT NULL` | Calculated average VWAP entry price across execution parts (stored as stringified `Decimal`). |
| `avg_exit_price` | `REAL`/`TEXT` | `NOT NULL` | Calculated average VWAP exit price across execution parts (stored as stringified `Decimal`). |
| `price_diff_slippage`| `REAL`/`TEXT` | `NOT NULL` | Difference between intended target price and executed price (stored as stringified `Decimal`). |
| `total_commissions` | `REAL`/`TEXT` | `NOT NULL` | Sum of commissions from all linked executions (stored as stringified `Decimal`). |
| `net_pnl` | `REAL`/`TEXT` | `NOT NULL` | Profit or Loss calculated as `(Exit Price - Entry Price) * Qty - Fees` (stored as stringified `Decimal`). |
| `settled_at` | `TIMESTAMP` | `DEFAULT CURRENT_TIMESTAMP` | Timestamp indicating when settlement calculations finalized. |

* **Constraints**:
  * `PRIMARY KEY (account_id, trade_group_id)`

---

## 2. CSV Interface Specification

Daily files must follow the pattern `orders_YYYY_MM_DD.csv` and use UTF-8-sig encoding.

### 2.1 CSV Fields Contract

| Variable Name | Data Type | Validation Rules | Description |
| :--- | :--- | :--- | :--- |
| `trade_group_id` | `TEXT` | `NOT NULL`, max length 64 | Unique ID linking ENTRY and exit orders of a trade setup. |
| `bracket_role` | `TEXT` | `IN ('ENTRY', 'SL', 'TP', 'EXIT')` | Bracket execution classification role. Case-insensitive. |
| `symbol` | `TEXT` | `NOT NULL`, uppercase letters | Asset symbol representing target trade instrument. |
| `sec_type` | `TEXT` | `CHECK = 'STK'` | Asset type; must match 'STK' (Equities). |
| `exchange` | `TEXT` | `CHECK = 'SMART'` | Trading exchange target; must match 'SMART'. |
| `account_id` | `TEXT` | `NOT NULL` | Associated Interactive Brokers account identifier. |
| `action` | `TEXT` | `IN ('BUY', 'SELL')` | Buying or selling trading side. Case-insensitive. |
| `quantity` | `INTEGER` | `NOT NULL`, `quantity > 0` | Target amount of shares proposed to trade. |
| `order_type` | `TEXT` | `IN ('LMT', 'STP', 'MKT', 'MOC')` | Execution trigger style (Limit, Stop, Market, Market-on-Close). |
| `target_price` | `Decimal` | `Positive if LMT/STP`, `Empty if MKT/MOC` | Reference limit or trigger activation price boundary. |
| `tif` | `TEXT` | `IN ('DAY', 'GTC')`, `Default: 'GTC'` | Order validity duration instruction (Time-In-Force). |
| `strategy_name` | `TEXT` | `Optional` | Strategy system classification key name. |

### 2.2 Time Format Contract
Datetime variables are formatted as **ISO-8601 strings with timezone offset**:
* Syntax Pattern: `YYYY-MM-DDTHH:MM:SS±HH:MM`
* Example: `2026-07-07T13:21:02+02:00`

---

## 3. Internal Python Data Structures

Mapping model classes in [app/core/models.py](file:///Users/produktmanagement/Python/github/TradeManager/app/core/models.py) are immutable `@dataclass(frozen=True)` containers.

### 3.1 Dataclass Definitions

| Class Name | Fields / Data Types | Purpose |
| :--- | :--- | :--- |
| `LegRow` | `trade_group_id: str`<br>`bracket_role: str`<br>`symbol: str`<br>`sec_type: str`<br>`exchange: str`<br>`account_id: str`<br>`action: str`<br>`quantity: int`<br>`order_type: str`<br>`target_price: Decimal \| None`<br>`tif: str`<br>`strategy_name: str` | Parsed representation of a raw CSV import row before any sizing or DB write. |
| `OrderRow` | `order_id: int`<br>`perm_id: int \| None`<br>`parent_id: int \| None`<br>`trade_group_id: str`<br>`account_id: str`<br>`bracket_role: str`<br>`symbol: str`<br>`sec_type: str`<br>`exchange: str`<br>`action: str`<br>`quantity: int`<br>`order_type: str`<br>`target_price: Decimal \| None`<br>`tif: str`<br>`strategy_name: str \| None`<br>`status: str`<br>`retry_count: int = 0`<br>`transmitted_at: str \| None = None` | Persistent intent record representing the database representation. Copied using `dataclasses.replace` to modify state. |
| `ExecutionRow` | `exec_id: str`<br>`order_id: int`<br>`price: Decimal`<br>`qty: Decimal`<br>`commission: Decimal \| None = None`<br>`currency: str \| None = None`<br>`executed_at: str \| None = None` | Atomic execution ticket representing partial/total fill reports. |
| `SettlementRow` | `account_id: str`<br>`trade_group_id: str`<br>`avg_entry_price: Decimal`<br>`avg_exit_price: Decimal`<br>`price_diff_slippage: Decimal`<br>`total_commissions: Decimal`<br>`net_pnl: Decimal`<br>`settled_at: str \| None = None` | Settlement result structure generated when exit leg completes. |

---

## 4. State Machine Transitions

The lifecycle status changes of a trade group's order are stateful and governed by the SQLite database states:

```
                  ┌──────────────┐
                  │   Created    │ (Initially imported)
                  └──────┬───────┘
                         │
                         ▼ (Placed to queue -> Transmit initiated)
                  ┌──────────────┐
                  │  Submitted   │ (Sent to Interactive Brokers socket)
                  └──────┬───────┘
                         │
        ┌────────────────┼────────────────┐
        ▼ (TWS Ack)      ▼ (TWS Cancel)   ▼ (Error/Timeout)
  ┌──────────────┐┌──────────────┐┌──────────────┐
  │ PreSubmitted ││  Cancelled   ││    Error     │ (Fail-Closed)
  └──────┬───────┘└──────────────┘└──────────────┘
         │
         ▼ (TWS Fill Callback)
  ┌──────────────┐
  │    Filled    │ (Triggers Settlement)
  └──────────────┘
```

| Source State | Destination State | Trigger Rule / Conditions |
| :--- | :--- | :--- |
| — | `Created` | Order record is parsed, downscaled successfully, and written to database. |
| `Created` | `Submitted` | Queue worker transmits parent/child legs to IBKR socket (first parent exit, then parent entry). |
| `Submitted` | `PreSubmitted` | Gateway returns receipt acknowledgment event. |
| `Submitted` | `Error` | What-If simulation fails, connection is severed, or API submission returns immediate error. |
| `PreSubmitted`| `Filled` | Order average fill price and size matches requested target amount. Triggers settlement computation. |
| `PreSubmitted`| `Cancelled` | Execution is halted via manual intervention, auto-purge, or TWS error cancellation. |
| `PreSubmitted`| `Error` | Connection disconnect limits exceeded, or Gateway reports failed transmission error. |
| `Submitted` | `Cancelled` | Brokerage cancels the order context before Gateway processing. |
| — | `Filled` | Automatic position reconciliation (`reconcile_broker_positions`) detects an unassigned broker position discrepancy and creates a synthetic `ENTRY` order (`strategy_name = NULL`, `trade_group_id = UNASSIGNED_*`) and matching execution ticket. |


---

## 5. Error Classification & Actions Matrix

Governed by [app/trading/error_codes.py](file:///Users/produktmanagement/Python/github/TradeManager/app/trading/error_codes.py), IBKR API error codes map to system actions.

| Error Class | Associated Codes | System Action / Response |
| :--- | :--- | :--- |
| **`INFO`** | `2104`, `2106`, `2107`, `2108`, `2119`, `2158`, `2100`, `2182`, `399` | Log warning/info. No execution actions are taken. System execution continues undisturbed. |
| **`RECONNECT`**| `1101`, `1102` | Pause outgoing transmissions. Block queue consumption. Gateway disconnect check logic starts. Resume once connection events clear. |
| **`RETRIABLE`**| `1100`, `1300`, `10148`, `502`, `504`, `162` | Queue worker backs off exponentially. Status reverts to `Created`. Order is queued again for retry (up to max configured retries limit). |
| **`CANCEL`** | `202`, `10147`, `10149`, `10268` | Order marked as `Cancelled` in database. Stop execution of remaining bracket elements if necessary to prevent exposure. |
| **`FATAL`** | *All other codes* (default) | Halt order transmission. Mark status as `Error`. Alert administrator immediately via Telegram message (Critical notification). |
