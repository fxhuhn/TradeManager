-- migrations/001_initial.sql
-- DDL-Schema für das IBKR Equities Trading System (Release 1)

-- 1. Verwaltungstabelle für Schema-Versionen
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Tabelle für Order-Intentionen und -Status
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY,
    perm_id INTEGER,
    parent_id INTEGER,
    trade_group_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    bracket_role TEXT NOT NULL CHECK (bracket_role IN ('ENTRY', 'SL', 'TP', 'EXIT')),
    symbol TEXT NOT NULL,
    sec_type TEXT NOT NULL CHECK (sec_type = 'STK'),
    exchange TEXT NOT NULL CHECK (exchange = 'SMART'),
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    target_price REAL,
    tif TEXT DEFAULT 'GTC',
    strategy_name TEXT,
    status TEXT NOT NULL CHECK (status IN ('Created', 'Submitted', 'PreSubmitted', 'Filled', 'Cancelled', 'Error')),
    retry_count INTEGER DEFAULT 0,
    transmitted_at TIMESTAMP,
    FOREIGN KEY (parent_id) REFERENCES orders (order_id) ON UPDATE CASCADE,
    -- Constraint für UPSERT-Betrieb:
    UNIQUE (account_id, trade_group_id, bracket_role)
);

-- Partieller Unique Index auf perm_id (erlaubt mehrere NULLs oder 0s, aber nur eine eindeutige perm_id)
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_perm_id 
ON orders (perm_id) 
WHERE perm_id IS NOT NULL AND perm_id != 0;

-- Indexe zur Performance-Optimierung
CREATE INDEX IF NOT EXISTS idx_orders_trade_group ON orders (trade_group_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

-- 3. Tabelle für atomare Teilausführungen (Partial Fills)
CREATE TABLE IF NOT EXISTS executions (
    exec_id TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL,
    price REAL NOT NULL,
    qty REAL NOT NULL,
    commission REAL, -- Nullable, da verzögert oder fehlend (Paper-Trading)
    currency TEXT,
    executed_at TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders (order_id) ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_executions_order_id ON executions (order_id);

-- 4. Tabelle für das konsolidierte Ergebnis geschlossener Trades (Settlement)
CREATE TABLE IF NOT EXISTS trades_settlement (
    account_id TEXT NOT NULL,
    trade_group_id TEXT NOT NULL,
    avg_entry_price REAL NOT NULL,
    avg_exit_price REAL NOT NULL,
    price_diff_slippage REAL NOT NULL,
    total_commissions REAL NOT NULL,
    net_pnl REAL NOT NULL,
    settled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, trade_group_id)
);
