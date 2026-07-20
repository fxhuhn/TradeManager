---
name: sqlite-persistence
description: "Expert SQLite Persistence & Database Invariants skill for TradeManager covering WAL mode, foreign key cascades, atomic transactions, schema migrations, and backup rules."
---

> [!IMPORTANT]
> Must strictly respect `.agents/rules/workspace.md`. Do not reference or operate on files outside the active repository workspace.

# SQLite Persistence & Database Invariants Skill

This skill enforces database architecture invariants for TradeManager's local relational store (`data/trading.db`), as specified in [architecture.md](file:///Users/produktmanagement/Python/github/TradeManager/architecture.md) and [references/architecture.md](file:///Users/produktmanagement/Python/github/TradeManager/references/architecture.md).

## Core Database Invariants

### 1. WAL Mode & Foreign Key Constraints
- **Journal Mode**: The SQLite database must operate exclusively in Write-Ahead Logging mode (`PRAGMA journal_mode=WAL`).
- **Foreign Keys**: Foreign key constraints must be explicitly enabled on every connection (`PRAGMA foreign_keys = ON`).
- **Location**: Database file lives at `data/trading.db` (git-ignored, strictly local state).

### 2. State Invariants & ID Cascades
- **Single Source of Truth**: The SQLite database is the sole state store. Processing workers and state recovery loops do not maintain in-memory execution state.
- **Negative Temporary Order IDs**: Unsubmitted orders parsed from CSV are assigned negative sequence integers (`-1`, `-2`, ...) as temporary primary keys.
- **Cascade Updates**: Foreign key constraints on child legs use `ON UPDATE CASCADE`. Updating a parent entry's negative `order_id` to its real TWS order ID automatically updates all linked child brackets.
- **Decimal Storage**: Money values (`price`, `commission`, `pnl`, `slippage`) are stored with Decimal precision representations in `REAL` or `TEXT` fields and converted via `decimal_from_db()` / `parse_positive_decimal()`.

### 3. Connection & Transaction Pattern
Always use async database transactions via `get_db()` and `transaction()` from `app.core.db`:

```python
from app.core.db import get_db, transaction

async def save_order_intent(orders: list[OrderRow]) -> None:
    """Persists scaled order intent rows atomically."""
    async with get_db() as db:
        async with transaction(db):
            for order in orders:
                await db.execute(
                    """
                    INSERT INTO orders (
                        order_id, perm_id, parent_id, trade_group_id, account_id,
                        bracket_role, symbol, sec_type, exchange, action, quantity,
                        order_type, target_price, tif, strategy_name, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.order_id, order.perm_id, order.parent_id,
                        order.trade_group_id, order.account_id, order.bracket_role,
                        order.symbol, order.sec_type, order.exchange, order.action,
                        order.quantity, order.order_type,
                        float(order.target_price) if order.target_price else None,
                        order.tif, order.strategy_name, order.status
                    )
                )
```

### 4. Schema Migrations & Backups
- **Migration Tracking**: Applied migrations are recorded in `schema_version` (`version INTEGER PRIMARY KEY`, `applied_at TIMESTAMP`).
- **Backup Rule**: Online backups are executed non-blocking via `VACUUM INTO` in `run_db_backup()`.
- **Integrity Validation**: Use `verify_db_integrity()` (`PRAGMA integrity_check`) before initializing system loops.

### 5. Rules Compliance
- **Strict Conciseness**: Strictly adhere to [.agents/rules/concise.md](.agents/rules/concise.md).
- **Architecture Sync**: Any modification to database schemas or helper functions in `app.core.db` must be updated in `architecture.md` and `references/architecture.md`.
